"""Independent desktop proxy controller. No original application components."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QIntValidator, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMenu, QPlainTextEdit,
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
        self._config_error = False
        self._applying_config = False
        self.setWindowTitle("网吧代理 · 独立版 v1.1.0")
        self.setWindowIcon(app_icon())
        self.resize(620, 800)
        self.setMinimumSize(580, 700)
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
        titles.addWidget(label("v1.1.0  /  独立内核  /  无卡密", "subtitle"))
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
        checks = QFormLayout()
        checks.setVerticalSpacing(6)
        checks.setHorizontalSpacing(16)
        self.check_labels = {}
        for key, title in (
            ("listener_check", "本地代理"),
            ("upstream_check", "上游检测"),
            ("route_check", "目标 TLS"),
            ("entitlement", "网吧权益"),
        ):
            value = label("未检测", "muted", True)
            value.setAccessibleName(title + "状态")
            self.check_labels[key] = value
            checks.addRow(label(title, "muted"), value)
        status_layout.addLayout(checks)
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
        self.tabs = tabs
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
        self.local_host_label = label("127.0.0.1  :")
        local_row.addWidget(self.local_host_label)
        self.local_port = PortEdit()
        self.local_port.setRange(1, 65535)
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
        file_buttons = QHBoxLayout()
        self.import_button = QPushButton("导入配置")
        self.import_button.clicked.connect(self.import_config)
        self.save_button = QPushButton("保存配置")
        self.save_button.clicked.connect(self.save_config)
        self.defaults_button = QPushButton("恢复默认配置")
        self.defaults_button.clicked.connect(self.restore_defaults)
        for button in (self.import_button, self.save_button, self.defaults_button):
            file_buttons.addWidget(button)
        config_layout.addLayout(file_buttons)
        config_layout.addWidget(label("配置文件只保存非敏感设置；用户名和密码需在本次会话填写。", "muted", True))
        config_layout.addStretch()
        tabs.addTab(config_shell, "代理控制")

        diagnostics_tab = QWidget()
        diagnostics_layout = QVBoxLayout(diagnostics_tab)
        diagnostics_layout.setContentsMargins(0, 10, 0, 0)
        diagnostic_panel, diagnostic_layout = panel()
        diagnostic_layout.addWidget(label("通过当前代理检测实际转发", "section"))
        diagnostic_layout.addWidget(label(
            "填写你要测试的 HTTPS / TLS 服务。检测会经过当前本地代理和所选上游，验证目标证书与 TLS 握手。", "muted", True,
        ))
        target_row = QHBoxLayout()
        self.target_host = QLineEdit()
        self.target_host.setPlaceholderText("目标域名或 IP（不含 https://）")
        self.target_host.setAccessibleName("TLS 检测目标地址")
        self.target_port = PortEdit()
        self.target_port.setRange(1, 65535)
        self.target_port.setValue(443)
        self.target_port.setFixedWidth(95)
        self.target_port.setAccessibleName("TLS 检测目标端口")
        target_row.addWidget(self.target_host)
        target_row.addWidget(self.target_port)
        diagnostic_layout.addLayout(target_row)
        self.route_check_button = QPushButton("检测实际转发")
        self.route_check_button.clicked.connect(self.check_route)
        diagnostic_layout.addWidget(self.route_check_button)
        self.route_feedback = label("启动代理后可检测。不会自动连接预设网站。", "muted", True)
        diagnostic_layout.addWidget(self.route_feedback)
        diagnostic_layout.addWidget(label("此检测仅验证所选 TCP / TLS 路径，不验证 UDP、游戏连接或网吧权益。", "muted", True))
        diagnostics_layout.addWidget(diagnostic_panel)
        self.export_button = QPushButton("导出诊断报告")
        self.export_button.clicked.connect(self.export_diagnostics)
        diagnostics_layout.addWidget(self.export_button, alignment=Qt.AlignmentFlag.AlignLeft)
        diagnostics_layout.addWidget(label("报告不包含账号、密码、服务器地址或访问目标。", "muted", True))
        diagnostics_layout.addStretch()
        diagnostics_shell = QScrollArea()
        diagnostics_shell.setWidgetResizable(True)
        diagnostics_shell.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        diagnostics_shell.setWidget(diagnostics_tab)
        tabs.addTab(diagnostics_shell, "连接诊断")

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

        self.feedback = label("", "muted", True)
        self.feedback.setMinimumHeight(24)
        outer.addWidget(self.feedback)

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
        for widget in (self.local_port, self.host, self.remote_port, self.username, self.password):
            widget.textChanged.connect(self.config_changed)
        self.mode.currentIndexChanged.connect(self.config_changed)
        self.target_host.textChanged.connect(self.route_target_changed)
        self.target_port.textChanged.connect(self.route_target_changed)

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
            self._apply_config(config)
        except (ValueError, OSError, TypeError):
            self._config_error = True
            self.feedback.setText("配置文件无法读取，已阻止启动且保留原文件。请导入有效配置或明确恢复默认配置。")
        self.mode_changed()

    def _apply_config(self, config: ProxyConfig):
        self._applying_config = True
        try:
            self._last_config = config
            self.mode.setCurrentIndex(max(0, self.mode.findData(config.mode)))
            self.local_port.setValue(config.listen_port)
            self.local_host_label.setText(f"{config.listen_host}  :")
            self.host.setText(config.upstream_host)
            self.remote_port.setValue(config.upstream_port)
            self.username.clear()
            self.password.clear()
            self._config_error = False
        finally:
            self._applying_config = False
        self.service.invalidate_diagnostics()
        self.route_feedback.setText("尚未检测目标。启动代理后可检测实际转发。")
        self.mode_changed()

    def current_config(self):
        mode = self.mode.currentData()
        return validate_config(replace(
            self._last_config,
            listen_port=self.local_port.value(),
            mode=mode,
            upstream_host=self.host.text().strip() if mode != "direct" else "",
            upstream_port=self.remote_port.value() if mode != "direct" else 1080,
            username=self.username.text() if mode != "direct" else "",
            password=self.password.text() if mode != "direct" else "",
        ))

    def config_changed(self, *_):
        if self._applying_config or self.service.state in ("starting", "running", "stopping"):
            return
        self.service.invalidate_diagnostics()
        self.route_feedback.setText("代理配置已修改，请启动后重新检测目标。")
        if not self._config_error:
            self.feedback.setText("配置已修改，检测结果已重置。")
        if hasattr(self, "timer"):
            self.refresh()

    def route_target_changed(self, *_):
        self.service.invalidate_route_diagnostics()
        self.route_feedback.setText("检测目标已修改，请重新检测。")
        if hasattr(self, "timer"):
            self.refresh()

    def restore_defaults(self):
        if self.service.state in ("starting", "running", "stopping"):
            return
        self._apply_config(ProxyConfig())
        self.feedback.setText("已选择默认配置；原文件未修改。点击启动或保存配置后才写入。")
        self.refresh()

    def import_config(self):
        if self.service.state in ("starting", "running", "stopping"):
            return
        filename, _ = QFileDialog.getOpenFileName(self, "导入非敏感代理配置", "", "JSON 配置 (*.json)")
        if not filename:
            return
        try:
            if not Path(filename).is_file():
                raise OSError("Selected file is unavailable")
            config = load_settings(Path(filename))
            self._apply_config(config)
            self.feedback.setText("配置已导入；认证信息需重新填写。启动或保存时才写入当前配置文件。")
        except (ValueError, OSError, TypeError):
            self.feedback.setText("导入失败：请检查配置格式、版本和字段。当前配置及原文件未修改。")
        self.refresh()

    def save_config(self):
        if self._config_error or self.service.state in ("starting", "running", "stopping"):
            return
        try:
            config = self.current_config()
            save_settings(config, self.config_path)
            self._last_config = config
            self.feedback.setText("非敏感配置已保存，未保存用户名或密码。")
        except (ValueError, TypeError) as exc:
            self.feedback.setText(str(exc))
        except OSError:
            self.feedback.setText("配置保存失败，请检查文件夹是否可写；原配置未被替换。")

    def export_diagnostics(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "导出诊断报告", "proxy-diagnostics.json", "JSON 报告 (*.json)",
        )
        if not filename:
            return
        try:
            report = self.service.diagnostic_report()
            Path(filename).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.feedback.setText("诊断报告已导出，不含账号、密码和网络地址。")
        except (OSError, TypeError, ValueError):
            self.feedback.setText("诊断报告保存失败，请选择可写的文件夹。")

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
        if self.service.state in ("starting", "running", "stopping"):
            return
        if self._config_error:
            self.feedback.setText("配置读取失败，尚未允许替换原文件。请先导入有效配置或恢复默认配置。")
            return
        try:
            config = self.current_config()
            if not self.preview:
                try:
                    save_settings(config, self.config_path)
                except OSError:
                    self.add_log("配置未能保存；本次仍可运行。")
            self._last_config = config
            self.feedback.setText("")
            self.route_feedback.setText("尚未检测目标；本地代理就绪后可检测实际转发。")
            if not self.service.start(config):
                self.feedback.setText("代理正在运行或停止，请稍后重试。")
        except (ValueError, OSError, TypeError) as exc:
            self.feedback.setText(str(exc))
        self.refresh()

    def stop_proxy(self):
        self.service.stop()
        self.feedback.setText("正在停止代理，检测结果已重置。")
        self.route_feedback.setText("代理已停止或正在停止，请启动后重新检测目标。")
        self.refresh()

    def check_upstream(self):
        if self._config_error:
            return
        try:
            config = self.current_config()
            if self.service.check_upstream_async(config):
                self.feedback.setText("正在检测上游连接…")
                self.check_button.setEnabled(False)
        except (ValueError, OSError, TypeError) as exc:
            self.feedback.setText(str(exc))

    def check_route(self):
        try:
            if self.service.check_route_async(self.target_host.text().strip(), self.target_port.value()):
                self.route_feedback.setText("正在通过当前代理检测目标 TLS 连接…")
            else:
                self.route_feedback.setText("请先启动代理，或等待当前检测结束。")
        except (ValueError, OSError, TypeError):
            self.route_feedback.setText("请填写有效的目标域名或 IP，以及 1 到 65535 之间的端口。")
        self.refresh()

    def copy_address(self):
        snapshot = self.service.snapshot()
        port = snapshot.get("listen_port") if snapshot.get("state") == "running" else self.local_port.value()
        host = snapshot.get("listen_host") if snapshot.get("state") == "running" else self._last_config.listen_host
        authority = f"[{host}]" if ":" in host else host
        address = f"socks5://{authority}:{port}"
        QApplication.clipboard().setText(address)
        self.feedback.setText(f"已复制 {address}")

    def add_log(self, message: str):
        self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    @staticmethod
    def check_text(check: dict, *, checking: bool = False) -> tuple[str, str]:
        if checking:
            return "检测中…", "#91651f"
        if check.get("stage") == "not_required":
            return "直连模式 · 无上游", "#557084"
        if check.get("ok") is None:
            return "未检测", "#557084"
        if check.get("ok") is not True:
            return "未通过 · 查看检测提示", "#ad4439"
        title = {
            "listener": "本地 SOCKS5 握手通过",
            "transport": "传输连接通过 · 尚未验证转发",
            "authentication": "SOCKS5 连接与认证通过",
            "configuration": "配置有效 · 尚未验证目标连接",
            "tunnel": "目标隧道建立 · 尚未验证 TLS",
            "tls": "目标 TLS 握手与证书通过",
        }.get(check.get("stage"), "检测通过")
        latency = check.get("latency_ms")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool) and latency >= 0:
            title += f" · {latency:.0f} ms"
        return title, "#287552"

    def refresh(self):
        snap = self.service.snapshot()
        state = snap.get("state", "stopped")
        for entry in self.service.drain_logs():
            self.add_log(entry)
        check = self.service.poll_check_result()
        if check is not None:
            self.feedback.setText(check.get("message", "检测已结束。"))
        route_result = self.service.poll_route_result()
        if route_result is not None:
            self.route_feedback.setText(route_result.get("message", "检测已结束。"))
        if self.last_state in ("starting", "running", "stopping") and state in ("stopped", "failed"):
            self.feedback.setText((snap.get("error") or "代理运行失败，请查看运行记录。") if state == "failed" else "本地代理已停止；检测结果已重置。")
            self.route_feedback.setText("当前代理未运行，请启动后重新检测目标。")
        title, color = {
            "stopped": ("代理未启动", "#8d9fac"),
            "starting": ("正在启动", "#b27a27"),
            "running": ("本地代理已就绪", "#287552"),
            "stopping": ("正在停止", "#b27a27"),
            "failed": ("启动或运行失败", "#ad4439"),
        }.get(state, ("代理未启动", "#8d9fac"))
        if self._config_error:
            title, color = "配置需要修复", "#ad4439"
        self.status_title.setText(title)
        self.status_dot.setStyleSheet(f"color:{color}")
        if state == "running":
            port = snap.get("listen_port") or self._last_config.listen_port
            host = snap.get("listen_host") or self._last_config.listen_host
            authority = f"[{host}]" if ":" in host else host
            detail = f"SOCKS5  {authority}:{port}"
            if self._last_config.mode == "direct":
                detail += "  ·  当前网络直连出口"
            else:
                detail += "  ·  可在连接诊断中验证实际转发"
        elif state == "failed":
            detail = snap.get("error") or "请查看运行记录，检查端口和上游配置后重试。"
        elif state == "starting":
            detail = "正在绑定端口并验证本地 SOCKS5 握手。"
        elif state == "stopping":
            detail = "正在关闭本工具的连接并释放端口。"
        else:
            detail = "选择转发方式，然后启动本机代理。"
        if self._config_error:
            detail = "原配置文件已保留，导入有效配置或恢复默认配置后才可启动。"
        self.status_detail.setText(detail)
        for key in ("listener_check", "upstream_check", "route_check"):
            result = snap.get(key) or {}
            checking = (
                (key == "listener_check" and state == "starting")
                or (key == "upstream_check" and self.service.checking)
                or (key == "route_check" and self.service.route_checking)
            )
            text, check_color = self.check_text(result, checking=checking)
            self.check_labels[key].setText(text)
            self.check_labels[key].setStyleSheet(f"color:{check_color}")
            self.check_labels[key].setToolTip(str(result.get("message", "")))
        self.check_labels["entitlement"].setText("未验证 · 需官方服务确认")
        self.check_labels["entitlement"].setStyleSheet("color:#91651f")
        busy = state in ("starting", "running", "stopping")
        self.start_button.setEnabled(not busy and not self._config_error)
        self.stop_button.setEnabled(state in ("starting", "running"))
        for widget in (self.mode, self.local_port, self.host, self.remote_port, self.username, self.password):
            widget.setEnabled(not busy)
        for widget in (self.import_button, self.defaults_button):
            widget.setEnabled(not busy)
        self.save_button.setEnabled(not busy and not self._config_error)
        self.check_button.setEnabled(not self._config_error and not self.service.checking and state not in ("starting", "stopping"))
        self.route_check_button.setEnabled(state == "running" and not self.service.route_checking)
        self.target_host.setEnabled(not self.service.route_checking)
        self.target_port.setEnabled(not self.service.route_checking)
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
