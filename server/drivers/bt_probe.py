"""蓝牙连接三通道探测。任一通道判定已连接，即认为连接有效。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from server.common.device_identity import apply_identity, normalize_mac, resolve_identity


@dataclass
class ProbeResult:
    connected: bool
    channel: str
    detail: dict = field(default_factory=dict)


class BluetoothProbe:
    def __init__(self, adb, ui, link: dict, patterns: dict | None = None) -> None:
        self.adb = adb
        self.ui = ui
        self.link = link
        self.patterns = patterns or {}
        self._identity_ready = False

    def ensure_identity(self) -> dict:
        """本轮还没认出设备时，按 App/GATT 补齐 name/mac，避免 yaml 里的旧值误伤。"""
        if self._identity_ready and (self.link.get("device_mac") or self.link.get("device_name")):
            return {
                "name": self.link.get("device_name") or "",
                "mac": self.link.get("device_mac") or "",
                "source": "cached",
            }
        read_text = None
        if self.ui is not None and hasattr(self.ui, "read_text"):
            read_text = self.ui.read_text
        identity = resolve_identity(self.link, adb=self.adb, read_text=read_text)
        apply_identity(self.link, identity)
        self._identity_ready = True
        return identity

    def channel_a(self) -> bool | None:
        """APP 文案。读不到界面时返回 None，不把空白当成断连。"""
        if self.ui is None:
            return None
        text = _without_status_bar_noise(self.ui.page_text())
        if not text or text.startswith("<!--"):
            return None
        for word in self.link.get("ui_disconnected") or []:
            if word and word in text:
                return False
        for word in self.link.get("ui_connected") or []:
            if word and word in text:
                return True
        return None

    def channel_b(self) -> bool | None:
        raw = self.adb.shell("dumpsys bluetooth_manager", check=False)
        if not raw.strip():
            return None
        self.ensure_identity()
        name = (self.link.get("device_name") or "").strip()
        mac = normalize_mac(self.link.get("device_mac") or "")
        if mac and mac in raw.upper():
            return _mac_connected(raw, mac)
        if name and name in raw:
            return _name_connected(raw, name)
        # 还认不出具体设备时，通道 B 不做假阴性，交给通道 A。
        return None

    def channel_c(self) -> bool | None:
        """设备是否还在扫描结果里。只辅助，不单独证明已连接。"""
        raw = self.adb.shell("dumpsys bluetooth_manager", check=False)
        self.ensure_identity()
        name = (self.link.get("device_name") or "").strip()
        mac = normalize_mac(self.link.get("device_mac") or "")
        if mac and mac in raw.upper():
            return True
        if name and name in raw:
            return True
        return False if raw.strip() else None

    def connected(self) -> ProbeResult:
        detail = {
            "A": self.channel_a(),
            "B": self.channel_b(),
            "C": self.channel_c(),
        }
        for name in ("A", "B"):
            if detail[name] is True:
                return ProbeResult(True, name, detail)
        if detail["A"] is False and detail["B"] is False:
            return ProbeResult(False, "A+B", detail)
        if detail["A"] is False and detail["B"] is None:
            return ProbeResult(False, "A", detail)
        if detail["B"] is False and detail["A"] is None:
            return ProbeResult(False, "B", detail)
        # 通道都读不到时，不当成断连，交给调用方按环境异常处理。
        return ProbeResult(True, "unknown", detail)

    def env_ok(self) -> tuple[bool, str]:
        devices = self.adb.devices()
        if self.adb.serial and self.adb.serial not in devices:
            return False, "手机不在线"
        if not self.adb.serial and len(devices) != 1:
            return False, "未连接唯一手机"
        bt_raw = self.adb.shell("dumpsys bluetooth_manager", check=False)
        pattern = self.patterns.get("bt_state_on") or r"(?i)state:\s*ON|enabled:\s*true"
        if bt_raw.strip() and not re.search(pattern, bt_raw):
            return False, "手机蓝牙未开启"
        air_raw = self.adb.shell("settings get global airplane_mode_on", check=False).strip()
        if air_raw == "1":
            return False, "飞行模式已打开"
        if not self.adb.pid_of(self.link["pkg"]):
            return False, "APP 进程不在"
        lock_raw = self.adb.shell("dumpsys window", check=False)
        locked = self.patterns.get("screen_unlocked")
        if locked and lock_raw.strip() and not re.search(locked, lock_raw):
            if re.search(r"(?i)mShowingLockscreen=true|isStatusBarKeyguard=true", lock_raw):
                return False, "屏幕未解锁"
        return True, ""


def _without_status_bar_noise(text: str) -> str:
    """状态栏「蓝牙已连接」含有「已连接」，不能当成 App 设备页的连接状态。"""
    cleaned = text or ""
    for noise in ("蓝牙已连接", "蓝牙未连接", "Bluetooth connected", "Bluetooth disconnected"):
        cleaned = cleaned.replace(noise, "")
    return cleaned


def _mac_connected(raw: str, mac: str) -> bool:
    upper = raw.upper()
    index = upper.find(mac)
    window = raw[max(0, index - 180): index + 180]
    if re.search(r"(?i)state\s*[:=]\s*DISCONNECTED|not connected", window):
        return False
    if re.search(r"(?i)state\s*[:=]\s*CONNECTED|connected\s*[:=]\s*true", window):
        return True
    return "CONNECTED" in window.upper()


def _name_connected(raw: str, name: str) -> bool:
    index = raw.find(name)
    window = raw[max(0, index - 180): index + len(name) + 180]
    if re.search(r"(?i)disconnected", window) and not re.search(r"(?i)state\s*[:=]\s*CONNECTED", window):
        return False
    return bool(re.search(r"(?i)connected", window))
