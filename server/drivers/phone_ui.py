"""手机系统界面操作。

各品牌分辨率、手势导航不同，这里只做三件和 App 无关的事：
收起通知栏、锁屏时从屏幕偏下位置上滑、在 App 内部滚动。
不从上往下滑动，避免把通知栏拉下来。
"""

from __future__ import annotations

import time

from server.drivers.ui_cache import UiCache


def screen_is_on(power_dump: str) -> bool:
    text = power_dump or ""
    if "Display Power: state=ON" in text or "mWakefulness=Awake" in text:
        return True
    return "mHoldingDisplaySuspendBlocker=true" in text


def keyguard_blocking(window_dump: str) -> bool:
    text = window_dump or ""
    # OPPO 用 isKeyguardShowing。先看 false，避免 true 是 false 的子串误判。
    if "isKeyguardShowing=false" in text:
        return False
    if "isKeyguardShowing=true" in text:
        return True
    locked = any(item in text for item in ("mShowingLockscreen=true", "isStatusBarKeyguard=true", "mDreamingLockscreen=true"))
    unlocked = any(item in text for item in ("mShowingLockscreen=false", "isStatusBarKeyguard=false", "mDreamingLockscreen=false"))
    if unlocked:
        return False
    return locked


def label_connected(text: str, connected: list[str], disconnected: list[str]) -> bool | None:
    """设备页连接文案。先认「未连接」，避免被「已连接」这三个字误伤。状态栏「蓝牙已连接」对不上。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    for word in disconnected:
        if word and word in cleaned:
            return False
    for word in connected:
        if word and cleaned == word:
            return True
    return None


def wearable_charging_from_page(page: str, pkg: str, charging_texts: list[str] | None = None) -> str:
    """穿戴设备是否在充电：只看 App 自己的页面文案/content-desc，不看手机 dumpsys。

    「请给设备充电」是电量不够的提示，不是正在充电，不能当成命中。
    返回命中的那句文案；没有就返回空串。
    """
    phrases = [str(item).strip() for item in (charging_texts or []) if str(item).strip()]
    if not phrases:
        phrases = ["充电中", "正在充电", "已充满"]
    from server.common.version_util import _page_texts

    for text in _page_texts(page or "", pkg=pkg or None):
        cleaned = (text or "").strip()
        if not cleaned:
            continue
        if "请给设备充电" in cleaned or "电量不足" in cleaned:
            continue
        for phrase in phrases:
            if phrase and phrase in cleaned:
                return phrase
    return ""


def inject_due(
    percent: int | None,
    page: str,
    keywords: list[str],
    on_upgrade_page: bool,
    elapsed_on_page: float,
    threshold: int,
    fallback: float,
) -> bool:
    """进度数字已经显示时，必须到阈值才注入。读不到数字时，才用兜底时间。"""
    if not on_upgrade_page:
        return False
    if percent is not None:
        return percent >= threshold
    if elapsed_on_page < fallback:
        return False
    return True


def upgrade_final(case: dict | None) -> str:
    return str(((case or {}).get("expect") or {}).get("upgrade_final") or "success").lower()


def allows_retry(case: dict | None, link: dict | None = None) -> bool:
    """干扰可以打断这一次，但随后必须重新点升级并成功。

    杀进程、断蓝牙对三条链路都是这样。关 Wi-Fi 只对 wifi_upgrade_links 里的链路允许
    （目前是眼镜：固件同时走蓝牙和 Wi-Fi）。手表、戒指只走蓝牙，这一次也不能失败。
    """
    final = upgrade_final(case)
    if final in {"retry_success", "interrupted", "reverse", "survive"}:
        return True
    if final != "success":
        return False
    actions = [step.get("action") for step in (case or {}).get("inject") or []]
    if "wifi_off" not in actions:
        return False
    expect = (case or {}).get("expect") or {}
    named = [str(item) for item in expect.get("wifi_upgrade_links") or []]
    link_id = str((link or {}).get("id") or "")
    if link_id:
        return link_id in named
    return str((link or {}).get("transport") or "").lower() == "wifi"


def case_direction(case: dict | None, link: dict | None = None) -> str:
    """retry：这一次可以失败，重试必须成功。forward：这一次必须成功。"""
    return "retry" if allows_retry(case, link) else "forward"


def switch_action(name: str) -> tuple[str, str] | None:
    """注入动作对应哪个系统开关。返回 (读法, 期望值)。"""
    table = {
        "bt_off": ("bluetooth_on", "False"),
        "bt_on": ("bluetooth_on", "True"),
        "wifi_off": ("wifi_on", "False"),
        "wifi_on": ("wifi_on", "True"),
        "screen_off": ("screen_on_state", "False"),
        "screen_on": ("screen_on_state", "True"),
    }
    return table.get(name)


def expected_switch(expect: str) -> bool:
    return str(expect).lower() == "true"


def direction_label(direction: str) -> str:
    return "可重试" if direction == "retry" else "必须成功"


def pass_rule(direction: str, case: dict | None = None, link: dict | None = None) -> str:
    if direction == "retry":
        actions = [step.get("action") for step in (case or {}).get("inject") or []]
        if "kill" in actions:
            return "杀掉会打断这一次。重新点击升级后必须成功，并且设备页已连接"
        if "bt_off" in actions:
            return "断蓝牙会打断这一次。重新点击升级后必须成功，并且设备页已连接"
        if "wifi_off" in actions:
            return "固件走 Wi-Fi，关闭会打断这一次。重新点击升级后必须成功，并且设备页已连接"
        return "这一次可以失败。重新点击升级后必须成功，并且设备页已连接"
    actions = [step.get("action") for step in (case or {}).get("inject") or []]
    if "wifi_off" in actions and str((link or {}).get("transport") or "").lower() != "wifi":
        return "固件走蓝牙，关 Wi-Fi 不能失败。升级必须成功，并且设备页已连接"
    return "升级必须成功，并且设备页已连接"


def classify_round(outcome: str, link_state: str, direction: str = "forward") -> str:
    """收口时只看最终这一次。页面走丢不是死机。"""
    if outcome == "success" and link_state == "connected":
        return "PASS"
    if link_state == "disconnected":
        return "HUNG_DEVICE"
    return "FAIL_UPGRADE"


def round_note(outcome: str, detail: str, link_state: str, direction: str = "forward") -> str:
    """写这一轮的结论。

    失败的**原因**必须放在最前面，收尾状态放在后面。
    2026-11-13 手表 T08：升级本身是「固件升级失败」（息屏打断 DFU，真实的设备问题），
    但收尾时没切回设备页，旧文案会写成「失败：固件升级失败，没有回到设备页」，
    读起来像是工具没导航好，把设备侧的缺陷盖掉了。
    现在写成「失败：固件升级失败（设备页状态未确认）」——原因在前，收尾在后。
    """
    tail = {
        "connected": "设备页已连接",
        "disconnected": "设备页未连接",
        "unseen": "设备页状态未确认",
    }.get(link_state, "设备页状态未确认")
    if direction == "retry":
        if outcome == "success" and link_state == "connected":
            return "通过：干扰后重新升级成功，设备页已连接"
        if outcome == "success":
            return f"失败：重新升级成功后{tail}"
        return f"失败：重新升级没有成功（{detail}），{tail}"
    if outcome == "success" and link_state == "connected":
        return "通过：升级成功且设备页已连接"
    if outcome == "success":
        return f"失败：升级成功后{tail}"
    return f"失败：{detail}（{tail}）"


class PhoneSession:
    """包一层 adb。有 AdbDriver / FakeAdb 时走它，单测不会摸到真机。"""

    def __init__(self, device, adb, cache: UiCache | None = None) -> None:
        self.device = device
        self.adb = adb
        self.cache = cache or UiCache(device)
        self._last_wake = 0.0

    def collapse(self) -> None:
        output = self.adb.shell("cmd statusbar collapse", check=False)
        lowered = (output or "").lower()
        if "unknown command" in lowered or "not found" in lowered or "no shell command" in lowered:
            self.adb.shell("service call statusbar 2", check=False)
        self.cache.mark_stale()

    def wake_and_unlock(self) -> None:
        self.collapse()
        power = self.adb.shell("dumpsys power", check=False)
        if not screen_is_on(power):
            self.adb.shell("input keyevent 224", check=False)
            time.sleep(0.4)
        window = self.adb.shell("dumpsys window", check=False)
        if keyguard_blocking(window):
            # 这台 OPPO 锁屏写着「上滑解锁」，手势要从屏幕最下方起滑。0.82 的位置滑不开。
            self._swipe_up(0.92, 0.32)
            time.sleep(0.4)
        self.collapse()
        self._last_wake = time.time()

    def screen_off(self) -> None:
        """只关屏幕。用休眠键，不用电源键，避免弹出关机菜单或把调试连接弄掉。

        不要指望 stay_on_while_plugged_in：这台 OPPO 上 `settings put` 被
        WRITE_SECURE_SETTINGS 挡住（实测一直 SecurityException），写了也不生效，
        真正让屏幕灭掉的就是这一个 223 键。所以恢复只靠 screen_on 发 224。
        """
        self.adb.shell("input keyevent 223", check=False)
        self.cache.mark_stale()

    def screen_on(self) -> None:
        """点亮并解锁。不改蓝牙，也不动系统设置。"""
        self.adb.shell("input keyevent 224", check=False)
        time.sleep(0.4)
        self.wake_and_unlock()

    def set_bluetooth(self, enable: bool) -> bool:
        """部分 ColorOS/Android 11 把 svc bluetooth disable 拦掉了。命令无效时走设置页开关。"""
        command = "svc bluetooth enable" if enable else "svc bluetooth disable"
        self.adb.shell(command, check=False)
        if self._wait_switch("bluetooth_on", enable, 5):
            return True
        if self._bluetooth_via_settings(enable):
            return True
        return False

    def set_wifi(self, enable: bool) -> bool:
        command = "svc wifi enable" if enable else "svc wifi disable"
        self.adb.shell(command, check=False)
        if self._wait_switch("wifi_on", enable, 5):
            return True
        if self._wifi_via_settings(enable):
            return True
        return False

    def _wait_switch(self, reader: str, expected: bool, seconds: float = 5) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            actual = getattr(self.adb, reader, lambda: None)()
            if actual is expected:
                return True
            time.sleep(1)
        actual = getattr(self.adb, reader, lambda: None)()
        return actual is expected

    def _open_settings(self, action: str) -> None:
        self.wake_and_unlock()
        self.adb.shell(f"am start -a {action}", check=False)
        time.sleep(2.5)

    def _bluetooth_via_settings(self, enable: bool) -> bool:
        self._open_settings("android.settings.BLUETOOTH_SETTINGS")
        switch = self._switch_widget(
            "com.coloros.wirelesssettings:id/switchWidget",
            "com.android.settings:id/switchWidget",
            "com.android.settings:id/switch_widget",
        )
        if switch is None:
            return False
        try:
            if switch.exists:
                switch.click()
            else:
                return False
        except Exception:
            return False
        if not self._wait_switch("bluetooth_on", enable, 8):
            return False
        self.adb.shell("input keyevent 4", check=False)
        time.sleep(0.5)
        return True

    def _wifi_via_settings(self, enable: bool) -> bool:
        self._open_settings("android.settings.WIFI_SETTINGS")
        switch = self._switch_widget(
            "com.coloros.wirelesssettings:id/switchWidget",
            "com.android.settings:id/switchWidget",
            "com.android.settings:id/switch_widget",
        )
        if switch is None:
            return False
        try:
            if switch.exists:
                switch.click()
            else:
                return False
        except Exception:
            return False
        if not self._wait_switch("wifi_on", enable, 8):
            return False
        self.adb.shell("input keyevent 4", check=False)
        time.sleep(0.5)
        return True

    def _switch_widget(self, *resource_ids: str):
        for rid in resource_ids:
            try:
                node = self.device(resourceId=rid)
                if getattr(node, "exists", False):
                    return node
            except Exception:
                continue
        return None

    def keep_awake(self) -> None:
        """升级过程中收起通知栏。屏幕本来亮着就不再发按键，避免把息屏用例提前点亮。

        调用方在轮询里每 2~3 秒叫一次，这里两件事都要便宜：
        收起通知栏只在距上次超过 4 秒时做，避免每轮都起一个 adb 进程。
        """
        now = time.time()
        if now - getattr(self, "_last_collapse", 0.0) >= 4:
            self.collapse()
            self._last_collapse = now
        if now - self._last_wake < 8:
            return
        self.wake_and_unlock()

    def foreground(self, pkg: str) -> bool:
        package = self.cache.package()
        if package:
            return package == pkg
        try:
            return bool(self.device(packageName=pkg).exists)
        except Exception:
            return False

    def start_app(self, pkg: str) -> None:
        starter = getattr(self.device, "app_start", None)
        try:
            if callable(starter):
                starter(pkg)
                self.cache.mark_stale()
                return
        except Exception:
            pass
        self.adb.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1", check=False)
        self.cache.mark_stale()

    def prepare_for_app(self, pkg: str, *, allow_relaunch: bool = True) -> None:
        """回到本 App 前台。已经在前台时只花一次前台包名查询，不再整轮重读。

        广告只闪几秒：误点离开后，用现有逻辑拉回目标 App 即可，不必单独点广告关闭键。
        换手机后包名/前台探测偶发滞后：拉回或收起通知后一律 mark_stale，避免读旧前台。
        """
        if self.foreground(pkg) and self.top_package() != "com.android.systemui":
            return
        self.wake_and_unlock()
        self.cache.mark_stale()
        if self.top_package() == "com.android.systemui":
            # 通知栏展开时前台是系统界面。返回键先收起它，避免点到通知。
            self.adb.shell("input keyevent 4", check=False)
            time.sleep(0.3)
            self.collapse()
            self.cache.mark_stale()
        if allow_relaunch and not self.foreground(pkg):
            self.start_app(pkg)
            time.sleep(1.0)
            self.collapse()
            self.cache.mark_stale()

    def top_package(self) -> str:
        return self.cache.package()

    def scroll_to_top(self, pkg: str) -> None:
        """把当前列表拉到最上面。连接状态在设备页顶部，停在「固件升级」那一行时读不到。"""
        node = self._scroller(pkg)
        scroll = getattr(node, "scroll", None) if node is not None and getattr(node, "exists", False) else None
        if scroll is not None:
            try:
                scroll.toBeginning(steps=30)
                time.sleep(0.3)
                return
            except Exception:
                pass
            finally:
                self.cache.mark_stale()
        # 找不到可滚动控件时，从屏幕中部向下拨，把顶部拉出来。
        # 不从状态栏起滑，避免把通知栏拉下来。
        width, height = self._window_size()
        x = max(1, width // 2)
        start = int(height * 0.35)
        end = int(height * 0.72)
        for _ in range(3):
            self.adb.shell(f"input swipe {x} {start} {x} {end} 280", check=False)
            self.cache.mark_stale()
            time.sleep(0.25)

    def scroll_forward(self, pkg: str) -> None:
        node = self._scroller(pkg)
        if node is not None and getattr(node, "exists", False):
            scroll = getattr(node, "scroll", None)
            if scroll is None:
                return
            try:
                scroll.forward()
                time.sleep(0.3)
            except Exception:
                return
            finally:
                self.cache.mark_stale()
            return
        if self.foreground(pkg):
            self._swipe_up()

    def _scroller(self, pkg: str):
        try:
            return self.device(scrollable=True, packageName=pkg)
        except Exception:
            return None

    def _swipe_up(self, start_ratio: float = 0.75, end_ratio: float = 0.45) -> None:
        width, height = self._window_size()
        x = max(1, width // 2)
        start = int(height * start_ratio)
        end = int(height * end_ratio)
        if start <= end:
            start, end = end, start
        self.adb.shell(f"input swipe {x} {start} {x} {end} 300", check=False)
        self.cache.mark_stale()

    def _window_size(self) -> tuple[int, int]:
        cached = getattr(self, "_size", None)
        if cached:
            return cached
        try:
            size = self.adb.window_size()
            if size and size[0] and size[1]:
                self._size = (int(size[0]), int(size[1]))
                return self._size
        except Exception:
            pass
        try:
            size = self.device.window_size()
            self._size = (int(size[0]), int(size[1]))
            return self._size
        except Exception:
            return 1080, 1920
