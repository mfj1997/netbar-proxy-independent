"""Independent desktop proxy controller. No original application components."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QIntValidator, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSystemTrayIcon, QTabWidget,
    QVBoxLayout, QWidget,
)

from proxy_engine import ProxyConfig
from service import ProxyService
from settings import load_settings, save_settings, validate_config


STYLES = """
QWidget { color: #263f56; font-family: "Microsoft YaHei UI"; font-size: 13px; }
QMainWindow, QWidget#page { background: #dce8f4; }
QLabel#title { font-size: 23px; font-weight: 700; color: #203d57; }
QLabel#subtitle { color: #516c82; font-size: 12px; }
QLabel#eyebrow { font-size: 11px; font-weight: 700; color: #54768e; }
QLabel#section { font-size: 14px; font-weight: 700; color: #284963; }
QLabel#status { font-size: 18px; font-weight: 700; }
QLabel#muted { color: #557084; font-size: 12px; }
QLabel#metric { font-family: "Consolas"; font-size: 20px; font-weight: 700; }
QFrame#panel { background: #f5f9fd; border: 1px solid #c4d6e6; border-radius: 12px; }
QFrame#route { background: #e6f0fa; border: 1px solid #c4d8eb; border-radius: 7px; }
QLineEdit, QSpinBox, QComboBox {
    background: #ffffff; border: 1px solid #b5cadb; border-radius: 6px;
    padding: 7px 9px; min-height: 22px;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 2px solid #467cad; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { color: #788b99; background: #eaf0f5; }
QComboBox::drop-down { border: none; width: 25px; }
QComboBox QAbstractItemView { background: white; selection-background-color: #dbeaf8; color: #263f56; }
QPushButton {
    background: #edf4fa; border: 1px solid #b6ccde; border-radius: 7px;
    padding: 9px 15px; font-weight: 600; min-height: 19px;
}
QPushButton:hover { background: #dcebf8; border-color: #81a4c1; }
QPushButton:focus { border: 2px solid #467cad; }
QPushButton:disabled { color: #8a9baa; background: #e5edf4; border-color: #c9d6e1; }
QPushButton#start { background: #287552; color: white; border-color: #287552; font-size: 15px; }
QPushButton#start:hover { background: #1d6243; }
QPushButton#start:disabled { background: #b6cfc3; border-color: #b6cfc3; color: #eff6f1; }
QPushButton#stop { background: #fff0ec; color: #a04336; border-color: #e1b2a9; }
QPushButton#stop:disabled { background: #e9eef3; border-color: #c9d6e1; color: #8a9baa; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QPlainTextEdit {
    background: #f8fbfe; border: 1px solid #c4d6e6; border-radius: 8px;
    padding: 10px; font-family: "Microsoft YaHei UI"; font-size: 12px;
}
QTabWidget::pane { border: none; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QTabBar::tab { background: transparent; padding: 10px 16px; color: #617c91; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #244f72; border-bottom: 2px solid #3f769f; font-weight: 700; }
QToolTip { background: #203d57; color: white; padding: 6px; border: none; }
"""


class ModeCombo(QComboBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#56768e" if self.isEnabled() else "#98aab7"))
        x, y = self.width() - 16, self.height() // 2
        painter.drawPolygon(QPolygon([QPoint(x - 4, y - 2), QPoint(x + 4, y - 2), QPoint(x, y + 3)]))
        painter.end()


class PortEdit(QLineEdit):
    def setRange(self, lower: int, upper: int):
        self.setValidator(QIntValidator(lower, upper, self))
        self.setMaxLength(5)

    def setValue(self, value: int):
        self.setText(str(value))

    def value(self) -> int:
        return int(self.text()) if self.text().isdigit() else 0


def app_icon() -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#315b7c"))
    p.drawRoundedRect(0, 0, 64, 64, 15, 15)
    p.setPen(QPen(QColor("#dceaf5"), 4))
    p.drawLine(18, 32, 46, 32)
    p.setBrush(QColor("#93d6b1"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(11, 25, 14, 14)
    p.setBrush(QColor("#e5f0fa"))
    p.drawEllipse(39, 25, 14, 14)
    p.end()
    return QIcon(pix)


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    return w


def panel() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    return frame, layout


def size_text(value: int) -> str:
    number = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if number < 1024 or unit == "GB":
            return f"{number:.1f} {unit}" if unit != "B" else f"{int(number)} B"
        number /= 1024
    return "0 B"


class MainWindow(QMainWindow):
    def __init__(self, *, preview: bool = False, config_path: Path | None = None):
        super().__init__()
        self.preview = preview
        self.config_path = config_path
        self.service = ProxyService()
        self.last_state = ""
        self._exit_pending = False
        self._last_config = ProxyConfig()
        self.setWindowTitle("网吧代理 · 独立版")
        self.setWindowIcon(app_icon())
        self.resize(570, 750)
        self.setMinimumSize(540, 660)
        if preview:
            self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self._build()
        self._create_tray()
        self._load()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.refresh()

    def _build(self):
        page = QWidget()
        page.setObjectName("page")
        self.setCentralWidget(page)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(14)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(label("网吧代理", "title"))
        titles.addWidget(label("独立内核  /  无卡密  /  无到期限制", "subtitle"))
        header.addLayout(titles)
        header.addStretch()
        mini = QPushButton("收起到托盘")
        mini.setToolTip("代理会继续运行；使用托盘菜单显示或退出。")
        mini.clicked.connect(self.minimize_to_tray)
        self.tray_button = mini
        header.addWidget(mini, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        status_panel, status_layout = panel()
        status_header = QHBoxLayout()
        self.status_dot = label("●", "status")
        self.status_dot.setStyleSheet("color:#8d9fac")
        self.status_title = label("代理未启动", "status")
        status_header.addWidget(self.status_dot)
        status_header.addWidget(self.status_title)
        status_header.addStretch()
        status_layout.addLayout(status_header)
        self.status_detail = label("选择转发方式，然后启动本机代理。", "muted", True)
        status_layout.addWidget(self.status_detail)
        route = QFrame()
        route.setObjectName("route")
        route_layout = QHBoxLayout(route)
        route_layout.setContentsMargins(12, 9, 12, 9)
        self.route_label = label("本机应用  →  独立代理  →  直连网络", "subtitle")
        route_layout.addWidget(self.route_label)
        status_layout.addWidget(route)
        stats = QHBoxLayout()
        self.metrics = {}
        for key, title, initial in (
            ("active_connections", "当前连接", "0"),
            ("bytes_up", "已发送", "0 B"),
            ("bytes_down", "已接收", "0 B"),
        ):
            stack = QVBoxLayout()
            stack.setSpacing(3)
            stack.addWidget(label(title, "muted"))
            metric = label(initial, "metric")
            metric.setMinimumWidth(120)
            self.metrics[key] = metric
            stack.addWidget(metric)
            stats.addLayout(stack)
            stats.addStretch()
        status_layout.addLayout(stats)
        outer.addWidget(status_panel)

        tabs = QTabWidget()
        config_shell = QScrollArea()
        config_shell.setWidgetResizable(True)
        config_shell.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        config_tab = QWidget()
        config_shell.setWidget(config_tab)
        config_layout = QVBoxLayout(config_tab)
        config_layout.setContentsMargins(0, 10, 0, 0)
        config_layout.setSpacing(12)
        settings_panel, settings_layout = panel()
        settings_layout.addWidget(label("代理配置", "section"))
        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        self.mode = ModeCombo()
        self.mode.addItem("直连转发 · 不切换出口", "direct")
        self.mode.addItem("SOCKS5 上游代理", "socks5")
        self.mode.addItem("HTTP CONNECT 上游代理", "http")
        self.mode.addItem("HTTPS CONNECT 上游代理", "https")
        self.mode.currentIndexChanged.connect(self.mode_changed)
        form.addRow("转发方式", self.mode)
        local_row = QHBoxLayout()
        local_row.addWidget(label("127.0.0.1  :"))
        self.local_port = PortEdit()
        self.local_port.setRange(1024, 65535)
        self.local_port.setValue(10808)
        self.local_port.setAccessibleName("本地 SOCKS5 端口")
        local_row.addWidget(self.local_port)
        self.copy_button = QPushButton("复制地址")
        self.copy_button.clicked.connect(self.copy_address)
        local_row.addWidget(self.copy_button)
        form.addRow("本地 SOCKS5", local_row)
        settings_layout.addLayout(form)

        self.remote_box = QWidget()
        remote = QFormLayout(self.remote_box)
        remote.setContentsMargins(0, 0, 0, 0)
        remote.setVerticalSpacing(12)
        remote.setHorizontalSpacing(16)
        remote_host_row = QHBoxLayout()
        self.host = QLineEdit()
        self.host.setPlaceholderText("服务器域名或 IP")
        self.host.setAccessibleName("上游服务器地址")
        self.remote_port = PortEdit()
        self.remote_port.setRange(1, 65535)
        self.remote_port.setValue(1080)
        self.remote_port.setFixedWidth(98)
        self.remote_port.setAccessibleName("上游服务器端口")
        remote_host_row.addWidget(self.host)
        remote_host_row.addWidget(self.remote_port)
        remote.addRow("上游服务器", remote_host_row)
        self.username = QLineEdit()
        self.username.setPlaceholderText("可选；仅保留在本次会话")
        remote.addRow("用户名", self.username)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("可选；不会写入配置文件")
        remote.addRow("密码", self.password)
        self.check_button = QPushButton("检测上游连接")
        self.check_button.clicked.connect(self.check_upstream)
        remote.addRow("", self.check_button)
        settings_layout.addWidget(self.remote_box)
        self.mode_note = label("", "muted", True)
        settings_layout.addWidget(self.mode_note)
        config_layout.addWidget(settings_panel)

        self.feedback = label("", "muted", True)
        self.feedback.setMinimumHeight(32)
        config_layout.addWidget(self.feedback)
        config_layout.addStretch()
        tabs.addTab(config_shell, "代理控制")

        log_tab = QWidget()
        logs = QVBoxLayout(log_tab)
        logs.setContentsMargins(0, 10, 0, 0)
        logs.addWidget(label("记录启动、停止和连接错误，不记录密码或访问地址。", "muted", True))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(300)
        self.log_view.setPlaceholderText("启动代理后，运行记录会显示在这里。")
        logs.addWidget(self.log_view)
        clear = QPushButton("清空记录")
        clear.clicked.connect(self.log_view.clear)
        logs.addWidget(clear, alignment=Qt.AlignmentFlag.AlignRight)
        tabs.addTab(log_tab, "运行记录")
        outer.addWidget(tabs, stretch=1)

        controls = QHBoxLayout()
        controls.setSpacing(12)
        self.start_button = QPushButton("启动代理")
        self.start_button.setObjectName("start")
        self.start_button.setMinimumHeight(31)
        self.start_button.clicked.connect(self.start_proxy)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("stop")
        self.stop_button.setMinimumHeight(31)
        self.stop_button.clicked.connect(self.stop_proxy)
        controls.addWidget(self.start_button, stretch=3)
        controls.addWidget(self.stop_button, stretch=1)
        outer.addLayout(controls)
        outer.addWidget(label(
            "在需要代理的应用中填写本地 SOCKS5 地址。\n"
            "本工具提供网络转发；网吧活动资格需在官方服务中确认。",
            "muted", True,
        ))

    def _create_tray(self):
        self.tray = None
        if not self.preview and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.windowIcon(), self)
            self.tray.setToolTip("网吧代理 · 未启动")
            menu = QMenu(self)
            show_action = QAction("显示窗口", self)
            show_action.triggered.connect(self.show_window)
            stop_action = QAction("停止代理", self)
            stop_action.triggered.connect(self.stop_proxy)
            exit_action = QAction("退出", self)
            exit_action.triggered.connect(self.close)
            menu.addAction(show_action)
            menu.addAction(stop_action)
            menu.addSeparator()
            menu.addAction(exit_action)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(
                lambda reason: self.show_window()
                if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
            )
            self.tray.show()
        self.tray_button.setEnabled(self.tray is not None)

    def _load(self):
        try:
            config = ProxyConfig() if self.preview else load_settings(self.config_path)
            self.mode.setCurrentIndex(max(0, self.mode.findData(config.mode)))
            self.local_port.setValue(config.listen_port)
            self.host.setText(config.upstream_host)
            self.remote_port.setValue(config.upstream_port)
            self._last_config = config
        except (ValueError, OSError, TypeError):
            self.feedback.setText("配置文件无法读取，已使用默认配置。重新启动代理可保存新配置。")
        self.mode_changed()

    def current_config(self):
        mode = self.mode.currentData()
        return validate_config(ProxyConfig(
            listen_host="127.0.0.1",
            listen_port=self.local_port.value(),
            mode=mode,
            upstream_host=self.host.text().strip() if mode != "direct" else "",
            upstream_port=self.remote_port.value() if mode != "direct" else 1080,
            username=self.username.text() if mode != "direct" else "",
            password=self.password.text() if mode != "direct" else "",
            timeout=10,
            allow_private_targets=True,
        ))

    def mode_changed(self):
        mode = self.mode.currentData()
        self.remote_box.setVisible(mode != "direct")
        if mode == "direct":
            note = "直接通过当前网络转发 TCP / UDP，不改变公网出口。"
            end = "直连网络"
        elif mode == "socks5":
            note = "支持 TCP；UDP 还需要上游服务器支持 UDP ASSOCIATE。SOCKS5 本身不加密传输。"
            end = "SOCKS5 上游"
        elif mode == "http":
            note = "仅转发 TCP。HTTP 上游连接不加密；跨公网使用时建议选择 HTTPS 上游。"
            end = "HTTP 上游"
        else:
            note = "通过 TLS 连接上游并校验证书；仅转发 TCP。"
            end = "HTTPS 上游"
        self.mode_note.setText(note)
        self.route_label.setText(f"本机应用  →  独立代理  →  {end}")

    def start_proxy(self):
        try:
            config = self.current_config()
            if not self.preview:
                try:
                    save_settings(config, self.config_path)
                except OSError:
                    self.add_log("配置未能保存；本次仍可运行。")
            self._last_config = config
            self.feedback.setText("")
            self.service.start(config)
        except (ValueError, OSError, TypeError) as exc:
            self.feedback.setText(str(exc))
        self.refresh()

    def stop_proxy(self):
        self.service.stop()
        self.refresh()

    def check_upstream(self):
        try:
            config = self.current_config()
            if self.service.check_upstream_async(config):
                self.feedback.setText("正在检测上游连接…")
                self.check_button.setEnabled(False)
        except (ValueError, OSError, TypeError) as exc:
            self.feedback.setText(str(exc))

    def copy_address(self):
        snapshot = self.service.snapshot()
        port = snapshot.get("listen_port") if snapshot.get("state") == "running" else self.local_port.value()
        address = f"socks5://127.0.0.1:{port}"
        QApplication.clipboard().setText(address)
        self.feedback.setText(f"已复制 {address}")

    def add_log(self, message: str):
        self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    def refresh(self):
        snap = self.service.snapshot()
        state = snap.get("state", "stopped")
        for entry in self.service.drain_logs():
            self.add_log(entry)
        check = self.service.poll_check_result()
        if check is not None:
            self.check_button.setEnabled(True)
            self.feedback.setText(check.get("message", "检测已结束。"))
        title, color = {
            "stopped": ("代理未启动", "#8d9fac"),
            "starting": ("正在启动", "#b27a27"),
            "running": ("本地代理已就绪", "#287552"),
            "stopping": ("正在停止", "#b27a27"),
            "failed": ("启动或运行失败", "#ad4439"),
        }.get(state, ("代理未启动", "#8d9fac"))
        self.status_title.setText(title)
        self.status_dot.setStyleSheet(f"color:{color}")
        if state == "running":
            port = snap.get("listen_port") or self._last_config.listen_port
            detail = f"SOCKS5  127.0.0.1:{port}"
            if self._last_config.mode == "direct":
                detail += "  ·  当前网络直连出口"
            else:
                detail += "  ·  上游在实际连接时验证转发"
        elif state == "failed":
            detail = snap.get("error") or "请查看运行记录，检查端口和上游配置后重试。"
        elif state == "starting":
            detail = "正在绑定本地端口。"
        elif state == "stopping":
            detail = "正在关闭本工具的连接并释放端口。"
        else:
            detail = "选择转发方式，然后启动本机代理。"
        self.status_detail.setText(detail)
        busy = state in ("starting", "running", "stopping")
        self.start_button.setEnabled(not busy)
        self.stop_button.setEnabled(state in ("starting", "running"))
        for widget in (self.mode, self.local_port, self.host, self.remote_port, self.username, self.password):
            widget.setEnabled(not busy)
        for key, widget in self.metrics.items():
            value = snap.get(key, 0)
            widget.setText(str(value) if key == "active_connections" else size_text(value))
        if self.tray and state != self.last_state:
            self.tray.setToolTip(f"网吧代理 · {title}")
        self.last_state = state

    def minimize_to_tray(self):
        if self.tray:
            self.hide()
            self.tray.showMessage("网吧代理", "程序已收起。双击托盘图标可恢复窗口。")

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        # Closing the window exits and stops the proxy. Hiding is a separate action.
        if self.service.shutdown(timeout=3):
            self.timer.stop()
            if self.tray:
                self.tray.hide()
            event.accept()
        else:
            event.ignore()
            self._exit_pending = True
            self.feedback.setText("正在关闭连接，稍后自动退出。")
            QTimer.singleShot(500, self.close)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", type=Path, help="Render an offline UI preview and exit.")
    parser.add_argument("--config", type=Path, help="Use a separate configuration file.")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("网吧代理独立版")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLES)
    app.setFont(QFont("Microsoft YaHei UI", 9))
    window = MainWindow(preview=args.preview is not None, config_path=args.config)
    window.show()
    if args.preview:
        def save_preview():
            args.preview.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.preview))
            window.close()
            app.quit()
        QTimer.singleShot(300, save_preview)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
