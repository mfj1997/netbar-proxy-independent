"""Bounded checks of our listener and an explicitly chosen TLS destination.

These checks never infer game entitlement or contact a built-in public target.
Returned messages are fixed text: protocol replies, addresses and secrets are
not copied into diagnostics or logs.
"""
from __future__ import annotations

import asyncio
import ipaddress
import math
import ssl
import time

from proxy_engine import ProxyError, _pack_address, _read_address
from settings import validate_target


class _CheckFailed(Exception):
    """A fixed, non-sensitive protocol failure message."""


def _port(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("端口必须是 1 到 65535 之间的整数。")
    return value


def _timeout(value: object) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 120):
        raise ValueError("检测超时必须大于 0 秒且不超过 120 秒。")
    return float(value)


def _listener(host: object, port: object) -> tuple[str, int]:
    if not isinstance(host, str):
        raise ValueError("本地检测只允许环回 IP。")
    host = "127.0.0.1" if host.strip().lower() == "localhost" else host.strip()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("本地检测只允许环回 IP。") from None
    if not address.is_loopback or "%" in host:
        raise ValueError("本地检测只允许环回 IP。")
    return str(address), _port(port)


def validate_route_target(host: object, port: object = 443) -> tuple[str, int]:
    """Validate an explicit host/IP and port without resolving or connecting."""
    try:
        normalized, port = validate_target(host, port)
    except ValueError:
        raise ValueError("请填写目标域名或 IP，不含协议、路径或认证信息。") from None
    return normalized, port


async def _greet(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    if await reader.readexactly(2) != b"\x05\x00":
        raise _CheckFailed("本地端口未通过 SOCKS5 协议检查。")


async def _close(writer: asyncio.StreamWriter | None, deadline: float) -> None:
    if writer is None:
        return
    transport = writer.transport
    if transport is None:
        return
    writer.close()
    task = asyncio.current_task()
    if deadline <= asyncio.get_running_loop().time() or (task and task.cancelling()):
        transport.abort()
        return
    try:
        async with asyncio.timeout_at(deadline):
            await writer.wait_closed()
    except asyncio.CancelledError:
        transport.abort()
        raise
    except (OSError, TimeoutError, ssl.SSLError):
        transport.abort()


def _result(ok: bool, stage: str, message: str, started: float) -> dict:
    return {"ok": ok, "stage": stage, "message": message,
            "latency_ms": round((time.monotonic() - started) * 1000, 1)}


async def verify_listener(host: str, port: int, timeout: float = 3) -> dict:
    """Negotiate SOCKS5 on loopback; a listening TCP socket alone is insufficient."""
    started = time.monotonic()
    writer = None
    deadline = asyncio.get_running_loop().time()
    ok = False
    message = "本地 SOCKS5 检查失败。"
    try:
        host, port = _listener(host, port)
        deadline += _timeout(timeout)
        async with asyncio.timeout_at(deadline):
            reader, writer = await asyncio.open_connection(host, port)
            await _greet(reader, writer)
        ok = True
        message = "本地 SOCKS5 协议检查通过；尚未验证目标转发。"
    except ValueError:
        message = "本地监听检测配置无效。"
    except _CheckFailed as exc:
        message = str(exc)
    except TimeoutError:
        message = "本地 SOCKS5 协议检查超时。"
    except (OSError, asyncio.IncompleteReadError):
        message = "本地代理无法连接或 SOCKS5 回复不完整。"
    finally:
        await _close(writer, deadline)
    return _result(ok, "listener", message, started)


async def check_route_tls(
    listen_host: str, listen_port: int, target_host: str,
    target_port: int = 443, timeout: float = 10,
) -> dict:
    """Verify a real TLS handshake through the running local SOCKS5 listener.

The destination is mandatory. Success proves this one TLS route and validates
its certificate/hostname, not UDP, every application, or official entitlement.
No HTTP/application payload or account data is sent.
    """
    started = time.monotonic()
    stage = "configuration"
    writer = None
    deadline = asyncio.get_running_loop().time()
    ok = False
    message = "目标连接检测失败。"
    try:
        listen_host, listen_port = _listener(listen_host, listen_port)
        target_host, target_port = validate_route_target(target_host, target_port)
        deadline += _timeout(timeout)
        async with asyncio.timeout_at(deadline):
            stage = "listener"
            reader, writer = await asyncio.open_connection(listen_host, listen_port)
            await _greet(reader, writer)
            stage = "tunnel"
            writer.write(b"\x05\x01\x00" + _pack_address(target_host, target_port))
            await writer.drain()
            response = await reader.readexactly(4)
            if response[0] != 5 or response[2] != 0:
                raise _CheckFailed("本地 SOCKS5 隧道回复无效。")
            await _read_address(reader, response[3])
            if response[1] != 0:
                raise _CheckFailed("目标隧道未建立，请检查上游认证、线路和目标端口。")
            stage = "tls"
            context = ssl.create_default_context()
            await writer.start_tls(
                context, server_hostname=target_host,
                ssl_handshake_timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                ssl_shutdown_timeout=0.2,
            )
            if writer.get_extra_info("ssl_object") is None:
                raise _CheckFailed("目标未完成 TLS 握手。")
            ok = True
            message = "经本地代理的目标 TLS 握手及证书校验通过；不代表游戏或网吧权益。"
    except ValueError:
        message = "检测配置无效，请填写目标域名或 IP、有效端口与超时。"
    except _CheckFailed as exc:
        message = str(exc)
    except ProxyError:
        message = "目标隧道的地址或协议回复无效。"
    except ssl.SSLCertVerificationError:
        message = "目标 TLS 证书或主机名校验失败。"
    except TimeoutError:
        message = {"listener": "本地 SOCKS5 检查超时。",
                   "tunnel": "目标隧道建立超时。",
                   "tls": "目标 TLS 握手超时。"}.get(stage, "目标检测超时。")
    except (OSError, ssl.SSLError, asyncio.IncompleteReadError):
        message = {"listener": "本地代理连接或协议检查失败。",
                   "tunnel": "目标隧道连接失败或回复不完整。",
                   "tls": "目标 TLS 握手失败，请确认目标支持 TLS。"}.get(stage, "目标检测失败。")
    finally:
        await _close(writer, deadline)
    return _result(ok, stage, message, started)
