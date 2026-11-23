"""adb 命令封装。业务层只调用这里的方法，不自己拼 adb。"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field


class AdbError(RuntimeError):
    pass


def hidden_subprocess_kwargs() -> dict:
    """窗口程序里每次起 adb 都会闪一个黑框。GlassesTest 用这个把控制台藏掉。"""
    if os.name != "nt":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return {
        "startupinfo": info,
        "creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)),
    }


def hide_console_windows() -> None:
    """uiautomator2 自己也会起进程，只改我们这一处盖不住。挂在 Popen 上。

    必须是子类，不能换成普通函数。asyncio 会写 class Popen(subprocess.Popen)，
    父类不是类时，后面导入直接崩。
    """
    if os.name != "nt" or getattr(subprocess, "_ota_hides_console", False):
        return
    original = subprocess.Popen

    class HiddenPopen(original):
        def __init__(self, *args, **kwargs):
            extra = hidden_subprocess_kwargs()
            flags = int(kwargs.get("creationflags") or 0) | int(extra["creationflags"])
            kwargs["creationflags"] = flags
            info = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = 0
            kwargs["startupinfo"] = info
            super().__init__(*args, **kwargs)

    subprocess.Popen = HiddenPopen
    subprocess._ota_hides_console = True


hide_console_windows()


def parse_battery(output: str) -> tuple[int | None, bool]:
    """只认 dumpsys 的 status。USB 供电但 status 不是充电/已充满时，不算在充电。

    手机插着 adb 时 USB powered 经常是 true，不能用来判断 T09。
    status 2 = 充电中，5 = 已充满。
    """
    level = None
    match = re.search(r"level:\s*(\d+)", output or "")
    if match:
        level = int(match.group(1))
    charging = bool(re.search(r"(?m)^\s*status:\s*[25]\b", output or ""))
    return level, charging


@dataclass
class DeviceInfo:
    serial: str
    model: str = ""
    android: str = ""
    battery_level: int | None = None
    charging: bool = False
    bluetooth_on: bool = True
    airplane: bool = False
    screen_unlocked: bool = True
    app_version: str = ""
    app_installed: bool = False
    manual: dict = field(default_factory=dict)


class AdbDriver:
    def __init__(self, adb_path: str = "adb", serial: str = "", timeout: int = 20) -> None:
        self.adb_path = adb_path or "adb"
        self.serial = serial
        self.timeout = timeout

    def _cmd(self, args: list[str], check: bool = True) -> str:
        command = [self.adb_path]
        if self.serial:
            command.extend(["-s", self.serial])
        command.extend(args)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                **hidden_subprocess_kwargs(),
            )
        except FileNotFoundError as exc:
            raise AdbError(f"找不到 adb: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb 超时: {' '.join(command)}") from exc
        output = (completed.stdout or "") + (completed.stderr or "")
        if check and completed.returncode != 0:
            raise AdbError(output.strip() or f"adb 失败: {' '.join(command)}")
        return output

    def devices(self) -> list[str]:
        output = self._cmd(["devices"], check=False)
        serials = []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    def shell(self, script: str, check: bool = True) -> str:
        return self._cmd(["shell", script], check=check)

    def getprop(self, name: str) -> str:
        return self.shell(f"getprop {name}", check=False).strip()

    def run_as(self, pkg: str, script: str) -> str:
        """以应用身份执行只读命令。正式包没有调试标记时会失败，调用方再改走界面。"""
        quoted = script.replace('"', '\\"')
        return self.shell(f'run-as {pkg} sh -c "{quoted}"', check=False)

    def package_debuggable(self, pkg: str) -> bool:
        output = self.shell(f"dumpsys package {pkg}", check=False)
        return "DEBUGGABLE" in output

    def package_version(self, pkg: str) -> tuple[bool, str]:
        output = self.shell(f"dumpsys package {pkg}", check=False)
        if "Unable to find package" in output or not output.strip():
            return False, ""
        match = re.search(r"versionName=([^\s]+)", output)
        installed = "Package [" in output or "versionName=" in output
        return installed, match.group(1) if match else ""

    def battery(self) -> tuple[int | None, bool]:
        return parse_battery(self.shell("dumpsys battery", check=False))

    def bluetooth_on(self) -> bool | None:
        """读系统开关。返回 None 表示读不到，不能当成已关闭。"""
        raw = self.shell("settings get global bluetooth_on", check=False).strip()
        if raw in {"0", "1"}:
            return raw == "1"
        dump = self.shell("dumpsys bluetooth_manager", check=False)
        if "state: ON" in dump or "enabled: true" in dump:
            return True
        if "state: OFF" in dump or "enabled: false" in dump:
            return False
        return None

    def wifi_on(self) -> bool | None:
        raw = self.shell("settings get global wifi_on", check=False).strip()
        if raw in {"0", "1"}:
            return raw == "1"
        dump = self.shell("dumpsys wifi", check=False)
        if "Wi-Fi is enabled" in dump:
            return True
        if "Wi-Fi is disabled" in dump:
            return False
        return None

    def screen_on_state(self) -> bool | None:
        power = self.shell("dumpsys power", check=False)
        if not power.strip():
            return None
        return "Display Power: state=ON" in power or "mWakefulness=Awake" in power

    def foreground(self) -> str:
        output = self.shell("dumpsys activity activities", check=False)
        for line in output.splitlines():
            if "topResumedActivity" in line or "mResumedActivity" in line:
                return line.strip()
        return ""

    def pid_of(self, pkg: str) -> str:
        output = self.shell(f"pidof {pkg}", check=False).strip()
        return output.split()[0] if output else ""

    def window_size(self) -> tuple[int, int]:
        output = self.shell("wm size", check=False)
        match = re.search(r"(\d+)x(\d+)", output)
        if not match:
            return 1080, 1920
        return int(match.group(1)), int(match.group(2))

    def screencap(self, remote: str = "/sdcard/ota_lab.png") -> str:
        self.shell(f"screencap -p {remote}")
        return remote

    def pull(self, remote: str, local: str) -> str:
        return self._cmd(["pull", remote, local])

    def snapshot(self, pkg: str) -> DeviceInfo:
        info = DeviceInfo(serial=self.serial)
        info.model = self.getprop("ro.product.model")
        info.android = self.getprop("ro.build.version.release")
        info.battery_level, info.charging = self.battery()
        info.app_installed, info.app_version = self.package_version(pkg)
        return info
