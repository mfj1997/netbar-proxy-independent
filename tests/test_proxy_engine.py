"""Integration tests use loopback-only servers, never the original executable."""
from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import socket
import ssl
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(SOURCE))
from proxy_engine import ProxyConfig, ProxyEngine, check_upstream


def address(host: str, port: int) -> bytes:
    try:
        parsed = ipaddress.ip_address(host)
        return bytes([1 if parsed.version == 4 else 4]) + parsed.packed + struct.pack("!H", port)
    except ValueError:
        encoded = host.encode("idna")
        return b"\x03" + bytes([len(encoded)]) + encoded + struct.pack("!H", port)


async def read_address(reader, atyp):
    if atyp == 1:
        host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
    elif atyp == 3:
        size = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(size)).decode("ascii")
    else:
        raise AssertionError(f"Unsupported address type: {atyp}")
    port = struct.unpack("!H", await reader.readexactly(2))[0]
    return host, port


async def close_writer(writer):
    writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), 1)


async def pipe(reader, writer):
    try:
        while block := await reader.read(65536):
            writer.write(block)
            await writer.drain()
    finally:
        with contextlib.suppress(Exception):
            writer.write_eof()


class DatagramEcho(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.transport.sendto(data, addr)


class DatagramInbox(asyncio.DatagramProtocol):
    def __init__(self):
        self.queue = asyncio.Queue()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.queue.put_nowait((data, addr))


class ProxyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.engines = []
        self.writers = []
        self.transports = []
        self.tasks = set()
        self.logs = []
        self.fixture_errors = []
        self.tempdirs = []

    async def asyncTearDown(self):
        for engine in self.engines:
            await asyncio.wait_for(engine.stop(), 4)
        for writer in self.writers:
            await close_writer(writer)
        for transport in self.transports:
            transport.close()
        for server in self.servers:
            server.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for server in self.servers:
            await asyncio.wait_for(server.wait_closed(), 3)
        for directory in self.tempdirs:
            directory.cleanup()
        if self.fixture_errors:
            self.fail(f"Local fixture failed: {self.fixture_errors!r}")

    async def server(self, handler, ssl_context=None):
        def track(reader, writer):
            async def wrapped():
                try:
                    await handler(reader, writer)
                except (ConnectionError, asyncio.IncompleteReadError):
                    pass
                except Exception as exc:
                    self.fixture_errors.append(exc)
                finally:
                    await close_writer(writer)
            task = asyncio.create_task(wrapped())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        result = await asyncio.start_server(track, "127.0.0.1", 0, ssl=ssl_context)
        self.servers.append(result)
        return result.sockets[0].getsockname()[1]

    async def echo(self, reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def engine(self, **overrides):
        settings = {"listen_port": 0, "timeout": 0.8}
        settings.update(overrides)
        engine = ProxyEngine(ProxyConfig(**settings), on_event=self.logs.append)
        self.engines.append(engine)
        await engine.start()
        return engine

    async def connect(self, engine, host, port, command=1):
        reader, writer = await asyncio.open_connection("127.0.0.1", engine.bound_port)
        self.writers.append(writer)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(2), 2), b"\x05\x00")
        writer.write(bytes([5, command, 0]) + address(host, port))
        await writer.drain()
        version, result, reserved, atyp = await asyncio.wait_for(reader.readexactly(4), 2)
        self.assertEqual((version, reserved), (5, 0))
        bind = await asyncio.wait_for(read_address(reader, atyp), 2)
        return reader, writer, result, bind

    async def assert_echo(self, engine, host="127.0.0.1"):
        port = await self.server(self.echo)
        reader, writer, result, _ = await self.connect(engine, host, port)
        self.assertEqual(result, 0)
        data = b"hello-independent-proxy\x00" * 1000
        writer.write(data)
        await writer.drain()
        actual = await asyncio.wait_for(reader.readexactly(len(data)), 3)
        self.assertEqual(actual, data)
        return reader, writer, len(data)

    async def test_direct_tcp_and_traffic_counters(self):
        engine = await self.engine()
        _, _, count = await self.assert_echo(engine)
        await asyncio.sleep(0.03)
        stats = engine.snapshot()
        self.assertGreaterEqual(stats["bytes_up"], count)
        self.assertGreaterEqual(stats["bytes_down"], count)
        self.assertGreaterEqual(stats["total_connections"], 1)
        self.assertGreaterEqual(stats["active_connections"], 1)

    async def test_domain_address_request(self):
        engine = await self.engine()
        await self.assert_echo(engine, "localhost")

    async def test_udp_direct_relay(self):
        loop = asyncio.get_running_loop()
        remote, _ = await loop.create_datagram_endpoint(DatagramEcho, local_addr=("127.0.0.1", 0))
        self.transports.append(remote)
        destination_port = remote.get_extra_info("sockname")[1]
        engine = await self.engine()
        _, _, result, bind = await self.connect(engine, "0.0.0.0", 0, command=3)
        self.assertEqual(result, 0)
        local, inbox = await loop.create_datagram_endpoint(DatagramInbox, local_addr=("127.0.0.1", 0))
        self.transports.append(local)
        payload = b"udp-real-loopback-payload"
        local.sendto(b"\x00\x00\x00" + address("127.0.0.1", destination_port) + payload, bind)
        answer, _ = await asyncio.wait_for(inbox.queue.get(), 3)
        self.assertEqual(answer[:3], b"\x00\x00\x00")
        self.assertEqual(answer[-len(payload):], payload)

    async def test_udp_socks_upstream_relay(self):
        loop = asyncio.get_running_loop()
        remote, _ = await loop.create_datagram_endpoint(DatagramEcho, local_addr=("127.0.0.1", 0))
        self.transports.append(remote)
        destination_port = remote.get_extra_info("sockname")[1]
        upstream = await self.engine()
        engine = await self.engine(mode="socks5", upstream_host="127.0.0.1",
                                   upstream_port=upstream.bound_port)
        _, _, result, bind = await self.connect(engine, "0.0.0.0", 0, command=3)
        self.assertEqual(result, 0)
        local, inbox = await loop.create_datagram_endpoint(DatagramInbox, local_addr=("127.0.0.1", 0))
        self.transports.append(local)
        payload = b"udp-through-two-proxies"
        local.sendto(b"\x00\x00\x00" + address("127.0.0.1", destination_port) + payload, bind)
        answer, _ = await asyncio.wait_for(inbox.queue.get(), 3)
        self.assertEqual(answer[:3], b"\x00\x00\x00")
        self.assertEqual(answer[-len(payload):], payload)

    async def socks_upstream(self, username=b"local-fixture", password=b"fixture-secret", reject=False):
        observed = []

        async def handler(reader, writer):
            version, count = await reader.readexactly(2)
            self.assertEqual(version, 5)
            methods = await reader.readexactly(count)
            self.assertIn(2, methods)
            writer.write(b"\x05\x02")
            await writer.drain()
            ver, length = await reader.readexactly(2)
            user = await reader.readexactly(length)
            plen = (await reader.readexactly(1))[0]
            secret = await reader.readexactly(plen)
            accepted = (ver, user, secret) == (1, username, password) and not reject
            observed.append(accepted)
            writer.write(b"\x01" + bytes([0 if accepted else 1]))
            await writer.drain()
            if not accepted:
                return
            try:
                ver, cmd, rsv, atyp = await reader.readexactly(4)
            except asyncio.IncompleteReadError:
                return  # Diagnostic probe intentionally only negotiates auth.
            self.assertEqual((ver, cmd, rsv), (5, 1, 0))
            target, port = await read_address(reader, atyp)
            self.assertIn(target, ("127.0.0.1", "localhost"))
            rr, rw = await asyncio.open_connection(target, port)
            try:
                writer.write(b"\x05\x00\x00" + address("127.0.0.1", port))
                await writer.drain()
                await asyncio.gather(pipe(reader, rw), pipe(rr, writer))
            finally:
                await close_writer(rw)
        return await self.server(handler), observed

    async def test_socks_upstream_auth_and_payload(self):
        port, observed = await self.socks_upstream()
        engine = await self.engine(mode="socks5", upstream_host="127.0.0.1", upstream_port=port,
                                   username="local-fixture", password="fixture-secret")
        await self.assert_echo(engine)
        self.assertEqual(observed, [True])
        exposed = repr(engine.snapshot()) + repr(self.logs)
        self.assertNotIn("fixture-secret", exposed)
        self.assertNotIn("local-fixture", exposed)

    async def test_socks_upstream_bad_auth_reports_failure(self):
        port, observed = await self.socks_upstream(reject=True)
        engine = await self.engine(mode="socks5", upstream_host="127.0.0.1", upstream_port=port,
                                   username="local-fixture", password="fixture-secret")
        _, _, result, _ = await self.connect(engine, "127.0.0.1", 9)
        self.assertNotEqual(result, 0)
        self.assertEqual(observed, [False])

    async def test_socks_diagnostic_is_auth_only(self):
        port, observed = await self.socks_upstream()
        result = await check_upstream(ProxyConfig(mode="socks5", upstream_host="127.0.0.1",
                                                upstream_port=port, username="local-fixture",
                                                password="fixture-secret", timeout=0.8))
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "authentication")
        self.assertEqual(observed, [True])
        self.assertNotIn("fixture-secret", repr(result))

    async def http_upstream(self, *, reject=False, ssl_context=None):
        observed = []

        async def handler(reader, writer):
            try:
                raw = await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError:
                return  # Transport-only diagnostic.
            text = raw.decode("ascii")
            observed.append(text)
            required = base64.b64encode(b"fixture-user:fixture-password").decode("ascii")
            if reject or f"Proxy-Authorization: Basic {required}".lower() not in text.lower():
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            method, target, protocol = text.splitlines()[0].split()
            self.assertEqual((method, protocol), ("CONNECT", "HTTP/1.1"))
            host, port = target.rsplit(":", 1)
            self.assertEqual(host, "127.0.0.1")
            rr, rw = await asyncio.open_connection(host, int(port))
            try:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(pipe(reader, rw), pipe(rr, writer))
            finally:
                await close_writer(rw)
        return await self.server(handler, ssl_context=ssl_context), observed

    def local_tls_context(self):
        """Trust is added only to this test context, never to Windows."""
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
        except ImportError:
            self.skipTest("cryptography is required only for local TLS integration tests")
        directory = tempfile.TemporaryDirectory(prefix="tls-fixture-", dir=Path(__file__).parent)
        self.tempdirs.append(directory)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "loopback test only")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(subject).issuer_name(subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
                ]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path = Path(directory.name) / "fixture.crt"
        key_path = Path(directory.name) / "fixture.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        return server_context, cert_path

    async def test_http_connect_auth_and_payload(self):
        port, observed = await self.http_upstream()
        engine = await self.engine(mode="http", upstream_host="127.0.0.1", upstream_port=port,
                                   username="fixture-user", password="fixture-password")
        await self.assert_echo(engine)
        self.assertEqual(len(observed), 1)
        self.assertNotIn("fixture-password", repr(engine.snapshot()) + repr(self.logs))

    async def test_http_connect_rejection(self):
        port, _ = await self.http_upstream(reject=True)
        engine = await self.engine(mode="http", upstream_host="127.0.0.1", upstream_port=port,
                                   username="fixture-user", password="fixture-password")
        _, _, result, _ = await self.connect(engine, "127.0.0.1", 9)
        self.assertNotEqual(result, 0)

    async def test_https_rejects_untrusted_certificate(self):
        context, _ = self.local_tls_context()
        port, _ = await self.http_upstream(ssl_context=context)
        result = await check_upstream(ProxyConfig(mode="https", upstream_host="127.0.0.1",
                                                upstream_port=port, timeout=2))
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "tls")

    async def test_https_connect_with_test_ca_and_hostname_verification(self):
        context, cert_path = self.local_tls_context()
        port, observed = await self.http_upstream(ssl_context=context)
        original = ssl.create_default_context
        def test_context(*args, **kwargs):
            ctx = original(*args, **kwargs)
            ctx.load_verify_locations(cafile=cert_path)
            self.assertTrue(ctx.check_hostname)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            return ctx
        with patch("proxy_engine.ssl.create_default_context", side_effect=test_context):
            engine = await self.engine(mode="https", upstream_host="127.0.0.1",
                                       upstream_port=port, username="fixture-user",
                                       password="fixture-password", timeout=2)
            _, writer, _ = await self.assert_echo(engine)
            self.assertEqual(len(observed), 1)
            # Client EOF must not leave the TLS upstream and peer pump stranded.
            await close_writer(writer)
            for _ in range(100):
                if engine.snapshot()["active_connections"] == 0:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(engine.snapshot()["active_connections"], 0)
            # TLS streams do not support half-close; stop still needs to finish.
            await asyncio.wait_for(engine.stop(), 4)

    async def test_http_diagnostic_does_not_claim_authentication(self):
        port, observed = await self.http_upstream()
        result = await check_upstream(ProxyConfig(mode="http", upstream_host="127.0.0.1",
                                                upstream_port=port, timeout=0.8))
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "transport")
        self.assertEqual(observed, [])

    async def test_http_mode_rejects_udp(self):
        port, _ = await self.http_upstream()
        engine = await self.engine(mode="http", upstream_host="127.0.0.1", upstream_port=port)
        _, _, result, _ = await self.connect(engine, "0.0.0.0", 0, command=3)
        self.assertEqual(result, 7)
        self.assertFalse(engine.snapshot()["udp_supported"])

    async def test_unsupported_bind_command(self):
        engine = await self.engine()
        _, _, result, _ = await self.connect(engine, "127.0.0.1", 9, command=2)
        self.assertEqual(result, 7)

    async def test_rejects_no_acceptable_auth_method(self):
        engine = await self.engine()
        reader, writer = await asyncio.open_connection("127.0.0.1", engine.bound_port)
        self.writers.append(writer)
        writer.write(b"\x05\x01\x02")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(2), 2), b"\x05\xff")

    async def test_incomplete_handshake_times_out(self):
        engine = await self.engine(timeout=0.2)
        reader, writer = await asyncio.open_connection("127.0.0.1", engine.bound_port)
        self.writers.append(writer)
        writer.write(b"\x05")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")

    async def test_port_conflict_fails_instead_of_claiming_running(self):
        taken = await self.server(self.echo)
        engine = ProxyEngine(ProxyConfig(listen_port=taken))
        self.engines.append(engine)
        with self.assertRaises(OSError):
            await engine.start()
        self.assertNotEqual(engine.snapshot()["state"], "running")

    async def test_non_loopback_listener_rejected(self):
        for host in ("0.0.0.0", "::", "192.0.2.1"):
            engine = ProxyEngine(ProxyConfig(listen_host=host, listen_port=0))
            self.engines.append(engine)
            with self.assertRaises(ValueError):
                await engine.start()

    async def test_stop_closes_sessions_and_listener(self):
        engine = await self.engine()
        reader, _, _ = await self.assert_echo(engine)
        port = engine.bound_port
        await engine.stop()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
        self.assertEqual(engine.snapshot()["active_connections"], 0)

    async def test_stop_owns_accepted_writer_before_client_task_first_runs(self):
        """Deterministically cancel the client task before its first instruction."""
        loop = asyncio.get_running_loop()
        accepted_pair = loop.create_future()
        release_fixture = asyncio.Event()

        async def handoff(reader, writer):
            accepted_pair.set_result((reader, writer))
            await release_fixture.wait()

        fixture_port = await self.server(handoff)
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", fixture_port)
        self.writers.append(client_writer)
        peer_reader, peer_writer = await accepted_pair
        engine = await self.engine()
        try:
            # No await between accepting and entering stop: _client's newly
            # created task has not had an opportunity to register its writer.
            engine._accepted(peer_reader, peer_writer)
            async with asyncio.timeout(2):
                await engine.stop()
            self.assertEqual(await asyncio.wait_for(client_reader.read(1), 1), b"")
            self.assertEqual(engine.snapshot()["active_connections"], 0)
        finally:
            release_fixture.set()
            await close_writer(peer_writer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
