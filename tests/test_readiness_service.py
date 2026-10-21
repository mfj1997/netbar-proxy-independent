"""Readiness, cancellation and diagnostic ownership regression tests.

All sockets are loopback fixtures. External probes are replaced with controlled
coroutines so no test contacts an upstream or a public destination.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(SOURCE))
from proxy_engine import ProxyConfig
from service import ProxyService


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def check_result(ok=True, stage="listener", message="Fixture check completed."):
    return {"ok": ok, "stage": stage, "message": message, "latency_ms": 1.0}


class ReadinessServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = ProxyService()
        self.addCleanup(self.service.shutdown, 3)

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Service did not reach the expected state before the deadline.")

    def start_direct(self):
        config = ProxyConfig(listen_port=unused_port(), timeout=1)
        self.assertTrue(self.service.start(config))
        self.wait_for(lambda: self.service.state == "running")
        return config

    def test_local_listener_must_pass_handshake_before_running(self):
        entered = threading.Event()
        release = threading.Event()

        async def delayed_probe(*args, **kwargs):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return check_result()

        with patch("service.verify_listener", side_effect=delayed_probe):
            self.service.start(ProxyConfig(listen_port=unused_port(), timeout=1))
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.service.state, "starting")
            self.assertIsNot(self.service.snapshot()["listener_check"]["ok"], True)
            release.set()
            self.wait_for(lambda: self.service.state == "running")
            self.assertIs(self.service.snapshot()["listener_check"]["ok"], True)

    def test_failed_local_handshake_releases_owned_port(self):
        port = unused_port()

        async def reject_probe(*args, **kwargs):
            return check_result(False, "listener", "Fixture listener rejected SOCKS5.")

        with patch("service.verify_listener", side_effect=reject_probe):
            self.service.start(ProxyConfig(listen_port=port, timeout=1))
            self.wait_for(lambda: self.service.state == "failed")
            self.assertIs(self.service.snapshot()["listener_check"]["ok"], False)
            self.assertTrue(self.service.shutdown(2))
        with socket.socket() as replacement:
            replacement.bind(("127.0.0.1", port))
            replacement.listen()
        self.assertEqual(self.service.snapshot()["active_connections"], 0)

    def test_stopped_service_does_not_start_a_route_probe(self):
        with patch("service.check_route_tls") as probe:
            self.assertFalse(self.service.check_route_async("fixture.invalid", 443))
            self.assertFalse(self.service.route_checking)
            probe.assert_not_called()

    def test_real_readiness_does_not_claim_remote_or_entitlement_success(self):
        self.start_direct()
        self.wait_for(lambda: self.service.snapshot()["active_connections"] == 0)
        snapshot = self.service.snapshot()
        self.assertIs(snapshot["listener_check"]["ok"], True)
        self.assertIsNot(snapshot["route_check"]["ok"], True)
        self.assertEqual(snapshot["entitlement"], "unverified")
        self.assertEqual(snapshot["failures"], 0)

    def test_completed_route_result_is_consumed_once(self):
        config = self.start_direct()

        async def successful_route(*args, **kwargs):
            return check_result(True, "tls", "Fixture destination TLS verified.")

        with patch("service.check_route_tls", side_effect=successful_route) as probe:
            self.assertTrue(self.service.check_route_async("fixture.invalid", 443))
            self.wait_for(lambda: self.service.snapshot()["route_check"]["ok"] is True)
            result = self.service.poll_route_result()
            self.assertIsNotNone(result)
            self.assertEqual(result["stage"], "tls")
            self.assertIsNone(self.service.poll_route_result())
            # The service must test its actual bound listener, not a GUI draft.
            arguments = probe.call_args
            all_arguments = list(arguments.args) + list(arguments.kwargs.values())
            self.assertIn(config.listen_port, all_arguments)
            self.assertIn("fixture.invalid", all_arguments)
        self.assertEqual(self.service.snapshot()["entitlement"], "unverified")

    def test_stale_route_completion_cannot_green_a_stopped_or_restarted_run(self):
        self.start_direct()
        entered = threading.Event()
        cancelled = threading.Event()

        async def old_route(*args, **kwargs):
            entered.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                # Simulate a result racing with cancellation.
                return check_result(True, "tls", "OLD-ROUTE-RESULT")

        with patch("service.check_route_tls", side_effect=old_route):
            self.assertTrue(self.service.check_route_async("old-fixture.invalid", 443))
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.service.shutdown(2))
            self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.service.poll_route_result())
        self.assertIsNot(self.service.snapshot()["route_check"]["ok"], True)
        self.start_direct()
        self.assertNotIn("OLD-ROUTE-RESULT", json.dumps(self.service.snapshot()))
        self.assertIsNot(self.service.snapshot()["route_check"]["ok"], True)

    def test_configuration_invalidation_discards_racing_upstream_result(self):
        entered = threading.Event()
        cancelled = threading.Event()
        workers = []

        async def old_check(*args, **kwargs):
            workers.append(threading.current_thread())
            entered.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                return check_result(True, "authentication", "OLD-CONFIG-RESULT")

        with patch("service.check_upstream", side_effect=old_check):
            config = ProxyConfig(mode="socks5", upstream_host="old-fixture.invalid")
            self.assertTrue(self.service.check_upstream_async(config))
            self.assertTrue(entered.wait(2))
            self.service.invalidate_diagnostics()
            workers[0].join(2)
            self.assertFalse(workers[0].is_alive())
            self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.service.poll_check_result())
        self.assertNotIn("OLD-CONFIG-RESULT", json.dumps(self.service.snapshot()))
        self.assertIsNot(self.service.snapshot()["upstream_check"]["ok"], True)

    def test_destination_edit_invalidates_only_its_route_result(self):
        self.start_direct()
        entered = threading.Event()
        workers = []

        async def old_route(*args, **kwargs):
            workers.append(threading.current_thread())
            entered.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return check_result(True, "tls", "OLD-DESTINATION-RESULT")

        with patch("service.check_route_tls", side_effect=old_route):
            self.assertTrue(self.service.check_route_async("old-destination.invalid", 443))
            self.assertTrue(entered.wait(2))
            self.service.invalidate_route_diagnostics()
            workers[0].join(2)
            self.assertFalse(workers[0].is_alive())
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["state"], "running")
        self.assertIs(snapshot["listener_check"]["ok"], True)
        self.assertIsNot(snapshot["route_check"]["ok"], True)
        self.assertIsNone(self.service.poll_route_result())
        self.assertNotIn("OLD-DESTINATION-RESULT", json.dumps(snapshot))

    def test_shutdown_cancels_both_probes_within_its_deadline(self):
        upstream_entered = threading.Event()
        route_entered = threading.Event()
        upstream_finished = threading.Event()
        route_finished = threading.Event()

        async def slow_upstream(*args, **kwargs):
            upstream_entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                upstream_finished.set()

        async def slow_route(*args, **kwargs):
            route_entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                route_finished.set()

        with patch("service.check_upstream", side_effect=slow_upstream), patch(
            "service.check_route_tls", side_effect=slow_route
        ):
            config = ProxyConfig(listen_port=unused_port(), timeout=1,
                                 mode="socks5", upstream_host="fixture.invalid")
            self.assertTrue(self.service.start(config))
            self.wait_for(lambda: self.service.state == "running")
            self.assertTrue(self.service.check_route_async("fixture.invalid", 443))
            self.assertTrue(upstream_entered.wait(2))
            self.assertTrue(route_entered.wait(2))
            started = time.monotonic()
            self.assertTrue(self.service.shutdown(1.5))
            self.assertLess(time.monotonic() - started, 1.8)
            self.assertTrue(upstream_finished.is_set())
            self.assertTrue(route_finished.is_set())
            self.assertFalse(self.service.checking)
            self.assertFalse(self.service.route_checking)

    def test_export_report_has_no_configuration_or_free_text_secrets(self):
        config = ProxyConfig(
            listen_port=unused_port(), mode="socks5", timeout=1,
            upstream_host="sensitive-upstream-fixture.invalid",
            username="synthetic-private-user", password="synthetic-private-password",
        )
        target = "sensitive-target-fixture.invalid"
        sensitive = [config.upstream_host, config.username, config.password,
                     target, r"C:\private-fixture\account.json"]

        async def noisy_probe(*args, **kwargs):
            return check_result(True, "authentication", " ".join(sensitive))

        async def noisy_route(*args, **kwargs):
            return check_result(True, "tls", " ".join(sensitive))

        with patch("service.check_upstream", side_effect=noisy_probe), patch(
            "service.check_route_tls", side_effect=noisy_route
        ):
            self.service.start(config)
            self.wait_for(lambda: self.service.state == "running")
            self.wait_for(lambda: self.service.snapshot()["upstream_check"]["ok"] is True)
            self.assertTrue(self.service.check_route_async(target, 443))
            self.wait_for(lambda: self.service.snapshot()["route_check"]["ok"] is True)
            report = self.service.diagnostic_report()
        encoded = json.dumps(report)
        for value in sensitive:
            self.assertNotIn(value, encoded)
        self.assertNotIn('"username"', encoded)
        self.assertNotIn('"password"', encoded)
        self.assertEqual(report["entitlement"], "unverified")

    def test_stopped_upstream_report_describes_the_configuration_checked(self):
        async def checked_upstream(*args, **kwargs):
            return check_result(True, "authentication", "Fixture authenticated.")

        with patch("service.check_upstream", side_effect=checked_upstream):
            config = ProxyConfig(mode="socks5", upstream_host="fixture.invalid")
            self.assertTrue(self.service.check_upstream_async(config))
            self.wait_for(lambda: self.service.snapshot()["upstream_check"]["ok"] is True)
            report = self.service.diagnostic_report()
        self.assertEqual(report["state"], "stopped")
        self.assertEqual(report["mode"], "socks5")
        self.assertEqual(report["upstream_check"]["stage"], "authentication")

    def test_snapshot_nested_checks_are_not_mutable_service_state(self):
        self.start_direct()
        snapshot = self.service.snapshot()
        snapshot["listener_check"]["ok"] = False
        snapshot["route_check"]["ok"] = True
        self.assertIs(self.service.snapshot()["listener_check"]["ok"], True)
        self.assertIsNot(self.service.snapshot()["route_check"]["ok"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
