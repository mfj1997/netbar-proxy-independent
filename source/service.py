"""Nonblocking GUI adapter for the asyncio proxy and upstream checks."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import queue
import threading
from typing import Any

from proxy_engine import ProxyConfig, ProxyEngine, check_upstream
from settings import validate_config


class ProxyService:
    """Own one proxy thread and one optional, short-lived diagnostic thread."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = False
        self._logs: queue.Queue[str] = queue.Queue(maxsize=200)
        self._check_thread: threading.Thread | None = None
        self._check_result: dict[str, Any] | None = None
        self._snapshot: dict[str, Any] = {
            "state": "stopped",
            "error": "",
            "listen_host": "127.0.0.1",
            "listen_port": 10808,
            "bytes_up": 0,
            "bytes_down": 0,
            "active_connections": 0,
            "total_connections": 0,
            "failures": 0,
            "udp_supported": True,
        }

    @property
    def state(self) -> str:
        with self._lock:
            return str(self._snapshot["state"])

    @property
    def status(self) -> str:
        return self.state

    @property
    def checking(self) -> bool:
        with self._lock:
            return bool(self._check_thread and self._check_thread.is_alive())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    @staticmethod
    def _safe_message(message: object, config: ProxyConfig) -> str:
        text = str(message)
        for secret in (config.password, config.username):
            if secret:
                text = text.replace(secret, "[已隐藏]")
        # Avoid embedded newlines and unbounded exception / event strings.
        return " ".join(text.splitlines())[:500]

    def _log(self, message: object, config: ProxyConfig) -> None:
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

    def drain_logs(self) -> list[str]:
        result = []
        while True:
            try:
                result.append(self._logs.get_nowait())
            except queue.Empty:
                return result

    def start(self, config: ProxyConfig) -> bool:
        """Validate immediately, then start in the background. False means busy."""
        config = validate_config(replace(config))
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_requested = False
            self._snapshot.update(
                state="starting",
                error="",
                listen_host=config.listen_host,
                listen_port=config.listen_port,
                bytes_up=0,
                bytes_down=0,
                active_connections=0,
                total_connections=0,
                failures=0,
                udp_supported=config.mode in ("direct", "socks5"),
            )
            self._thread = threading.Thread(
                target=self._thread_main,
                args=(config,),
                name="local-proxy",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        """Request shutdown without joining, waiting, or touching other processes."""
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
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
                # The worker may have just closed its own loop.
                pass

    def shutdown(self, timeout: float = 3.0) -> bool:
        """Request stop and briefly join the proxy on application exit only."""
        self.stop()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, timeout))
        return thread is None or not thread.is_alive()

    def _publish(self, engine: ProxyEngine, state: str) -> None:
        details = dict(engine.snapshot())
        with self._lock:
            self._snapshot.update(details)
            self._snapshot["state"] = (
                "stopping" if self._stop_requested and state == "running" else state
            )

    def _thread_main(self, config: ProxyConfig) -> None:
        try:
            asyncio.run(self._run(config))
        except Exception as exc:
            error = self._safe_message(exc, config)
            with self._lock:
                self._snapshot.update(state="failed", error=error, active_connections=0)
            self._log("代理启动或运行失败：" + error, config)
        finally:
            with self._lock:
                self._loop = None
                self._stop_event = None
                if self._snapshot["state"] != "failed":
                    self._snapshot.update(state="stopped", active_connections=0)

    async def _run(self, config: ProxyConfig) -> None:
        engine = ProxyEngine(config, on_event=lambda event: self._log(event, config))
        stop_event = asyncio.Event()
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._stop_event = stop_event
            if self._stop_requested:
                stop_event.set()
        try:
            if not stop_event.is_set():
                await engine.start()
                self._publish(engine, "running")
                self._log("本地代理已开始监听。", config)
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.15)
                except asyncio.TimeoutError:
                    self._publish(engine, "running")
        finally:
            await engine.stop()
            self._publish(engine, "stopped")
            self._log("本地代理已停止。", config)

    def check_upstream_async(self, config: ProxyConfig) -> bool:
        """Run one protocol/transport check without blocking the caller."""
        config = validate_config(replace(config))
        with self._lock:
            if self._check_thread is not None and self._check_thread.is_alive():
                return False
            self._check_result = None
            self._check_thread = threading.Thread(
                target=self._check_main,
                args=(config,),
                name="proxy-diagnostic",
                daemon=True,
            )
            self._check_thread.start()
            return True

    def _check_main(self, config: ProxyConfig) -> None:
        try:
            result = asyncio.run(check_upstream(config))
            if not isinstance(result, dict):
                raise ValueError("检测结果格式无效。")
            result = {
                "ok": result.get("ok") is True,
                "stage": str(result.get("stage", "unknown")),
                "message": self._safe_message(result.get("message", ""), config),
            }
        except Exception as exc:
            result = {
                "ok": False,
                "stage": "error",
                "message": self._safe_message(exc, config),
            }
        with self._lock:
            self._check_result = result

    def poll_check_result(self) -> dict[str, Any] | None:
        """Consume the newest completed result, or return None while waiting."""
        with self._lock:
            result = self._check_result
            self._check_result = None
            return dict(result) if result is not None else None
