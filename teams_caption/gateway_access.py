"""Device tokens and the network policy for gateway clients on other machines.

Tokens are shown once when created; the data directory only keeps their SHA-256.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

from filelock import FileLock

from . import config

NAME = re.compile(r"^[\w.-]{1,32}$")
RESERVED = {"local"}
# Home and office LANs: RFC 1918, link-local, IPv6 unique-local and link-local.
LAN_NETWORKS = [ipaddress.ip_network(n) for n in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10")]


def devices_path() -> Path:
    return config.DATA_DIR / "gateway-devices.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _revision():
    try:
        info = devices_path().stat()
        return info.st_mtime_ns, info.st_size, info.st_ino
    except FileNotFoundError:
        return None


def _read() -> list[dict]:
    try:
        data = json.loads(devices_path().read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except ValueError as exc:
        raise RuntimeError(f"设备文件 {devices_path()} 已损坏，请修复或删除后重新添加设备") from exc
    devices = data.get("devices", []) if isinstance(data, dict) else []
    return [d for d in devices if isinstance(d, dict) and isinstance(d.get("name"), str) and isinstance(d.get("sha256"), str)]


def _write(devices: list[dict]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=config.DATA_DIR, delete=False, mode="w", encoding="utf-8") as f:
        json.dump({"devices": devices}, f, ensure_ascii=False, indent=2)
    try:
        os.replace(f.name, devices_path())
    finally:
        Path(f.name).unlink(missing_ok=True)


def _lock() -> FileLock:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return FileLock(str(config.DATA_DIR / "gateway-devices.lock"), timeout=5)


def add_device(name: str) -> str:
    """Create a token for a new device and return it. The token itself is not stored."""
    if not isinstance(name, str) or not NAME.match(name) or name.lower() in RESERVED:
        raise RuntimeError("设备名称须为 1–32 个字母、数字、下划线、点或连字符，且不能是 local")
    with _lock():
        devices = _read()
        if any(d["name"].lower() == name.lower() for d in devices):
            raise RuntimeError(f"设备 {name} 已存在。要换新 token，请先 token-revoke {name}")
        token = secrets.token_urlsafe(32)
        devices.append({"name": name, "sha256": _digest(token), "created": time.strftime("%Y-%m-%d %H:%M:%S")})
        _write(devices)
    return token


def revoke_device(name: str) -> bool:
    with _lock():
        devices = _read()
        kept = [d for d in devices if d["name"].lower() != str(name).lower()]
        if len(kept) == len(devices):
            return False
        _write(kept)
    return True


def list_devices() -> list[dict]:
    return [{"name": d["name"], "created": d.get("created", "")} for d in _read()]


class DeviceRegistry:
    """Looks tokens up by hash and rereads the device file after every change."""

    def __init__(self):
        self.lock = threading.Lock()
        self.revision = object()
        self.by_hash: dict[str, str] = {}

    def _refresh(self):
        revision = _revision()
        if revision != self.revision:
            try:
                devices = _read()
            except RuntimeError:
                devices = []  # a damaged file admits no remote device
            self.by_hash = {d["sha256"]: d["name"] for d in devices}
            self.revision = revision

    def lookup(self, token: str) -> str | None:
        if not token:
            return None
        with self.lock:
            self._refresh()
            return self.by_hash.get(_digest(token))

    def count(self) -> int:
        with self.lock:
            self._refresh()
            return len(self.by_hash)


def peer_address(host: str):
    address = ipaddress.ip_address(host.split("%", 1)[0])
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


def parse_networks(values) -> list:
    if isinstance(values, str):
        values = [values]
    try:
        return [ipaddress.ip_network(v, strict=False) for v in values or []]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"gateway_networks 须为 CIDR 网段列表，例如 [\"192.168.1.0/24\"]：{exc}") from None


def remote_allowed(address, networks) -> bool:
    """Remote clients come from the configured networks, or by default from LAN ranges."""
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        return False
    return any(address.version == n.version and address in n for n in networks or LAN_NETWORKS)
