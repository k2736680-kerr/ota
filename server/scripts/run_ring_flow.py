"""Da Ring 真机升级。

路径：我的 → 固件升级 → 固件升级。
升级页没有百分比，进度只在 logcat 的 ring_tag：`onProgressChanged: 12`。
按钮文案是「更新到最新版本」。没连上或后台没包时，这个按钮不出现。

进 DFU 时手机会短暂掉连，App 会弹「未找到设备，请打开蓝牙绑定设备后重试」。
这是重连过程，不是失败。点掉「好的」后继续看进度，App 自己会再连上接着传。
"""

from __future__ import annotations

import re
import time

from server.common.config_loader import load_link
from server.drivers.adb import AdbDriver
from server.drivers.phone_ui import PhoneSession, label_connected
from server.drivers.ui_cache import UiCache
from server.scripts.run_dafit_flow import RETRYABLE_CODES, DafitFlow, _percent_label

UPGRADE_ACTIVITY = "FirmwareUpgradeActivity"
MENU_ACTIVITY = "FirmwareActivity"
LOST_DIALOG = "未找到设备"
# 戒指没绑定时 App 的「添加设备」页文案。看到就说明得重新配对，不是等连接。
UNBOUND_TEXTS = ("添加设备", "请从下面的列表中选择您的设备")


def run_ring_case(device, folder, case_id: str, settings=None, logger=None, stop_check=None) -> dict:
    return RingFlow(device, folder, settings, logger, stop_check=stop_check).run(case_id)


