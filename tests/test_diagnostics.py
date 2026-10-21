"""Real loopback checks, including TLS data through the actual proxy listener."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import socket
import ssl
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(SOURCE))
from diagnostics import check_route_tls, verify_listener, validate_route_target
from proxy_engine import ProxyConfig, ProxyEngine


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engines = []
        self.servers = []
        self.tasks = set()
        self.errors = []
        self.tempdirs = []

    async def asyncTearDown(self):
        for engine in self.engines:
            await asyncio.wait_for(engine.stop(), 3)
        for server in self.servers:
            server.close()
            await server.wait_closed()
        remaining = list(self.tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        for folder in self.tempdirs:
            folder.cleanup()
        self.assertEqual(self.errors, [])

    async def server(self, handler, context=None):
        def accepted(reader, writer):
            async def run():
                try:
                    await handler(reader, writer)
                except (ConnectionError, asyncio.IncompleteReadError):
                    pass
                except Exception as exc:
                    self.errors.append(type(exc).__name__)
                finally:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(writer.wait_closed(), 0.5)
            task = asyncio.create_task(run())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        server = await asyncio.start_server(accepted, "127.0.0.1", 0, ssl=context)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def engine(self, **options):
        config = {"listen_port": 0, "timeout": 1}
        config.update(options)
        engine = ProxyEngine(ProxyConfig(**config))
        self.engines.append(engine)
        await engine.start()
        return engine

    async def drained(self, engine):
        async with asyncio.timeout(2):
            while engine.snapshot()["active_connections"]:
                await asyncio.sleep(0.01)

    def tls_contexts(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        folder = tempfile.TemporaryDirectory(prefix="diagnostic-tls-", dir=Path(__file__).parent)
        self.tempdirs.append(folder)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local diagnostic fixture")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = Path(folder.name) / "cert.pem", Path(folder.name) / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(cert_path, key_path)
        client = ssl.create_default_context()
        client.load_verify_locations(cafile=cert_path)
        self.assertTrue(client.check_hostname)
        self.assertEqual(client.verify_mode, ssl.CERT_REQUIRED)
        return server, client

    async def test_listener_negotiates_real_socks_without_recording_false_failure(self):
        engine = await self.engine()
        result = await verify_listener("127.0.0.1", engine.bound_port)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "listener")
        await self.drained(engine)
        self.assertEqual(engine.snapshot()["failures"], 0)
        self.assertEqual(set(result), {"ok", "stage", "message", "latency_ms"})

    async def test_tcp_listener_is_not_enough(self):
        async def wrong_protocol(reader, writer):
            await reader.readexactly(3)
            writer.write(b"OK")
            await writer.drain()
        port = await self.server(wrong_protocol)
        result = await verify_listener("127.0.0.1", port)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "listener")

    async def test_listener_timeout_and_cancel_close_the_connection(self):
        closed = asyncio.Event()
        async def silent(reader, writer):
            await reader.readexactly(3)
            self.assertEqual(await reader.read(), b"")
            closed.set()
        port = await self.server(silent)
        started = time.monotonic()
        result = await verify_listener("127.0.0.1", port, timeout=0.1)
        self.assertFalse(result["ok"])
        self.assertLess(time.monotonic() - started, 1)
        await asyncio.wait_for(closed.wait(), 1)
        closed.clear()
        task = asyncio.create_task(verify_listener("127.0.0.1", port, timeout=30))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), 1)

    async def test_invalid_addresses_do_not_connect_or_resolve(self):
        with patch("diagnostics.asyncio.open_connection") as connect:
            for host in ("192.0.2.1", "example.com", "0.0.0.0", "::", "localhost:99"):
                result = await verify_listener(host, 10808)
                self.assertFalse(result["ok"])
            for host in ("", "https://example.com", "user:secret@example.com", "x\r\ny"):
                result = await check_route_tls("127.0.0.1", 10808, host)
                self.assertFalse(result["ok"])
                self.assertEqual(result["stage"], "configuration")
                self.assertNotIn("secret", repr(result))
            for port in (True, "443", 0, 65536):
                result = await check_route_tls("127.0.0.1", 10808, "localhost", port)
                self.assertFalse(result["ok"])
            connect.assert_not_called()

    async def test_trusted_tls_uses_actual_listener_and_sends_no_application_payload(self):
        server_context, client_context = self.tls_contexts()
        received = []
        async def target(reader, writer):
            received.append(await reader.read())
        port = await self.server(target, server_context)
        engine = await self.engine()
        with patch("diagnostics.ssl.create_default_context", return_value=client_context):
            result = await check_route_tls("127.0.0.1", engine.bound_port, "127.0.0.1", port, 2)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "tls")
        await self.drained(engine)
        self.assertEqual(received, [b""])
        self.assertGreater(engine.snapshot()["bytes_up"], 0)
        self.assertGreater(engine.snapshot()["bytes_down"], 0)
        self.assertNotIn("127.0.0.1", repr(result))

    async def test_route_follows_configured_socks_upstream(self):
        server_context, client_context = self.tls_contexts()
        async def target(reader, writer):
            await reader.read()
        port = await self.server(target, server_context)
        upstream = await self.engine()
        local = await self.engine(mode="socks5", upstream_host="127.0.0.1",
                                  upstream_port=upstream.bound_port)
        with patch("diagnostics.ssl.create_default_context", return_value=client_context):
            result = await check_route_tls("127.0.0.1", local.bound_port, "127.0.0.1", port, 2)
        self.assertTrue(result["ok"], result)
        self.assertGreater(upstream.snapshot()["bytes_up"], 0)
        self.assertGreater(upstream.snapshot()["bytes_down"], 0)

    async def test_untrusted_certificate_is_not_reported_as_route_success(self):
        server_context, _ = self.tls_contexts()
        async def target(reader, writer):
            await reader.read()
        port = await self.server(target, server_context)
        local = await self.engine()
        result = await check_route_tls("127.0.0.1", local.bound_port, "127.0.0.1", port, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "tls")

    async def test_hostname_mismatch_is_rejected(self):
        server_context, client_context = self.tls_contexts()
        async def target(reader, writer):
            await reader.read()
        port = await self.server(target, server_context)
        local = await self.engine()
        with patch("diagnostics.ssl.create_default_context", return_value=client_context):
            result = await check_route_tls("127.0.0.1", local.bound_port, "localhost", port, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "tls")

    async def test_connect_success_without_real_tls_does_not_pass(self):
        closed = asyncio.Event()
        async def fake_proxy(reader, writer):
            self.assertEqual(await reader.readexactly(3), b"\x05\x01\x00")
            writer.write(b"\x05\x00")
            await writer.drain()
            self.assertEqual(await reader.readexactly(4), b"\x05\x01\x00\x01")
            await reader.readexactly(6)
            writer.write(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", 1))
            await writer.drain()
            while await reader.read(4096):
                pass
            closed.set()
        port = await self.server(fake_proxy)
        started = time.monotonic()
        result = await check_route_tls("127.0.0.1", port, "127.0.0.1", 443, timeout=0.2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "tls")
        self.assertLess(time.monotonic() - started, 1)
        await asyncio.wait_for(closed.wait(), 1)

    async def test_target_tunnel_failure_is_distinct_from_tls_failure(self):
        local = await self.engine(allow_private_targets=False)
        result = await check_route_tls("127.0.0.1", local.bound_port, "127.0.0.1", 443, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "tunnel")

    async def test_deadline_covers_all_stages_together(self):
        async def slow_proxy(reader, writer):
            await reader.readexactly(3)
            await asyncio.sleep(0.15)
            writer.write(b"\x05\x00")
            await writer.drain()
            await reader.readexactly(10)
            await asyncio.sleep(0.15)
            writer.write(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", 1))
            await writer.drain()
        port = await self.server(slow_proxy)
        result = await check_route_tls("127.0.0.1", port, "127.0.0.1", 443, timeout=0.25)
        self.assertFalse(result["ok"])
        # Separate per-stage timeouts would reach TLS after 300 ms. The shared
        # deadline expires while still waiting for the tunnel response.
        self.assertEqual(result["stage"], "tunnel")
        self.assertLess(result["latency_ms"], 500)

    async def test_invalid_socks_reply_is_sanitized(self):
        async def bad_reply(reader, writer):
            await reader.readexactly(3)
            writer.write(b"\x05\x00")
            await writer.drain()
            await reader.read(512)
            writer.write(b"\x05\x00\x00\x7f" + b"fixture-sensitive-message")
            await writer.drain()
        port = await self.server(bad_reply)
        result = await check_route_tls("127.0.0.1", port, "localhost", 443, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "tunnel")
        self.assertNotIn("fixture-sensitive", repr(result))

    async def test_partial_request_after_greeting_still_counts_as_failure(self):
        local = await self.engine()
        reader, writer = await asyncio.open_connection("127.0.0.1", local.bound_port)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        self.assertEqual(await reader.readexactly(2), b"\x05\x00")
        writer.write(b"\x05")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await self.drained(local)
        self.assertEqual(local.snapshot()["failures"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
