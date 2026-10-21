"""GUI regressions for configuration preservation and explicit diagnostics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SOURCE = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(SOURCE))

from PySide6.QtWidgets import QApplication
from app import MainWindow
from proxy_engine import ProxyConfig
from settings import load_settings, save_settings


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="gui-proxy-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        self.windows = []
        self.tray_patch = patch.object(MainWindow, "_create_tray", lambda window: setattr(window, "tray", None))
        self.tray_patch.start()
        self.addCleanup(self.tray_patch.stop)
        self.addCleanup(self.close_windows)

    def close_windows(self):
        for window in self.windows:
            window.service.shutdown(3)
            window.close()
            window.deleteLater()
        self.application.processEvents()

    def window(self):
        window = MainWindow(config_path=self.path)
        self.windows.append(window)
        return window

    def wait_for(self, predicate, timeout=4):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("GUI action did not complete")

    def test_bad_file_remains_untouched_until_explicit_defaults_and_save(self):
        invalid = b'{"schema_version":1,"mode":"socks5",broken'
        self.path.write_bytes(invalid)
        window = self.window()
        self.assertFalse(window.start_button.isEnabled())
        self.assertFalse(window.save_button.isEnabled())
        window.local_port.setValue(10809)
        window.start_proxy()  # A direct callback must honor the same guard.
        self.assertEqual(window.service.state, "stopped")
        self.assertEqual(self.path.read_bytes(), invalid)
        window.restore_defaults()
        self.assertTrue(window.start_button.isEnabled())
        self.assertEqual(self.path.read_bytes(), invalid)
        window.save_config()
        self.assertEqual(load_settings(self.path).mode, "direct")

    def test_import_preserves_ipv6_timeout_and_private_target_policy(self):
        profile = Path(self.directory.name) / "profile.json"
        config = ProxyConfig(listen_host="::1", listen_port=19090, timeout=37,
                             mode="socks5", upstream_host="localhost", upstream_port=19091,
                             allow_private_targets=False)
        save_settings(config, profile)
        window = self.window()
        with patch("app.QFileDialog.getOpenFileName", return_value=(str(profile), "JSON")):
            window.import_config()
        current = window.current_config()
        self.assertEqual(current.listen_host, "::1")
        self.assertEqual(current.timeout, 37)
        self.assertFalse(current.allow_private_targets)
        self.assertFalse(self.path.exists())
        self.assertIn("::1", window.local_host_label.text())
        with patch("app.QApplication.clipboard") as clipboard:
            window.copy_address()
            clipboard.return_value.setText.assert_called_once_with("socks5://[::1]:19090")
        window.username.setText("fixture-user-private")
        window.password.setText("fixture-password-private")
        window.save_config()
        raw = self.path.read_text("utf-8")
        self.assertNotIn("fixture", raw)
        saved = load_settings(self.path)
        self.assertEqual(saved.listen_host, "::1")
        self.assertEqual(saved.timeout, 37)
        self.assertFalse(saved.allow_private_targets)

    def test_import_rejects_secret_fields_without_replacing_current_config(self):
        secret_profile = Path(self.directory.name) / "invalid-profile.json"
        secret_profile.write_text(json.dumps({"schema_version": 1, "password": "fixture-secret"}), encoding="utf-8")
        save_settings(ProxyConfig(listen_port=19092), self.path)
        original = self.path.read_bytes()
        window = self.window()
        with patch("app.QFileDialog.getOpenFileName", return_value=(str(secret_profile), "JSON")):
            window.import_config()
        self.assertEqual(window.current_config().listen_port, 19092)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIn("导入失败", window.feedback.text())
        self.assertNotIn("fixture-secret", window.feedback.text())

    def test_real_start_requires_listener_check_and_keeps_entitlement_unverified(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        save_settings(ProxyConfig(listen_port=port, timeout=1), self.path)
        window = self.window()
        self.assertEqual(window.target_host.text(), "")
        self.assertFalse(window.route_check_button.isEnabled())
        window.start_proxy()
        self.wait_for(lambda: window.service.state == "running")
        window.refresh()
        self.assertTrue(window.service.snapshot()["listener_check"]["ok"])
        self.assertIn("握手通过", window.check_labels["listener_check"].text())
        self.assertIn("未验证", window.check_labels["entitlement"].text())
        self.assertFalse(window.import_button.isEnabled())
        self.assertFalse(window.defaults_button.isEnabled())
        self.assertTrue(window.route_check_button.isEnabled())
        self.assertIsNone(window.service.snapshot()["route_check"]["ok"])
        window.route_feedback.setText("fixture previous route success")
        window.feedback.setText("fixture previous upstream success")
        window.stop_proxy()
        self.wait_for(lambda: window.service.state == "stopped")
        window.refresh()
        self.assertFalse(window.route_check_button.isEnabled())
        self.assertNotIn("fixture", window.route_feedback.text())
        self.assertNotIn("fixture", window.feedback.text())
        self.assertIn("未验证", window.check_labels["entitlement"].text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
