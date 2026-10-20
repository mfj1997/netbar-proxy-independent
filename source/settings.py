"""Validated local preferences. Authentication values are never persisted."""

from __future__ import annotations

from dataclasses import replace
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

from proxy_engine import ProxyConfig


APP_DIRECTORY = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
DEFAULT_SETTINGS_PATH = APP_DIRECTORY / "config.json"
_FIELDS = {
    "listen_host",
    "listen_port",
    "mode",
    "upstream_host",
    "upstream_port",
    "timeout",
    "allow_private_targets",
}
_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\Z")


def _integer_port(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(f"{label}必须是 1 到 65535 之间的整数。")
    return value


def _hostname(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("服务器地址必须是文字。")
    value = value.strip()
    if not value and allow_empty:
        return ""
    if not value:
        raise ValueError("请填写服务器地址。")
    if any(c in value for c in "/\\@?#%[]") or any(c.isspace() for c in value):
        raise ValueError("服务器地址只填写 IP 或域名，不包含协议、端口或路径。")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if ":" in value:
        raise ValueError("IPv6 地址无效，服务器端口请单独填写。")
    if all(c.isdigit() or c == "." for c in value):
        raise ValueError("服务器 IPv4 地址无效。")
    try:
        ascii_name = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("服务器域名格式无效。") from None
    if (
        len(ascii_name) > 253
        or not ascii_name
        or any(not _LABEL.fullmatch(part) for part in ascii_name.split("."))
    ):
        raise ValueError("服务器域名格式无效。")
    return ascii_name


def validate_config(config: ProxyConfig) -> ProxyConfig:
    """Return a normalized copy or raise a concise, credential-free error."""
    if not isinstance(config, ProxyConfig):
        raise ValueError("代理配置类型无效。")
    if not isinstance(config.listen_host, str):
        raise ValueError("监听地址必须是本机环回 IP。")
    listen_host = config.listen_host.strip()
    if listen_host.lower() == "localhost":
        listen_host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(listen_host)
    except ValueError:
        raise ValueError("监听地址必须是本机环回 IP，例如 127.0.0.1。") from None
    if not address.is_loopback or "%" in listen_host:
        raise ValueError("本程序仅允许本机访问，请使用 127.0.0.1 或 ::1。")
    if config.mode not in ("direct", "socks5", "http", "https"):
        raise ValueError("代理模式无效，请重新选择。")
    listen_port = _integer_port(config.listen_port, "本地端口")
    upstream_port = _integer_port(config.upstream_port, "服务器端口")
    upstream_host = _hostname(
        config.upstream_host, allow_empty=config.mode == "direct"
    )
    if (
        isinstance(config.timeout, bool)
        or not isinstance(config.timeout, (int, float))
        or not math.isfinite(config.timeout)
        or not 1 <= config.timeout <= 120
    ):
        raise ValueError("连接超时必须是 1 到 120 秒之间的数字。")
    if type(config.allow_private_targets) is not bool:
        raise ValueError("内网访问设置必须是布尔值。")
    for name, value in (("用户名", config.username), ("密码", config.password)):
        if not isinstance(value, str):
            raise ValueError(f"{name}必须是文字。")
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            raise ValueError(f"{name}含无效的 Unicode 字符。") from None
        if "\x00" in value or len(encoded) > 255:
            raise ValueError(f"{name}不得含空字符，且 UTF-8 长度不能超过 255 字节。")
    result = replace(
        config,
        listen_host=str(address),
        listen_port=listen_port,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        timeout=float(config.timeout),
    )
    # Keep the core's protocol-specific validation in the same call path.
    result.validate()
    return result


def load_settings(path: str | os.PathLike[str] | None = None) -> ProxyConfig:
    """Load non-secret preferences; a missing file gives safe local defaults."""
    location = Path(path) if path is not None else DEFAULT_SETTINGS_PATH
    if not location.exists():
        return validate_config(ProxyConfig())
    try:
        if location.stat().st_size > 16_384:
            raise ValueError("配置文件过大，请重新保存配置。")
        document = json.loads(location.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("配置文件不是有效的 UTF-8 JSON，请重新保存配置。") from None
    if not isinstance(document, dict):
        raise ValueError("配置文件必须是 JSON 对象。")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ValueError("配置文件版本不支持，请重新保存配置。")
    if any(key not in _FIELDS | {"schema_version"} for key in document):
        raise ValueError("配置文件含不支持的字段；用户名和密码应在程序中临时填写。")
    values = {key: value for key, value in document.items() if key in _FIELDS}
    return validate_config(ProxyConfig(**values))


def save_settings(
    config: ProxyConfig, path: str | os.PathLike[str] | None = None
) -> Path:
    """Atomically store the allowlisted non-secret preferences."""
    config = validate_config(config)
    location = Path(path) if path is not None else DEFAULT_SETTINGS_PATH
    location.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 1,
        **{key: getattr(config, key) for key in sorted(_FIELDS)},
    }
    encoded = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{location.name}.",
            suffix=".tmp",
            dir=location.parent,
            delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, location)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return location