class RingFlow(DafitFlow):
    def __init__(self, device, folder, settings=None, logger=None, stop_check=None) -> None:
        super().__init__(device, folder, settings, logger, stop_check=stop_check)
        self.link = load_link("ring")
        self.pkg = self.link["pkg"]
        serial = getattr(device, "serial", "") or self.adb.serial
        self.adb = AdbDriver(self.settings.get("adb_path") or "adb", serial, timeout=40)
        self.cache = UiCache(device, ttl=float(self.settings.get("ui_cache_ttl") or 2.0))
        self.session = PhoneSession(device, self.adb, self.cache)
        locators = self.link.get("locators") or {}
        self.entry_text = "固件升级"
        self.button_text = "更新到最新版本"
        # 成功弹窗按钮是「好的」；「完成」只是兜底。
        self.done_text = "好的"
        self.button_id = _rid(locators.get("upgrade_button"), f"{self.pkg}:id/btn_upgrade")
        self.menu_upgrade_id = _rid(locators.get("firmware_menu_upgrade"), f"{self.pkg}:id/tv_firmware_upgrade")
        self.mine_id = _rid(locators.get("device_tab"), f"{self.pkg}:id/navigation_user")
        self.state_id = _rid(locators.get("upgrade_state"), f"{self.pkg}:id/tv_current_state")
        # 版本优先读页面「当前版本:…」文案；resourceId 只作缺文案时的兜底。
        self.version_id = _rid(locators.get("version_field"), "")
        self.running_texts = list(self.link.get("running_texts") or ["正在固件升级"])
        self.failure_texts = list(self.link.get("failure_texts") or ["升级失败"])
        self.connected_words = list(self.link.get("ui_connected") or ["已连接"])
        self.disconnected_words = list(self.link.get("ui_disconnected") or ["未连接"])
        # 进页时已经在升级。_click_upgrade 不能再点一次，点第二次会把这次传打断。
        self._adopted = False
        # 本轮从什么时间点开始看日志。每次真正发起升级都会重置。
        self._since_stamp = _now_stamp()
        self._log_cache = None
        # 父类构造时按手表 link 走过；戒指换了 link，下一轮 run 时要重新识别。
        self._identity_bound = False

    def _bind_device_identity(self) -> None:
        """戒指优先读「我的」页控件；GATT 在升级后常被清掉，不能当唯一来源。"""
        if getattr(self, "_identity_bound", False):
            return
        from server.common.device_identity import apply_identity, resolve_identity

        def read_text(locator):
            rid = _rid(locator, "")
            if not rid:
                return ""
            return self._text_of_id(rid) or ""

        try:
            identity = resolve_identity(self.link, adb=self.adb, read_text=read_text)
            if not identity.get("mac") and not identity.get("name"):
                self.session.prepare_for_app(self.pkg, allow_relaunch=True)
                self._click_mine()
                identity = resolve_identity(self.link, adb=self.adb, read_text=read_text)
        except Exception as exc:
            self._say(f"识别设备失败: {exc}")
            self._identity_bound = True
            return
        apply_identity(self.link, identity)
        self._identity_bound = True
        name = identity.get("name") or ""
        mac = identity.get("mac") or ""
        if name or mac:
            self._say(f"识别设备 {name or '?'} {mac}（来源 {identity.get('source')}）")

    def _run(self, case: dict, started: float) -> dict:
        """父类的收口逻辑，额外把「没绑定」判成环境不满足。"""
        row = super()._run(case, started)
        if row.get("code") in {"FAIL_UPGRADE", "HUNG_DEVICE"} and self._unbound():
            row["code"] = "ABORT_ENV"
            row["note"] = "戒指已从 App 解绑（显示「添加设备」），请先在 App 里重新配对"
            self._save_log("unbound")
        return row

    def _touched_firmware(self) -> bool:
        """本轮有没有真的开始往戒指里写固件。

        看两个硬信号：出现过任意一次 onProgressChanged（哪怕 0%），
        或者戒指已经进 bootloader。两者任一成立就说明写操作发生过。
        比「点过升级」更准：页面没进对、按钮没点上时不该阻止重试。
        """
        text = self._ring_log(1200)
        return "onProgressChanged:" in text or "onBootLoaderState：true" in text
    def _open_ready(self) -> str:
        """进升级页。已经在传的话，这一轮就盯着这一次，不再等它结束又点第二次。"""
        running = self._phase() == "running"
        self.session.prepare_for_app(self.pkg, allow_relaunch=not running)
        if self._unbound():
            self._say(f"[{self.case_id} {self.case_name}] 戒指已从 App 解绑，需要重新配对")
            return "unbound"
        phase = self._open_upgrade()
        if phase == "running":
            self._adopted = True
            self._say(f"[{self.case_id} {self.case_name}] 升级已经在进行，这一轮直接盯着这次")
            return "ready"
        if phase in {"success", "failure"}:
            self._click_done()
            time.sleep(0.8)
            phase = self._open_upgrade()
            if phase == "running":
                self._adopted = True
                return "ready"
        return phase

    def _open_upgrade(self) -> str:
        if self._on_upgrade_activity():
            phase = self._resolve_phase()
            self._note_announced()
            return phase
        if not self._on_firmware_menu():
            self._back_to_tabs(leave_upgrade=True)
            self._click_mine()
            # 戒指被解绑后 App 会回到「添加设备」页，这时等多久都不会变成已连接。
            # 直接判环境不满足，别空耗 reconnect_timeout。
            if self._unbound():
                self._say(f"[{self.case_id} {self.case_name}] 戒指已从 App 解绑，需要重新配对")
                return "unbound"
            reconnect = int(self.settings.get("reconnect_timeout") or 180)
            if not self._wait_connected(reconnect):
                text = (self._connect_text() or "").strip()
                # 杀 App 后常见长时间「连接中…」：再给一段缓冲，别立刻当 absent。
                if "连接中" in text:
                    self._say(
                        f"[{self.case_id} {self.case_name}] 仍显示连接中，再等 60 秒"
                    )
                    if not self._wait_connected(60):
                        text = (self._connect_text() or "").strip()
                        if "连接中" in text:
                            return "absent"
                        if self._unbound():
                            return "unbound"
                        return "disconnected"
                elif self._unbound():
                    return "unbound"
                else:
                    return "disconnected"
            if not self._reveal(self.entry_text):
                return "absent"
            node = self._exact_node(self.entry_text)
            if node is None:
                return "absent"
            node.click()
            self.cache.mark_stale()
            time.sleep(1.2)
        if not self._click_menu_upgrade():
            return "absent"
        phase = self._resolve_phase()
        self._note_announced()
        return phase

    def _resolve_phase(self) -> str:
        """按钮和「已是最新」都要等连接拉完版本后再定。"""
        deadline = time.time() + 12
        last = "other"
        while time.time() < deadline:
            if self.stop_check():
                return last
            last = self._phase()
            if last in {"ready", "running", "success", "failure", "need_charge"}:
                return last
            if last == "latest":
                time.sleep(1.5)
                confirm = self._phase()
                if confirm == "latest":
                    return "latest"
                last = confirm
                if last in {"ready", "running"}:
                    return last
            time.sleep(1)
        return last

    def _click_upgrade(self) -> None:
        if self._adopted or self._phase() == "running":
            self._adopted = False
            # 已经在跑的这一轮同样要定基准。T04/T05 打断后回来时页面还在「正在固件升级」，
            # 走的就是这条路；不重置的话，上一次的 0% 会被算进这一次，立刻误判卡死。
            self._mark_dfu_baseline()
            self._say(f"[{self.case_id} {self.case_name}] 不重复点击，升级还在跑")
            return
        node = self._node_by_id(self.button_id) or self._exact_node(self.button_text)
        if node is None:
            raise RuntimeError("升级页没有「更新到最新版本」按钮")
        node.click()
        self._say(f"[{self.case_id} {self.case_name}] 点击更新到最新版本")
        self._mark_dfu_baseline()
        self.cache.mark_stale()
        time.sleep(2)

    def _phase(self) -> str:
        # 屏幕灭着时 uiautomator 只能抓到没有 App 文案的空页面，
        # 后面每条判断都会失真。手表那边已经踩过一次，戒指同样要先点亮再判。
        self._ensure_screen_on()
        self._dismiss_lost_dialog()
        page = self.cache.page()
        state = self._state_text()
        blob = f"{state}\n{page}"
        if "请给设备充电" in blob or "电量不足" in blob:
            return "need_charge"
        if any(word and word in blob for word in self.failure_texts):
            return "failure"
        if any(word in blob for word in ("升级成功", "升级完成", "更新成功", "更新完成")):
            return "success"
        if self._ui_running(blob):
            # 页面确实写着「正在固件升级」，但还要确认它真的在往前走。
            # 这两种情况页面都一样，只能从 ring_tag 日志里看出来。
            if self._dfu_failed():
                return "failure"
            if self._spinning():
                return "failure"
            return "running"
        if self.button_text in blob or self._node_by_id(self.button_id) is not None:
            return "ready"
        if self._on_upgrade_activity() and "当前版本" in blob:
            return "latest"
        if self.pkg not in (self.cache.package() or self.pkg):
            return "other"
        return "other"

    def _percent(self) -> int | None:
        """页面上没有百分号。只在升级文案出现时，读 ring_tag 里最后一次进度。"""
        if not self._ui_running(f"{self._state_text()}\n{self.cache.cached_page() or ''}"):
            page = self.cache.page()
            if not self._ui_running(f"{self._state_text()}\n{page}"):
                return None
        found = re.findall(r"onProgressChanged:\s*(\d+)", self._ring_log())
        if not found:
            return None
        return int(found[-1])

    def _ring_log(self, lines: int = 2000) -> str:
        """同一次判定里多处要读日志，1.5 秒内复用同一份，别每处都起一次 adb。

        行数给得比实际需要大：一次完整升级会刷上百条 onProgressChanged，
        `-t 800` 这种窗口在日志密的时候可能连一轮都装不下，计数就会漏。
        """
        now = time.time()
        cached = getattr(self, "_log_cache", None)
        if cached and now - cached[0] <= 1.5 and cached[1] >= lines:
            return cached[2]
        text = self.adb.shell(f"logcat -d -t {lines} -s ring_tag:D", check=False) or ""
        self._log_cache = (now, lines, text)
        return text

    def _dfu_failed(self) -> bool:
        """App 自己报 DFU 失败。页面不一定同步刷新，但这一轮已经没救了。

        2026-11-12：升级在 5% 被切走后报 `OtaRestoreManager upgrade error:
        DFU process failed.`，页面仍显示「正在固件升级」。只看页面就会一直等下去。
        只看「本次升级开始之后」新增的那些，历史日志不能让每一轮都判失败。
        """
        for stamp in _dfu_failure_stamps(self._ring_log()):
            if stamp >= self._since_stamp:
                return True
        return False

    def _mark_dfu_baseline(self) -> None:
        """记下「本次升级从这一刻开始」。之后所有判据都只看这个时间点之后的行。

        每次真正开始一次升级（包括 T04/T05 打断后重新点升级）都必须调用一次，
        否则上一轮的历史日志会算进这一轮，把正常重试误判成卡死。
        """
        self._since_stamp = _now_stamp()
        self._log_cache = None

    def _spinning(self) -> bool:
        """在同一个低进度上反复重来，说明这一轮不可能自己走完。

        2026-11-12 实测：戒指每升到 5% 就 BLE 超时断连（dumpsys 里 reason=8，HCI 超时），
        App 重连后从 0% 重传，三轮下来最高只到 5%。页面始终「正在固件升级」，
        进度也确实在变，三路进展信号都判不出异常，只能靠「从头来过几次」识别。

        0% 出现 1 次是正常起传，4 次说明已经空转三轮，该收口了。
        按时间戳统计，不看 `logcat -t N` 的滚动窗口——窗口会把早期的 0% 滚掉，
        让这个检测静默失效。
        """
        return self._restart_count() >= 4

    def _restart_count(self) -> int:
        starts = [
            stamp for stamp, percent in _progress_stamps(self._ring_log())
            if percent == 0 and stamp >= self._since_stamp
        ]
        return len(starts)


    def _version_text(self) -> str:
        """从「当前版本:USY-I403-1.1.9」里取出完整版本号，不绑 resourceId。"""
        from server.common.version_util import ring_version_from_page

        page = ""
        try:
            page = self.cache.page() or ""
        except Exception:
            page = ""
        token = ring_version_from_page(page, pkg=self.pkg)
        if token:
            return token
        if self.version_id:
            text = self._text_of_id(self.version_id) or ""
            from server.common.version_util import firmware_token

            return firmware_token(text)
        return ""

    def _click_done(self) -> None:
        """戒指成功页是弹窗「升级成功!」+「好的」，不是手表那种「完成」按钮。

        2026-11-16 全量：每轮 T01 结果截图都停在这张弹窗上。父类只点「完成」，
        弹窗一直挡着，后面只好空等再强制重启 App。这里先点「好的」。
        """
        if self._dismiss_success_dialog():
            return
        super()._click_done()

    def _dismiss_success_dialog(self) -> bool:
        page = self.cache.cached_page()
        if page is None:
            page = self.cache.page()
        blob = page or ""
        if not any(word in blob for word in ("升级成功", "更新成功", "升级完成", "更新完成")):
            return False
        node = self._exact_node("好的") or self._exact_node("完成")
        if node is None:
            return False
        try:
            node.click()
        except Exception:
            return False
        self._say(f"[{self.case_id} {self.case_name}] 已关掉升级成功弹窗")
        self.cache.mark_stale()
        time.sleep(0.8)
        return True

    def _before_version_text(self) -> str:
        return self._version_text()

    def _verify_firmware_applied(self, code: str, note: str) -> tuple[str, str]:
        """页面判成功后，只补充版本备注，不因版本不变改判。

        同版本循环本来就不要求固件号变化：跑完显示完成、能重连 App 即成功。
        收尾后停在「我的」页，升级页上的版本控件不在；能再读到就写进备注。
        """
        if code != "PASS":
            return code, note
        before = getattr(self, "before_version", "") or ""
        if not before:
            return code, f"{note}（固件版本未校验：升级前没读到版本）"
        after = self._version_text() or self._read_version_on_upgrade_page()
        if not after:
            return code, f"{note}（固件版本未校验：升级后没读到版本）"
        if before == after:
            return code, f"{note}；同版本循环，版本仍为 {after}"
        return code, f"{note}；固件已从 {before} 更新到 {after}"

    def _read_version_on_upgrade_page(self) -> str:
        """从「我的」再进升级页读当前版本。失败就返回空，不把收尾搞砸。"""
        try:
            phase = self._open_upgrade()
            if phase in {"unbound", "disconnected", "absent"}:
                return ""
            text = self._version_text() or ""
            # 读完退回，别把下一轮留在升级页上。
            self._click_done()
            if self._on_upgrade_activity() or self._on_firmware_menu():
                self._back_to_tabs(leave_upgrade=True)
            return text
        except Exception:
            return ""

    def _note_announced(self) -> None:
        if self.announced:
            return
        found = re.findall(r"version='([^']+)'", self._ring_log(400))
        if found:
            self.announced = found[-1]

    def _ui_running(self, blob: str) -> bool:
        return any(word and word in (blob or "") for word in self.running_texts)

    def _dismiss_lost_dialog(self) -> bool:
        """DFU 掉连时的「未找到设备」。点掉即可，不能当成升级失败。"""
        page = self.cache.cached_page()
        if page is None:
            page = self.cache.page()
        if LOST_DIALOG not in (page or ""):
            return False
        node = self._exact_node("好的")
        if node is None:
            return False
        try:
            node.click()
        except Exception:
            return False
        self._say(f"[{self.case_id} {self.case_name}] 戒指重连中，已关掉「未找到设备」")
        self.cache.mark_stale()
        time.sleep(0.6)
        return True

    def _wait_connected(self, seconds: int) -> bool:
        """等戒指回连。已经在「我的」页就只读文字，绝不重复点 tab。

        2026-11-12 踩过：这里原来每 2 秒点一次「我的」，升级跑到 5% 时把 App
        从 FirmwareUpgradeActivity 切走，DFU 当场报 `DFU process failed.`,
        重连后又从 0% 重来，看起来就像永远跑不完。
        """
        deadline = time.time() + seconds
        last_say = 0.0
        while time.time() < deadline:
            if self.stop_check():
                return False
            # 只有不在「我的」页时才切一次，且升级进行中一律不切。
            if not self._on_mine_page() and not self._upgrade_running():
                self._click_mine()
            text = (self._connect_text() or "").strip()
            if label_connected(text, self.connected_words, self.disconnected_words) is True:
                self._say(f"[{self.case_id} {self.case_name}] 戒指已连接")
                return True
            if time.time() - last_say >= 30:
                self._say(f"[{self.case_id} {self.case_name}] 等戒指连接（当前 {text or '读不到'}）")
                last_say = time.time()
            time.sleep(2)
        return False

    def _on_mine_page(self) -> bool:
        return self._exists_id(_rid(self.link["locators"].get("device_name"), f"{self.pkg}:id/tv_user_device_name"))

    def _unbound(self) -> bool:
        """App 里已经没有绑定的戒指。表现为「添加设备」页，或「我的」页没有设备卡片。"""
        page = self.cache.cached_page()
        if page is None:
            page = self.cache.page()
        if UNBOUND_TEXTS and any(word in (page or "") for word in UNBOUND_TEXTS):
            return True
        # 在「我的」页却读不到设备卡片，也是没绑。
        if self._exists_id(self.mine_id) and not self._on_mine_page():
            return True
        return False

    def _upgrade_running(self) -> bool:
        if self._on_upgrade_activity():
            return True
        return "正在固件升级" in (self._state_text() or "")

    def _click_mine(self) -> None:
        node = self._node_by_id(self.mine_id)
        if node is None:
            return
        try:
            node.click()
        except Exception:
            return
        self.cache.mark_stale()
        time.sleep(0.5)
        self._refresh_identity_from_ui()

    def _refresh_identity_from_ui(self) -> None:
        """「我的」页有设备名/MAC 控件时，用页面值覆盖 GATT 识别结果。

        有的戒指只在 App 里显示名字，dumpsys Bonded 列表里没有可读名称。
        """
        from server.common.device_identity import apply_identity, normalize_mac

        name = (self._text_of_id(_rid(self.link["locators"].get("device_name"), f"{self.pkg}:id/tv_user_device_name")) or "").strip()
        mac = normalize_mac(self._text_of_id(_rid(self.link["locators"].get("device_mac"), f"{self.pkg}:id/tv_user_device_address")) or "")
        if not name and not mac:
            return
        before = (self.link.get("device_name") or "", self.link.get("device_mac") or "")
        apply_identity(self.link, {"name": name, "mac": mac})
        after = (self.link.get("device_name") or "", self.link.get("device_mac") or "")
        if after != before and (after[0] or after[1]):
            self._say(f"[{self.case_id} {self.case_name}] 页面设备 {after[0] or '?'} {after[1]}（来源 ui）")

    def _click_menu_upgrade(self) -> bool:
        node = self._node_by_id(self.menu_upgrade_id)
        if node is None:
            return False
        try:
            node.click()
        except Exception:
            return False
        self.cache.mark_stale()
        time.sleep(1.2)
        return True

    def _on_upgrade_activity(self) -> bool:
        return self.cache.activity().endswith(UPGRADE_ACTIVITY)

    def _on_firmware_menu(self) -> bool:
        activity = self.cache.activity()
        return activity.endswith(MENU_ACTIVITY) and not activity.endswith(UPGRADE_ACTIVITY)

    def _on_device_page(self) -> bool:
        if self._on_upgrade_activity() or self._on_firmware_menu():
            return False
        return self._exists_id(_rid(self.link["locators"].get("device_name"), f"{self.pkg}:id/tv_user_device_name")) or bool(
            self._connect_text()
        )

    def _state_text(self) -> str:
        return self._text_of_id(self.state_id) or ""

    def _node_by_id(self, rid: str):
        if not rid:
            return None
        try:
            node = self.device(resourceId=rid)
        except Exception:
            return None
        return node if getattr(node, "exists", False) else None

    def _goto_entry(self) -> None:
        """父类在页面走丢时会调用。升级进行中不能被这里带回菜单。"""
        if self._phase() == "running":
            return
        self._open_upgrade()


