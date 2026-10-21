"""Configuration persistence and background-service lifecycle integration."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(SOURCE))
from proxy_engine import ProxyConfig
from service import ProxyService
from settings import load_settings, save_settings, validate_config


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="settings-", dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"

    def test_round_trip_persists_preferences_without_authentication(self):
        original = ProxyConfig(mode="socks5", upstream_host="localhost", upstream_port=1080,
                               username="private-fixture-user", password="private-fixture-password")
        save_settings(original, self.path)
        raw = self.path.read_text("utf-8")
        self.assertNotIn("private-fixture", raw)
        self.assertNotIn("username", raw)
        self.assertNotIn("password", raw)
        loaded = load_settings(self.path)
        self.assertEqual(loaded.mode, "socks5")
        self.assertEqual(loaded.upstream_host, "localhost")
        self.assertEqual((loaded.username, loaded.password), ("", ""))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_missing_file_uses_loopback_defaults(self):
        config = load_settings(self.path)
        self.assertEqual(config.listen_host, "127.0.0.1")
        self.assertEqual(config.mode, "direct")
        self.assertFalse(self.path.exists())

    def test_rejects_upstream_pointing_at_own_listener(self):
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_config(ProxyConfig(mode="socks5", upstream_host=host, upstream_port=10808))
        separate = validate_config(ProxyConfig(mode="socks5", upstream_host="127.0.0.2", upstream_port=10808))
        self.assertEqual(separate.upstream_host, "127.0.0.2")
        separate_v6 = validate_config(ProxyConfig(listen_host="::1", mode="socks5", upstream_host="127.0.0.1", upstream_port=10808))
        self.assertEqual(separate_v6.listen_host, "::1")

    def test_rejects_invalid_schema_and_secret_fields(self):
        bad_values = [
            [], {"schema_version": True}, {"schema_version": "1"},
            {"schema_version": 2},
            {"schema_version": 1, "password": "fixture"},
            {"schema_version": 1, "username": "fixture"},
            {"schema_version": 1, "unknown": 1},
        ]
        for value in bad_values:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_settings(self.path)

    def test_strict_ports_booleans_and_timeouts(self):
        original = ProxyConfig()
        bad_values = [
            ("listen_port", True), ("listen_port", "10808"), ("listen_port", 0),
            ("listen_port", 65536), ("upstream_port", False),
            ("allow_private_targets", "false"), ("allow_private_targets", 1),
            ("timeout", float("nan")), ("timeout", float("inf")),
            ("timeout", False), ("timeout", 0.5),
        ]
        for name, value in bad_values:
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError):
                    validate_config(replace(original, **{name: value}))

    def test_rejects_non_loopback_and_malformed_upstream_hosts(self):
        for host in ("0.0.0.0", "::", "192.0.2.1"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_config(ProxyConfig(listen_host=host))
        for host in ("https://localhost", "localhost:80", "host/name", "host\r\nbad", "999.0.0.1"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_config(ProxyConfig(mode="http", upstream_host=host))

    def test_malformed_json_and_oversized_file_are_errors(self):
        for value in ("not json", "\x00", " " * 17000):
            self.path.write_text(value, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(self.path)


class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            while data := self.request.recv(65536):
                self.request.sendall(data)
        except (OSError, TimeoutError):
            pass


class _ThreadingServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = ProxyService()
        self.addCleanup(self.service.shutdown, 3)

    def wait_for(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"Condition did not complete; state={self.service.snapshot()}")

    def test_start_stop_race_and_restart(self):
        for _ in range(4):
            config = ProxyConfig(listen_port=unused_port(), timeout=1)
            self.assertTrue(self.service.start(config))
            self.service.stop()
            self.assertTrue(self.service.shutdown(3))
            self.assertEqual(self.service.state, "stopped")
        self.assertTrue(self.service.start(ProxyConfig(listen_port=unused_port(), timeout=1)))
        self.wait_for(lambda: self.service.state == "running")
        self.assertFalse(self.service.start(ProxyConfig(listen_port=unused_port(), timeout=1)))
        self.assertTrue(self.service.shutdown(3))
        self.assertEqual(self.service.state, "stopped")

    def test_running_service_transfers_and_shutdown_closes_only_its_session(self):
        remote = _ThreadingServer(("127.0.0.1", 0), _Echo)
        thread = threading.Thread(target=remote.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(remote.server_close)
        self.addCleanup(remote.shutdown)
        port = unused_port()
        self.service.start(ProxyConfig(listen_port=port, timeout=1))
        self.wait_for(lambda: self.service.state == "running")
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"\x05\x01\x00")
            self.assertEqual(sock.recv(2), b"\x05\x00")
            sock.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1")
                         + struct.pack("!H", remote.server_address[1]))
            reply = b""
            while len(reply) < 10:
                reply += sock.recv(10 - len(reply))
            self.assertEqual(reply[:2], b"\x05\x00")
            sock.sendall(b"service-round-trip")
            self.assertEqual(sock.recv(1024), b"service-round-trip")
            self.assertTrue(self.service.shutdown(3))
            self.assertEqual(sock.recv(1024), b"")
        # The independent echo fixture remains available.
        with socket.create_connection(remote.server_address, timeout=2) as independent:
            independent.sendall(b"still-running")
            self.assertEqual(independent.recv(1024), b"still-running")
        self.assertEqual(self.service.snapshot()["active_connections"], 0)

    def test_busy_port_reports_failed(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            self.service.start(ProxyConfig(listen_port=occupied.getsockname()[1], timeout=1))
            self.wait_for(lambda: self.service.state == "failed")
            self.assertTrue(self.service.snapshot()["error"])

    def test_invalid_configuration_does_not_start(self):
        with self.assertRaises(ValueError):
            self.service.start(ProxyConfig(listen_host="0.0.0.0"))
        self.assertEqual(self.service.state, "stopped")

    def test_nonblocking_diagnostic_result_consumed_once(self):
        self.assertTrue(self.service.check_upstream_async(ProxyConfig()))
        result = None
        end = time.monotonic() + 3
        while time.monotonic() < end:
            result = self.service.poll_check_result()
            if result is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "configuration")
        self.assertIsNone(self.service.poll_check_result())


if __name__ == "__main__":
    unittest.main(verbosity=2)
