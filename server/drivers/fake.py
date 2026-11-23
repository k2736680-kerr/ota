"""dry-run / 单测用的假手机。不触碰真机。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeAdb:
    serial: str = "DRYRUN"
    model: str = "dry-run-phone"
    android: str = "14"
    battery_level: int = 80
    charging: bool = True
    app_installed: bool = True
    app_version: str = "1.0.0"
    process_alive: bool = True
    bluetooth_on: bool = True
    airplane: bool = False
    screen_locked: bool = False
    connected: bool = True
    visible: bool = True
    device_name: str = "L806"
    device_mac: str = "00:11:22:33:44:55"
    logcat: str = "dry-run logcat line\n"
    shell_log: list[str] = field(default_factory=list)
    fail_shell: set[str] = field(default_factory=set)

    def devices(self) -> list[str]:
        return [self.serial] if self.serial else []

    def shell(self, script: str, check: bool = True) -> str:
        self.shell_log.append(script)
        for token in self.fail_shell:
            if token in script:
                if check:
                    raise RuntimeError(f"fake shell failed: {script}")
                return ""
        if script.startswith("getprop ro.product.model"):
            return self.model
        if script.startswith("getprop ro.build.version.release"):
            return self.android
        if script.startswith("dumpsys package"):
            if not self.app_installed:
                return "Unable to find package"
            return f"Package [x]\n    versionName={self.app_version}\n"
        if script.startswith("dumpsys battery"):
            power = "true" if self.charging else "false"
            return f"level: {self.battery_level}\nAC powered: {power}\nUSB powered: false\nstatus: 2\n"
        if script.startswith("pidof"):
            return "1234" if self.process_alive else ""
        if "bluetooth_manager" in script:
            return self._bluetooth_dump()
        if "airplane_mode_on" in script:
            return "1" if self.airplane else "0"
        if script.startswith("dumpsys window"):
            flag = "true" if self.screen_locked else "false"
            return f"mShowingLockscreen={flag}\nisStatusBarKeyguard={flag}\n"
        if script.startswith("logcat"):
            return self.logcat
        return "ok"

    def getprop(self, name: str) -> str:
        return self.shell(f"getprop {name}", check=False).strip()

    def run_as(self, pkg: str, script: str) -> str:
        self.shell_log.append(f"run-as {pkg} {script}")
        if "band_version" in script:
            return "MOY-V7T3-2.0.0"
        if "firmware_version" in script:
            return "MOY-V7T3-2.0.1"
        return ""

    def package_debuggable(self, pkg: str) -> bool:
        return True

    def package_version(self, pkg: str) -> tuple[bool, str]:
        output = self.shell(f"dumpsys package {pkg}", check=False)
        if "Unable to find package" in output:
            return False, ""
        return True, self.app_version

    def battery(self) -> tuple[int | None, bool]:
        return self.battery_level, self.charging

    def foreground(self) -> str:
        return f"topResumedActivity={self.serial}"

    def pid_of(self, pkg: str) -> str:
        return self.shell(f"pidof {pkg}", check=False).strip()

    def window_size(self) -> tuple[int, int]:
        return 1080, 1920

    def screencap(self, remote: str = "/sdcard/ota_lab.png") -> str:
        self.shell_log.append(f"screencap {remote}")
        return remote

    def pull(self, remote: str, local: str) -> str:
        self.shell_log.append(f"pull {remote} {local}")
        return "pulled"

    def snapshot(self, pkg: str):
        from server.drivers.adb import DeviceInfo

        info = DeviceInfo(serial=self.serial, model=self.model, android=self.android)
        info.battery_level = self.battery_level
        info.charging = self.charging
        info.app_installed = self.app_installed
        info.app_version = self.app_version
        return info

    def _bluetooth_dump(self) -> str:
        state = "ON" if self.bluetooth_on else "OFF"
        if not self.visible and not self.connected:
            return f"state: {state}\nenabled: {str(self.bluetooth_on).lower()}\n"
        link_state = "CONNECTED" if self.connected else "DISCONNECTED"
        mac = self.device_mac or "00:11:22:33:44:55"
        name = self.device_name or "L806"
        return (
            f"state: {state}\nenabled: {str(self.bluetooth_on).lower()}\n"
            f"Bonded devices:\n"
            f"  {mac} [ DUAL ] {name}\n"
            f"Device {mac} name={name} state={link_state}\n"
            f"  com.example.app (Registered)\n"
            f"  Connections: 1\n"
            f"    01-01 00:00:00.000 - 1ms : {mac} (1)\n"
        )


class FakeNode:
    def __init__(self, device: "FakeUiDevice", key: str, text: str, exists: bool) -> None:
        self.device = device
        self.key = key
        self.text = text
        self.exists = exists

    def wait(self, timeout: float = 0) -> bool:
        return self.exists

    def click(self) -> None:
        self.device.note_click(self.key)

    @property
    def info(self) -> dict:
        return {"text": self.text}


class FakeUiDevice:
    """点击升级后进入传输页；传输页同时带成功文案，dry-run 才能在注入后收口。"""

    def __init__(self, idle: str | None = None, busy: str | None = None, strict: bool = False) -> None:
        self.idle = idle or "已连接 固件升级 立即升级 实报:2.0.0 公布:2.0.1 githash:C07F291"
        self.busy = busy or "已连接 正在升级 传输中 30% 升级成功 实报:2.0.0 公布:2.0.1 githash:C07F291"
        self.strict = strict
        self.phase = 0
        self.missing: set[str] = set()
        self.clicks: list[str] = []

    def note_click(self, key: str) -> None:
        self.clicks.append(key)
        self.phase = 1

    def click(self, x: float, y: float) -> None:
        self.clicks.append(f"xy:{x},{y}")
        self.phase = 1

    def dump_hierarchy(self) -> str:
        return self.busy if self.phase else self.idle

    def __call__(self, **kwargs):
        key = (
            kwargs.get("text")
            or kwargs.get("textContains")
            or kwargs.get("resourceId")
            or kwargs.get("textMatches")
            or ""
        )
        page = self.dump_hierarchy()
        # 默认「查什么有什么」会让 _has() 永远为真，于是任何页面都同时被当成
        # 「升级失败」「升级成功」「正在升级」。2026-11-13 就是这样把
        # 「您的固件是最新版本」页判成 failure 的。这里按页面内容如实回答：
        # 传了 idle/busy 页面时，节点是否存在由页面里有没有这段文字决定。
        if self.strict:
            needle = str(key)
            present = needle in page or needle.replace("com.example.", "") in page
            exists = present and needle not in self.missing
        else:
            exists = str(key) not in self.missing
        text = ""
        if "tv_version" in str(key) and "tv_new_version" not in str(key):
            text = _pick(page, r"实报[:：]\s*([0-9.]+)", "2.0.0")
        elif "tv_new_version" in str(key):
            text = _pick(page, r"公布[:：]\s*([0-9.]+)", "2.0.1")
        elif "当前版本" in str(key):
            text = _pick(page, r"当前版本：\s*([^<\"]+)", _pick(page, r"实报[:：]\s*([0-9.]+)", "2.0.0"))
        elif "githash" in str(key).lower():
            text = _pick(page, r"githash[:：]\s*([A-Za-z0-9]+)", "C07F291")
        return FakeNode(self, str(key), text or str(key), exists)


def _pick(page: str, pattern: str, default: str) -> str:
    import re

    found = re.search(pattern, page)
    return found.group(1) if found else default