def _rid(locator, default: str) -> str:
    if not isinstance(locator, dict):
        return default
    value = locator.get("value")
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        return value.get("resourceId") or default
    return default


# logcat 每行前缀是 `MM-DD HH:MM:SS.mmm`。判据都按这个时间戳切，
# 不靠 `logcat -t N` 的滚动窗口——窗口大小会随日志密度变化，计数并不可靠。
_LOG_STAMP = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d+)", re.M)


def _now_stamp() -> str:
    return time.strftime("%m-%d %H:%M:%S")


def _progress_stamps(text: str) -> list[tuple[str, int]]:
    """取出每条 `onProgressChanged: N` 的时间戳和进度值，按出现顺序。"""
    found: list[tuple[str, int]] = []
    for line in (text or "").splitlines():
        match = re.search(r"onProgressChanged:\s*(\d+)", line)
        if not match:
            continue
        stamp = _line_stamp(line)
        if stamp:
            found.append((stamp, int(match.group(1))))
    return found


def _dfu_failure_stamps(text: str) -> list[str]:
    """取出每条 `DFU process failed` 的时间戳。"""
    stamps: list[str] = []
    for line in (text or "").splitlines():
        if "DFU process failed" not in line:
            continue
        stamp = _line_stamp(line)
        if stamp:
            stamps.append(stamp)
    return stamps


def _line_stamp(line: str) -> str:
    match = _LOG_STAMP.match(line or "")
    return match.group(1) if match else ""

