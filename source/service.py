"""Owned proxy lifecycle and separate, cancellable readiness checks."""
from __future__ import annotations
import asyncio
import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
import queue
import threading
import time
from typing import Any, Callable

from diagnostics import verify_listener, check_route_tls
from proxy_engine import ProxyConfig, ProxyEngine, check_upstream
from settings import validate_config, validate_target


def _unchecked(stage: str) -> dict:
    return {"ok": None, "stage": stage, "message": "尚未检测。", "latency_ms": None}


@dataclass(eq=False)
class _CheckJob:
    kind: str
    thread: threading.Thread | None = None
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None
    cancelled: bool = False


class ProxyService:
    """Manage only this instance's sockets, worker and bounded diagnostics."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread = None
        self._loop = None
        self._stop_event = None
        self._stop_requested = False
        self._active_config = None
        self._diagnostic_config = None
        self._logs = queue.Queue(maxsize=200)
        self._jobs: dict[str, _CheckJob] = {}
        self._all_jobs: set[_CheckJob] = set()
        self._results = {"upstream": None, "route": None}
        self._snapshot = {
            "state": "stopped", "error": "", "mode": "direct",
            "listen_host": "127.0.0.1", "listen_port": 10808,
            "bytes_up": 0, "bytes_down": 0, "active_connections": 0,
            "total_connections": 0, "failures": 0, "udp_supported": True,
            "listener_check": _unchecked("listener"),
            "upstream_check": _unchecked("upstream"),
            "route_check": _unchecked("tls"), "entitlement": "unverified",
        }

    @property
    def state(self):
        with self._lock:
            return self._snapshot["state"]

    @property
    def status(self):
        return self.state

    @property
    def checking(self):
        with self._lock:
            return "upstream" in self._jobs

    @property
    def route_checking(self):
        with self._lock:
            return "route" in self._jobs

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._snapshot)

    @staticmethod
    def _safe_message(message, config):
        value = str(message)
        for secret in (config.password, config.username):
            if secret:
                value = value.replace(secret, "[已隐藏]")
        return " ".join(value.splitlines())[:500]

    def _log(self, message, config):
        value = self._safe_message(message, config)
        try:
            self._logs.put_nowait(value)
        except queue.Full:
            try:
                self._logs.get_nowait()
            except queue.Empty:
                pass
            try:
                self._logs.put_nowait(value)
            except queue.Full:
                pass

    def drain_logs(self):
        result = []
        while True:
            try:
                result.append(self._logs.get_nowait())
            except queue.Empty:
                return result

    def _invalidate_locked(self, kind):
        job = self._jobs.pop(kind, None)
        self._results[kind] = None
        self._snapshot[kind + "_check"] = _unchecked("tls" if kind == "route" else "upstream")
        if job is not None:
            job.cancelled = True
            if job.loop is not None and job.task is not None:
                try:
                    job.loop.call_soon_threadsafe(job.task.cancel)
                except RuntimeError:
                    pass

    def invalidate_diagnostics(self):
        """Invalidate stopped-form checks before accepting edited settings."""
        with self._lock:
            if self._snapshot["state"] in ("starting", "running", "stopping"):
                return
            for kind in ("upstream", "route"):
                self._invalidate_locked(kind)
            self._diagnostic_config = None

    def invalidate_route_diagnostics(self):
        with self._lock:
            self._invalidate_locked("route")

    def start(self, config):
        config = validate_config(replace(config))
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            for kind in ("upstream", "route"):
                self._invalidate_locked(kind)
            self._stop_requested = False
            self._active_config = config
            self._diagnostic_config = config
            self._snapshot.update(
                state="starting", error="", mode=config.mode,
                listen_host=config.listen_host, listen_port=config.listen_port,
                bytes_up=0, bytes_down=0, active_connections=0, total_connections=0,
                failures=0, udp_supported=config.mode in ("direct", "socks5"),
                listener_check=_unchecked("listener"), entitlement="unverified",
            )
            self._thread = threading.Thread(target=self._thread_main, args=(config,), name="local-proxy", daemon=True)
            self._thread.start()
            return True

    def stop(self):
        with self._lock:
            for kind in ("upstream", "route"):
                self._invalidate_locked(kind)
            self._diagnostic_config = None
            self._snapshot["listener_check"] = _unchecked("listener")
            if self._thread is None or not self._thread.is_alive():
                if self._snapshot["state"] != "failed":
                    self._snapshot["state"] = "stopped"
                return
            self._stop_requested = True
            self._snapshot["state"] = "stopping"
            loop, event = self._loop, self._stop_event
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    def shutdown(self, timeout=3.0):
        deadline = time.monotonic() + max(0, timeout)
        self.stop()
        with self._lock:
            threads = [self._thread] + [job.thread for job in self._all_jobs]
        for thread in threads:
            if thread is not None and thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
        return not any(thread is not None and thread.is_alive() for thread in threads)

    def _publish(self, engine, state):
        details = dict(engine.snapshot())
        details["listen_port"] = details.get("listen_port") or engine.config.listen_port
        with self._lock:
            self._snapshot.update(details)
            self._snapshot["state"] = "stopping" if self._stop_requested and state in ("starting", "running") else state

    def _thread_main(self, config):
        try:
            asyncio.run(self._run(config))
        except Exception as exc:
            error = self._safe_message(exc, config)
            with self._lock:
                for kind in ("upstream", "route"):
                    self._invalidate_locked(kind)
                self._snapshot.update(state="failed", error=error, active_connections=0)
                self._snapshot["listener_check"] = {"ok": False, "stage": "listener", "message": error, "latency_ms": None}
            self._log("本地代理启动或运行失败：" + error, config)
        finally:
            with self._lock:
                self._loop = None
                self._stop_event = None
                self._active_config = None
                if self._snapshot["state"] != "failed":
                    self._snapshot.update(state="stopped", active_connections=0)

    async def _run(self, config):
        engine = ProxyEngine(config, on_event=lambda event: self._log(event, config))
        stop_event = asyncio.Event()
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._stop_event = stop_event
            if self._stop_requested:
                stop_event.set()
        try:
            if stop_event.is_set():
                return
            await engine.start()
            self._publish(engine, "starting")
            probe = asyncio.create_task(verify_listener(config.listen_host, engine.snapshot()["listen_port"], timeout=min(3.0, config.timeout)))
            stopped = asyncio.create_task(stop_event.wait())
            try:
                done, _ = await asyncio.wait((probe, stopped), return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:
                    return
                result = self._normalize_result(await probe, config)
                if not result["ok"]:
                    raise RuntimeError(result["message"] or "本地 SOCKS5 握手未通过。")
                with self._lock:
                    if self._stop_requested:
                        return
                    self._snapshot["listener_check"] = result
                    self._publish(engine, "running")
                    if config.mode == "direct":
                        self._snapshot["upstream_check"] = {"ok": None, "stage": "not_required", "message": "直连模式无上游；尚未验证目标转发。", "latency_ms": None}
                    else:
                        self._launch_check_locked("upstream", config, lambda: check_upstream(config))
                self._log("本地 SOCKS5 握手已通过；上游和目标转发状态单独检测。", config)
            finally:
                for task in (probe, stopped):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(probe, stopped, return_exceptions=True)
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.15)
                except asyncio.TimeoutError:
                    self._publish(engine, "running")
        finally:
            await engine.stop()
            self._publish(engine, "stopped")
            self._log("本地代理已停止。", config)

    @classmethod
    def _normalize_result(cls, result, config):
        if not isinstance(result, dict):
            raise ValueError("检测结果格式无效。")
        latency = result.get("latency_ms")
        if type(latency) not in (float, int) or not math.isfinite(latency) or latency < 0:
            latency = None
        return {"ok": result.get("ok") is True, "stage": str(result.get("stage", "error")),
                "message": cls._safe_message(result.get("message", "检测完成。"), config), "latency_ms": latency}

    def _launch_check_locked(self, kind, config, factory):
        if kind in self._jobs:
            return False
        job = _CheckJob(kind)
        self._results[kind] = None
        self._snapshot[kind + "_check"] = {"ok": None, "stage": "checking", "message": "正在检测…", "latency_ms": None}
        job.thread = threading.Thread(target=self._check_main, args=(job, config, factory), name="proxy-" + kind + "-diagnostic", daemon=True)
        self._jobs[kind] = job
        self._all_jobs.add(job)
        job.thread.start()
        return True

    def check_upstream_async(self, config):
        config = validate_config(replace(config))
        with self._lock:
            if self._snapshot["state"] in ("starting", "stopping"):
                return False
            if self._snapshot["state"] == "running" and config != self._active_config:
                raise ValueError("请先停止代理，再检测修改后的配置。")
            if config != self._diagnostic_config:
                self._invalidate_locked("upstream")
                self._invalidate_locked("route")
                self._diagnostic_config = config
            if self._snapshot["state"] != "running":
                self._snapshot.update(mode=config.mode, listen_host=config.listen_host, listen_port=config.listen_port)
            return self._launch_check_locked("upstream", config, lambda: check_upstream(config))

    def check_route_async(self, target_host, target_port=443):
        target_host, target_port = validate_target(target_host, target_port)
        with self._lock:
            if self._snapshot["state"] != "running" or self._active_config is None:
                return False
            config = self._active_config
            host, port = self._snapshot["listen_host"], self._snapshot["listen_port"]
            return self._launch_check_locked("route", config, lambda: check_route_tls(host, port, target_host, target_port, timeout=config.timeout))

    def _check_main(self, job, config, factory):
        started = time.monotonic()
        async def perform():
            with self._lock:
                job.loop = asyncio.get_running_loop()
                job.task = asyncio.current_task()
                if job.cancelled:
                    raise asyncio.CancelledError
            return await asyncio.wait_for(factory(), timeout=config.timeout)
        try:
            result = self._normalize_result(asyncio.run(perform()), config)
        except asyncio.CancelledError:
            result = None
        except (TimeoutError, asyncio.TimeoutError):
            result = {"ok": False, "stage": "timeout", "message": "检测超时，请检查配置和服务是否可用。", "latency_ms": None}
        except Exception:
            result = {"ok": False, "stage": "error", "message": "检测未能完成，请检查配置后重试。", "latency_ms": None}
        finally:
            with self._lock:
                self._all_jobs.discard(job)
                job.loop = None
                job.task = None
        with self._lock:
            if self._jobs.get(job.kind) is not job or job.cancelled:
                return
            self._jobs.pop(job.kind, None)
            if result is not None:
                if result["latency_ms"] is None:
                    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
                self._snapshot[job.kind + "_check"] = result
                self._results[job.kind] = result

    def _poll_result(self, kind):
        with self._lock:
            result = self._results[kind]
            self._results[kind] = None
            return dict(result) if result is not None else None

    def poll_check_result(self):
        return self._poll_result("upstream")

    def poll_route_result(self):
        return self._poll_result("route")

    def diagnostic_report(self):
        """Strict export allowlist: no free text, destinations or credentials."""
        snap = self.snapshot()
        stages = {"listener", "upstream", "tls", "tunnel", "transport", "authentication", "configuration", "not_required", "checking", "timeout", "error"}
        def report_check(name):
            check = snap.get(name, {})
            ok, latency = check.get("ok"), check.get("latency_ms")
            return {"ok": ok if ok is True or ok is False else None,
                    "stage": check.get("stage") if check.get("stage") in stages else "unknown",
                    "latency_ms": latency if type(latency) in (int, float) and math.isfinite(latency) and latency >= 0 else None}
        return {
            "schema_version": 1, "app_version": "1.1.0", "created_utc": datetime.now(timezone.utc).isoformat(),
            "state": snap["state"] if snap["state"] in {"starting", "running", "stopping", "stopped", "failed"} else "unknown",
            "mode": snap["mode"] if snap["mode"] in {"direct", "socks5", "http", "https"} else "unknown",
            "listener_check": report_check("listener_check"), "upstream_check": report_check("upstream_check"),
            "route_check": report_check("route_check"), "entitlement": "unverified",
            "scope": "TCP/TLS diagnostics only; no game or entitlement verification",
        }
