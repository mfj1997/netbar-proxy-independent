"""Independent loopback SOCKS5 proxy, implemented using Python's standard library.

This module does not modify operating-system proxy settings. Process/listener
health and upstream transport checks do not claim any game entitlement.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import ipaddress
import socket
import ssl
import struct
import time
from typing import Callable


MODES = {"direct", "socks5", "http", "https"}
MAX_HEADER = 16384
MAX_CONNECTIONS = 256


@dataclass
class ProxyConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 10808
    mode: str = "direct"
    upstream_host: str = ""
    upstream_port: int = 1080
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    timeout: float = 10.0
    allow_private_targets: bool = True

    def validate(self) -> None:
        if not all(isinstance(value, str) for value in (
            self.listen_host, self.mode, self.upstream_host, self.username, self.password
        )):
            raise ValueError("地址、模式和认证信息须为文本。")
        try:
            address = ipaddress.ip_address(self.listen_host)
        except ValueError as exc:
            raise ValueError("监听地址须为环回 IP，例如 127.0.0.1 或 ::1。") from exc
        if not address.is_loopback:
            raise ValueError("本程序只允许在本机环回地址监听。")
        if isinstance(self.listen_port, bool) or not isinstance(self.listen_port, int):
            raise ValueError("本地端口须为整数。")
        if not 0 <= self.listen_port <= 65535:
            raise ValueError("本地端口须在 0 至 65535 之间。")
        if self.mode not in MODES:
            raise ValueError("不支持的代理模式。")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not 0.2 <= self.timeout <= 120:
            raise ValueError("连接超时须在 0.2 至 120 秒之间。")
        if not isinstance(self.allow_private_targets, bool):
            raise ValueError("私有目标设置须为布尔值。")
        if self.mode != "direct":
            if not self.upstream_host or any(c in self.upstream_host for c in "\r\n\x00 /"):
                raise ValueError("请填写有效的上游服务器主机名或 IP。")
            if not isinstance(self.upstream_port, int) or isinstance(self.upstream_port, bool):
                raise ValueError("上游端口须为整数。")
            if not 1 <= self.upstream_port <= 65535:
                raise ValueError("上游端口须在 1 至 65535 之间。")
            if bool(self.username) != bool(self.password):
                raise ValueError("用户名与密码须同时填写或同时留空。")
            if any(c in self.username + self.password for c in "\r\n\x00"):
                raise ValueError("认证信息不能包含换行符或空字符。")
            if self.mode == "socks5" and (
                len(self.username.encode("utf-8")) > 255
                or len(self.password.encode("utf-8")) > 255
            ):
                raise ValueError("SOCKS5 用户名或密码超过协议长度限制。")
            if self.mode in {"http", "https"} and ":" in self.username:
                raise ValueError("HTTP 代理用户名不能包含冒号。")
            if len((self.username + self.password).encode("utf-8")) > 4096:
                raise ValueError("认证信息过长。")


class ProxyError(Exception):
    def __init__(self, message: str, reply: int = 1):
        super().__init__(message)
        self.reply = reply


def _pack_address(host: str, port: int) -> bytes:
    if not 0 <= port <= 65535:
        raise ProxyError("目标端口无效。", 8)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            raw = host.encode("idna")
        except UnicodeError as exc:
            raise ProxyError("目标地址无效。", 8) from exc
        if not 1 <= len(raw) <= 255 or any(c in raw for c in (0, 10, 13)):
            raise ProxyError("目标地址无效。", 8)
        prefix = b"\x03" + bytes([len(raw)]) + raw
    else:
        prefix = bytes([1 if address.version == 4 else 4]) + address.packed
    return prefix + struct.pack("!H", port)


async def _read_address(reader: asyncio.StreamReader, atyp: int) -> tuple[str, int]:
    if atyp == 1:
        host = str(ipaddress.ip_address(await reader.readexactly(4)))
    elif atyp == 4:
        host = str(ipaddress.ip_address(await reader.readexactly(16)))
    elif atyp == 3:
        length = (await reader.readexactly(1))[0]
        if not length:
            raise ProxyError("目标地址无效。", 8)
        raw = await reader.readexactly(length)
        try:
            host = raw.decode("ascii")
        except UnicodeError as exc:
            raise ProxyError("目标地址无效。", 8) from exc
        if any(c in host for c in "\r\n\x00 /"):
            raise ProxyError("目标地址无效。", 8)
    else:
        raise ProxyError("不支持的地址类型。", 8)
    port = struct.unpack("!H", await reader.readexactly(2))[0]
    return host, port


def _unpack_datagram(data: bytes) -> tuple[str, int, bytes]:
    if len(data) < 4 or data[:3] != b"\x00\x00\x00":
        raise ProxyError("不支持分片或无效的 UDP 数据包。")
    atyp, offset = data[3], 4
    if atyp in (1, 4):
        count = 4 if atyp == 1 else 16
        if len(data) < offset + count + 2:
            raise ProxyError("UDP 数据包不完整。")
        host = str(ipaddress.ip_address(data[offset:offset + count]))
        offset += count
    elif atyp == 3:
        if len(data) < 5 or data[4] == 0:
            raise ProxyError("UDP 地址无效。")
        count, offset = data[4], 5
        if len(data) < offset + count + 2:
            raise ProxyError("UDP 数据包不完整。")
        try:
            host = data[offset:offset + count].decode("ascii")
        except UnicodeError as exc:
            raise ProxyError("UDP 地址无效。") from exc
        if any(c in host for c in "\r\n\x00 /"):
            raise ProxyError("UDP 地址无效。")
        offset += count
    else:
        raise ProxyError("UDP 地址类型不支持。")
    port = struct.unpack("!H", data[offset:offset + 2])[0]
    if not port:
        raise ProxyError("目标端口无效。")
    return host, port, data[offset + 2:]


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    except (Exception, asyncio.CancelledError):
        pass


async def _open_upstream(config: ProxyConfig):
    context = ssl.create_default_context() if config.mode == "https" else None
    return await asyncio.wait_for(
        asyncio.open_connection(
            config.upstream_host,
            config.upstream_port,
            ssl=context,
            server_hostname=config.upstream_host if context else None,
            limit=MAX_HEADER,
        ),
        config.timeout,
    )


async def _socks_auth(reader, writer, config: ProxyConfig) -> None:
    # When credentials are supplied, require that authentication method;
    # silently falling back to anonymous would misreport authentication.
    method = 2 if config.username else 0
    writer.write(bytes([5, 1, method]))
    await writer.drain()
    response = await reader.readexactly(2)
    if response != bytes([5, method]):
        raise ProxyError("上游 SOCKS5 不接受所选认证方式。", 2)
    if method == 2:
        user, password = config.username.encode("utf-8"), config.password.encode("utf-8")
        writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        await writer.drain()
        if await reader.readexactly(2) != b"\x01\x00":
            raise ProxyError("上游 SOCKS5 认证失败。", 2)


async def _socks_request(reader, writer, command: int, host: str, port: int):
    writer.write(b"\x05" + bytes([command]) + b"\x00" + _pack_address(host, port))
    await writer.drain()
    header = await reader.readexactly(4)
    if header[0] != 5 or header[2] != 0:
        raise ProxyError("上游 SOCKS5 回复无效。")
    address = await _read_address(reader, header[3])
    if header[1] != 0:
        raise ProxyError("上游 SOCKS5 拒绝代理请求。", header[1] if header[1] <= 8 else 1)
    return address


async def check_upstream(config: ProxyConfig) -> dict:
    """Check transport/authentication only; never contact a third-party target."""
    writer = None
    try:
        config.validate()
        if config.mode == "direct":
            return {"ok": True, "stage": "configuration", "message": "直连模式配置有效；尚未测试目标连接。"}
        reader, writer = await _open_upstream(config)
        if config.mode == "socks5":
            await asyncio.wait_for(_socks_auth(reader, writer, config), config.timeout)
            return {"ok": True, "stage": "authentication", "message": "SOCKS5 连接与认证通过；尚未测试目标转发。"}
        kind = "HTTPS TLS" if config.mode == "https" else "HTTP TCP"
        return {"ok": True, "stage": "transport", "message": f"{kind} 连接已建立；尚未验证代理认证或 CONNECT 转发。"}
    except ssl.SSLCertVerificationError:
        return {"ok": False, "stage": "tls", "message": "上游 TLS 证书验证失败。"}
    except ValueError as exc:
        return {"ok": False, "stage": "configuration", "message": str(exc)}
    except ProxyError as exc:
        return {"ok": False, "stage": "authentication", "message": str(exc)}
    except (asyncio.TimeoutError, TimeoutError):
        return {"ok": False, "stage": "transport", "message": "连接或协议协商超时。"}
    except (OSError, asyncio.IncompleteReadError, ssl.SSLError):
        return {"ok": False, "stage": "transport", "message": "上游连接失败或协议回复不完整。"}
    finally:
        await _close_writer(writer)


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, receive):
        self.receive = receive
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.receive(data, addr)

    def error_received(self, exc):
        # OS datagram errors reveal neither user data nor destination in logs.
        pass


class _UdpAssociation:
    def __init__(self, engine, peer_ip: str, requested_port: int):
        self.engine = engine
        self.peer_ip = str(ipaddress.ip_address(peer_ip))
        self.client_port = requested_port or None
        self.client_address = None
        self.transport = None
        self.outbound: dict[int, asyncio.DatagramTransport] = {}
        self.outbound_lock = asyncio.Lock()
        self.remote_allowed: dict[tuple[str, int], float] = {}
        self.upstream_reader = None
        self.upstream_writer = None
        self.upstream_relay = None
        self.upstream_family = None
        self.tasks: set[asyncio.Task] = set()
        self.closed = False

    async def start(self):
        loop = asyncio.get_running_loop()
        family = socket.AF_INET6 if ":" in self.engine.config.listen_host else socket.AF_INET
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._from_client),
            local_addr=(self.engine.config.listen_host, 0),
            family=family,
        )
        if self.engine.config.mode == "socks5":
            cfg = self.engine.config
            self.upstream_reader, self.upstream_writer = await _open_upstream(cfg)
            async def associate():
                await _socks_auth(self.upstream_reader, self.upstream_writer, cfg)
                return await _socks_request(
                    self.upstream_reader, self.upstream_writer, 3, "0.0.0.0", 0
                )
            host, port = await asyncio.wait_for(associate(), cfg.timeout)
            if host in {"0.0.0.0", "::"}:
                host = self.upstream_writer.get_extra_info("peername")[0]
            if not port:
                raise ProxyError("上游未提供有效 UDP 中继端口。")
            choices = await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM), cfg.timeout
            )
            if not choices:
                raise ProxyError("无法解析上游 UDP 中继。")
            self.upstream_family = choices[0][0]
            self.upstream_relay = choices[0][4]
            await self._get_outbound(self.upstream_family)
        return self.transport.get_extra_info("sockname")[:2]

    async def _get_outbound(self, family):
        async with self.outbound_lock:
            if family not in self.outbound:
                local = ("::", 0) if family == socket.AF_INET6 else ("0.0.0.0", 0)
                transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: _Datagrams(self._from_remote), local_addr=local, family=family
                )
                if self.closed:
                    transport.close()
                    raise ProxyError("UDP 会话已关闭。")
                self.outbound[family] = transport
        return self.outbound[family]

    def _from_client(self, data, addr):
        if self.closed or len(self.tasks) >= 32:
            return
        if str(ipaddress.ip_address(addr[0])) != self.peer_ip:
            return
        if self.client_port is not None and addr[1] != self.client_port:
            return
        try:
            host, port, payload = _unpack_datagram(data)
        except ProxyError:
            self.engine._failures += 1
            return
        self.client_port = addr[1]
        self.client_address = addr
        task = asyncio.create_task(self._send(data, host, port, payload))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _send(self, packet, host, port, payload):
        try:
            host = await self.engine._policy_host(host, port, udp=True)
            if self.engine.config.mode == "socks5":
                if not self.engine.config.allow_private_targets:
                    packet = b"\x00\x00\x00" + _pack_address(host, port) + payload
                self.outbound[self.upstream_family].sendto(packet, self.upstream_relay)
            else:
                addresses = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_DGRAM),
                    self.engine.config.timeout,
                )
                if not addresses:
                    raise ProxyError("无法解析 UDP 目标。")
                family, _, _, _, remote = addresses[0]
                transport = await self._get_outbound(family)
                now = time.monotonic()
                self.remote_allowed = {
                    key: expiry for key, expiry in self.remote_allowed.items() if expiry > now
                }
                if len(self.remote_allowed) >= 512:
                    self.remote_allowed.pop(next(iter(self.remote_allowed)))
                self.remote_allowed[(str(ipaddress.ip_address(remote[0])), remote[1])] = now + 300
                transport.sendto(payload, remote)
            self.engine._bytes_up += len(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.engine._failures += 1

    def _from_remote(self, data, addr):
        if self.closed or self.client_address is None:
            return
        remote = (str(ipaddress.ip_address(addr[0])), addr[1])
        if self.engine.config.mode == "socks5":
            expected = (str(ipaddress.ip_address(self.upstream_relay[0])), self.upstream_relay[1])
            if remote != expected:
                return
            try:
                _, _, payload = _unpack_datagram(data)
            except ProxyError:
                return
            packet = data
        else:
            if self.remote_allowed.get(remote, 0) < time.monotonic():
                return
            payload = data
            packet = b"\x00\x00\x00" + _pack_address(remote[0], remote[1]) + data
        try:
            self.transport.sendto(packet, self.client_address)
            self.engine._bytes_down += len(payload)
        except (OSError, RuntimeError):
            self.engine._failures += 1

    async def close(self):
        self.closed = True
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.transport:
            self.transport.close()
        for transport in self.outbound.values():
            transport.close()
        self.outbound.clear()
        await _close_writer(self.upstream_writer)


class ProxyEngine:
    def __init__(self, config: ProxyConfig, on_event: Callable[[str], None] | None = None, *, on_log=None):
        self.config = config
        self.on_event = on_event or on_log
        self._server = None
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._bound_port = 0
        self._state = "stopped"
        self._bytes_up = 0
        self._bytes_down = 0
        self._total = 0
        self._active = 0
        self._failures = 0
        self._last_error = ""
        self._last_error_time = 0.0

    @property
    def bound_port(self):
        return self._bound_port

    def _log(self, message):
        if self.on_event:
            try:
                self.on_event(message)
            except Exception:
                pass

    def snapshot(self):
        return {
            "state": self._state,
            "listen_host": self.config.listen_host,
            "listen_port": self._bound_port,
            "bytes_up": self._bytes_up,
            "bytes_down": self._bytes_down,
            "active_connections": self._active,
            "total_connections": self._total,
            "failures": self._failures,
            "last_error": self._last_error,
            "udp_supported": self.config.mode in {"direct", "socks5"},
        }

    async def start(self):
        if self._server is not None:
            return
        self.config.validate()
        self._state = "starting"
        try:
            self._server = await asyncio.start_server(
                self._accepted, self.config.listen_host, self.config.listen_port, limit=MAX_HEADER
            )
            self._bound_port = self._server.sockets[0].getsockname()[1]
            self._state = "running"
            self._last_error = ""
            self._log("本机 SOCKS5 监听已启动。")
        except BaseException:
            self._state = "failed"
            raise

    async def stop(self):
        self._state = "stopping"
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for writer in list(self._writers):
            writer.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        self._writers.clear()
        self._tasks.clear()
        self._active = 0
        self._bound_port = 0
        self._state = "stopped"
        self._log("代理已停止，本程序建立的连接已关闭。")

    def _accepted(self, reader, writer):
        if len(self._tasks) >= MAX_CONNECTIONS or self._state != "running":
            writer.close()
            self._failures += 1
            return
        # Register ownership before scheduling the coroutine. stop() can cancel
        # a task before its first instruction, in which case its finally block
        # never runs and must not be the only owner responsible for this socket.
        self._writers.add(writer)
        task = asyncio.create_task(self._client(reader, writer))
        self._tasks.add(task)

        def completed(done):
            self._tasks.discard(done)
            self._writers.discard(writer)
            writer.close()
            if not done.cancelled():
                # Consume an unexpected cleanup exception without logging
                # external protocol data or leaving an unretrieved exception.
                done.exception()

        task.add_done_callback(completed)

    async def _policy_host(self, host, port, udp=False):
        if self.config.allow_private_targets:
            return host
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            choices = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
                ), self.config.timeout,
            )
            addresses = [ipaddress.ip_address(item[4][0]) for item in choices]
        if not addresses or any(not address.is_global for address in addresses):
            raise ProxyError("当前配置不允许访问私有或保留地址。", 2)
        # Pin the checked IP instead of asking an upstream to resolve it again.
        return str(addresses[0])

    async def _connect(self, host, port):
        host = await self._policy_host(host, port)
        if not port:
            raise ProxyError("目标端口无效。", 8)
        if self.config.mode == "direct":
            return await asyncio.wait_for(asyncio.open_connection(host, port), self.config.timeout)
        reader, writer = await _open_upstream(self.config)
        try:
            async def handshake():
                if self.config.mode == "socks5":
                    await _socks_auth(reader, writer, self.config)
                    await _socks_request(reader, writer, 1, host, port)
                else:
                    host_ascii = host.encode("idna").decode("ascii")
                    if any(c in host_ascii for c in "\r\n\x00 /"):
                        raise ProxyError("目标地址无效。", 8)
                    authority = f"[{host_ascii}]:{port}" if ":" in host_ascii else f"{host_ascii}:{port}"
                    lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
                    if self.config.username:
                        credential = f"{self.config.username}:{self.config.password}".encode("utf-8")
                        lines.append("Proxy-Authorization: Basic " + base64.b64encode(credential).decode("ascii"))
                    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
                    await writer.drain()
                    header = await reader.readuntil(b"\r\n\r\n")
                    if len(header) > MAX_HEADER:
                        raise ProxyError("上游 HTTP 回复头过大。")
                    status = header.split(b"\r\n", 1)[0].split(b" ")
                    if len(status) < 2 or status[0] not in {b"HTTP/1.0", b"HTTP/1.1"}:
                        raise ProxyError("上游 HTTP 回复无效。")
                    if status[1] == b"407":
                        raise ProxyError("上游 HTTP 代理认证失败。", 2)
                    if not (len(status[1]) == 3 and status[1].isdigit() and 200 <= int(status[1]) < 300):
                        raise ProxyError("上游 HTTP 代理拒绝 CONNECT。")
            await asyncio.wait_for(handshake(), self.config.timeout)
            return reader, writer
        except BaseException:
            await _close_writer(writer)
            raise

    async def _reply(self, writer, code=0, host="0.0.0.0", port=0):
        writer.write(b"\x05" + bytes([code]) + b"\x00" + _pack_address(host, port))
        await writer.drain()

    async def _client(self, reader, writer):
        self._writers.add(writer)
        self._active += 1
        self._total += 1
        upstream_writer = None
        association = None
        request_received = False
        replied = False
        try:
            async def greeting():
                header = await reader.readexactly(2)
                if header[0] != 5 or not header[1]:
                    raise ProxyError("客户端 SOCKS5 握手无效。")
                methods = await reader.readexactly(header[1])
                if 0 not in methods:
                    writer.write(b"\x05\xff")
                    await writer.drain()
                    raise ProxyError("本机代理仅支持免认证接入。")
                writer.write(b"\x05\x00")
                await writer.drain()
                request = await reader.readexactly(4)
                if request[0] != 5 or request[2] != 0:
                    raise ProxyError("客户端 SOCKS5 请求无效。")
                return request[1], await _read_address(reader, request[3])
            command, (host, port) = await asyncio.wait_for(greeting(), self.config.timeout)
            request_received = True
            if command == 1:
                upstream_reader, upstream_writer = await self._connect(host, port)
                self._writers.add(upstream_writer)
                local = upstream_writer.get_extra_info("sockname")
                await self._reply(writer, host=local[0], port=local[1])
                replied = True
                await self._relay(reader, writer, upstream_reader, upstream_writer)
            elif command == 3:
                if self.config.mode not in {"direct", "socks5"}:
                    raise ProxyError("当前上游模式不支持 UDP。", 7)
                peer = writer.get_extra_info("peername")[0]
                if host not in {"0.0.0.0", "::", peer}:
                    raise ProxyError("UDP 客户端地址必须与本机控制连接一致。", 2)
                association = _UdpAssociation(self, peer, port)
                relay_host, relay_port = await asyncio.wait_for(association.start(), self.config.timeout)
                await self._reply(writer, host=relay_host, port=relay_port)
                replied = True
                monitors = [asyncio.create_task(reader.read(1))]
                if association.upstream_reader:
                    monitors.append(asyncio.create_task(association.upstream_reader.read(1)))
                try:
                    await asyncio.wait(monitors, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in monitors:
                        task.cancel()
                    await asyncio.gather(*monitors, return_exceptions=True)
            else:
                raise ProxyError("不支持此 SOCKS5 命令。", 7)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failures += 1
            if isinstance(exc, ProxyError):
                message = str(exc)
            elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                message = "代理请求超时。"
            elif isinstance(exc, ConnectionRefusedError):
                message = "连接被拒绝，请检查服务器及端口。"
            elif isinstance(exc, ssl.SSLCertVerificationError):
                message = "上游 TLS 证书验证失败。"
            else:
                message = "代理请求未完成，请检查网络或客户端协议。"
            self._last_error = message
            now = time.monotonic()
            if now - self._last_error_time >= 2.0:
                self._log(message)
                self._last_error_time = now
            if request_received and not replied:
                code = exc.reply if isinstance(exc, ProxyError) else (
                    5 if isinstance(exc, ConnectionRefusedError) else
                    6 if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else 1
                )
                try:
                    await self._reply(writer, code)
                except (Exception, asyncio.CancelledError):
                    pass
        finally:
            if association:
                await association.close()
            if upstream_writer:
                self._writers.discard(upstream_writer)
                await _close_writer(upstream_writer)
            self._writers.discard(writer)
            await _close_writer(writer)
            self._active -= 1

    async def _relay(self, client_reader, client_writer, remote_reader, remote_writer):
        async def pump(reader, writer, upload):
            while True:
                data = await reader.read(65536)
                if not data:
                    if writer.can_write_eof():
                        try:
                            writer.write_eof()
                            await writer.drain()
                        except (OSError, RuntimeError):
                            pass
                    else:
                        # TLS cannot represent a TCP half-close. Close its
                        # transport so the other relay task cannot leak forever.
                        writer.close()
                    return
                writer.write(data)
                await writer.drain()
                if upload:
                    self._bytes_up += len(data)
                else:
                    self._bytes_down += len(data)
        tasks = [
            asyncio.create_task(pump(client_reader, remote_writer, True)),
            asyncio.create_task(pump(remote_reader, client_writer, False)),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
