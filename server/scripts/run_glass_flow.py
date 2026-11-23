"""daecho 真机升级。

升级页同时显示主控和影音，各自按钮没有勾选框。
任务按固件标题匹配按钮，只点被选中的模块。

页面判断分四种，每种的处理完全不同：
- ready   两块固件都有「升级至最新版本」按钮，可以点升级。
- latest  按钮是「已是最新版本」，说明后台这个包已经被装过或没配新包。
          这不是升级失败，也不是正在升级，必须立刻停止任务让人去后台换包，
          否则后面每一条用例都会在十几秒内假失败一遍。
- running 真在升级：有百分比，或者停在升级动画页 DeviceFirmwareActivity。
- failure 页面明确写着升级失败。

进升级页还常遇到两种拦路弹窗，都不算用例失败，先处理掉再判断：
- 「请给设备充电」：眼镜电量或充电状态不满足，点确定后本轮按环境不满足处理。
- App 自身更新弹窗：只点取消，不点下载。
"""

from __future__ import annotations

import re
import time

from server.common.config_loader import load_link
from server.drivers.adb import AdbDriver
from server.drivers.phone_ui import PhoneSession
from server.drivers.ui_cache import UiCache
from server.drivers.progress_watch import ProgressWatch
from server.links.firmware_select import module_labels, selected_modules
from server.links.glass_link import GlassLink
from server.scripts.run_dafit_flow import DafitFlow, _percent_label

# 页面上的固定文案。空页面时这些一个都不会命中，所以判断前要先确认屏幕是亮的。
FIRMWARE_ACTIVITY = "DeviceFirmwareActivity"
CHARGE_POPUP_TITLE = "请给设备充电"
CHARGE_POPUP_OK = "确定"


def run_glass_case(device, folder, case_id: str, settings=None, logger=None, firmware_targets=None,
                   stop_check=None) -> dict:
    return GlassFlow(device, folder, settings, logger, firmware_targets, stop_check=stop_check).run(case_id)


class GlassFlow(DafitFlow):
    def __init__(self, device, folder, settings=None, logger=None, firmware_targets=None,
                 stop_check=None) -> None:
        super().__init__(device, folder, settings, logger, stop_check=stop_check)
        self.link = load_link("glass")
        self.pkg = self.link["pkg"]
        serial = getattr(device, "serial", "") or self.adb.serial
        self.adb = AdbDriver(self.settings.get("adb_path") or "adb", serial, timeout=40)
        # 一次观察内复用页面 dump 和前台包名，避免一次判定读十几遍。
        self.cache = UiCache(device, ttl=float(self.settings.get("ui_cache_ttl") or 2.0))
        self.session = PhoneSession(device, self.adb, self.cache)
        self.targets = list(firmware_targets or ["mcu"])
        self.modules = selected_modules(self.link, self.targets)
        self.runner = GlassLink(self.link, self.adb, self._ui(), logger, sleeper=time.sleep)
        self.runner.set_firmware_targets(self.targets)
        self.connected_words = list(self.link.get("ui_connected") or ["已连接"])
        self.disconnected_words = list(self.link.get("ui_disconnected") or ["未连接"])
        self._clicked_at = 0.0
        self.need_charge = False
        self._screen_lit = False
        # 父类构造时按手表 link 走过；眼镜换了 link，下一轮 run 时要重新识别。
        self._identity_bound = False

    def _ui(self):
        from server.drivers.uia import UiDriver

        return UiDriver(self.device)

    def _announce(self, case: dict) -> None:
        super()._announce(case)
        self._say(f"本次固件 {module_labels(self.modules)}")

    # ---------- 进升级页 ----------

    def _open_ready(self) -> str:
        """把手机带到可以点升级的状态。返回 ready / latest / running / need_charge / absent / disconnected。

        上一轮若还停在「正在升级」/固件 Activity，不能直接 FAIL「上一轮未结束」：
        先等它跑完并回到设备页，再开本轮（对齐手表 _open_ready 的排水逻辑）。
        """
        self.session.prepare_for_app(self.pkg, allow_relaunch=self._phase() != "running")
        self.runner.dismiss_app_update()
        phase = self._phase()
        if phase == "running":
            self._say("[上一轮升级还没结束，先等它跑完再开始本轮]")
            outcome, _detail = self._wait(
                {"id": self.case_id, "inject": [], "trigger": {}},
                allow_inject=False,
            )
            if outcome == "need_charge":
                return "need_charge"
            self._return_home()
            time.sleep(1)
            self.session.prepare_for_app(self.pkg, allow_relaunch=True)
            self.runner.dismiss_app_update()
            phase = self._phase()
            if phase == "running":
                # 排水后仍在升：不能硬开下一轮，交给上层记「上一轮未结束」。
                return "running"
        if phase == "need_charge":
            return phase
        if phase == "latest":
            # 可能是页面没刷新；重启后再看，仍 latest 才交给上层 ABORT。
            # 这里不要走 _ensure_connected 长等：没包时本来就不会连上升级入口。
            self._restart_app()
            self.runner.dismiss_app_update()
            self.runner.open_upgrade()
            return self._resolve_entry()
        if phase == "failure" and self._retry_button_visible():
            return "ready"
        if not self._ensure_connected(90):
            return "disconnected"
        self.runner.open_upgrade()
        phase = self._resolve_entry()
        return phase

    def _resolve_entry(self) -> str:
        """进了升级页再判一次。判不出来就重启 App 重进，还不行才算 absent。

        真实经历：后台明明配了升级包，但 App 停在「已是最新版本」。
        杀掉 App 重新进入绝大多数情况就恢复了，所以先试重启，再下结论。
        """
        for attempt in range(2):
            self._dismiss_charge_popup()
            self.runner.dismiss_app_update()
            phase = self._phase()
            if phase in {"ready", "running", "latest", "need_charge"}:
                return phase
            if self._all_latest():
                return "latest"
            if attempt == 0:
                self._restart_app()
                self.runner.open_upgrade()
        return "absent"

    def _ensure_screen_on(self) -> None:
        """屏幕灭着就直接点亮。

        屏幕关掉时 uiautomator 只能抓到一份没有 App 文案的空页面，任何判断都会失真。
        前晚 T08 息屏后 stay_on 被留成 0，屏幕自己灭掉，后面每条用例都读到黑屏，
        开场就报「上一轮升级未结束」。这里先保证屏幕亮着，再谈判断。
        用例自身的息屏干扰由 _wait 控制，不走这里。
        """
        if self._screen_lit:
            return
        try:
            power = self.adb.shell("dumpsys power", check=False)
        except Exception:
            return
        from server.drivers.phone_ui import screen_is_on

        if screen_is_on(power):
            self._screen_lit = True
            return
        self._say(f"[{self.case_id} {self.case_name}] 屏幕是灭的，先点亮")
        self.session.wake_and_unlock()
        self.cache.mark_stale()
        self._screen_lit = True

    def _restart_app(self) -> None:
        """杀掉 App 再拉起来。升级页状态没刷新时靠这个恢复。"""
        self._say(f"[{self.case_id} {self.case_name}] 升级页状态不对，重启 App 再看")
        self.adb.shell(f"am force-stop {self.pkg}", check=False)
        time.sleep(1.5)
        self.cache.mark_stale()
        self.session.prepare_for_app(self.pkg, allow_relaunch=True)
        time.sleep(1.5)

    def _dismiss_charge_popup(self) -> bool:
        """「请给设备充电」弹窗。点确定，并把本轮标记成环境不满足。"""
        page = self.cache.page()
        if CHARGE_POPUP_TITLE not in page:
            return False
        if self.need_charge:
            # 已经提示过，别再每轮重复点一次确定。
            return True
        self.need_charge = True
        self._say(f"[{self.case_id} {self.case_name}] 眼镜提示电量不足，无法升级")
        node = self._exact_node(CHARGE_POPUP_OK)
        if node is not None:
            try:
                node.click()
            except Exception:
                pass
        self.cache.mark_stale()
        time.sleep(0.6)
        return True

    def _all_latest(self) -> bool:
        return all(self.runner._module_action(module) == "latest" for module in self.modules)

    def _ensure_connected(self, seconds: int) -> bool:
        """蓝牙干扰后，先等 App 自己重新连上眼镜，再去点重试。"""
        deadline = time.time() + seconds
        announced = False
        while time.time() < deadline:
            if self._on_device_page() and self._connect_state() == "已连接":
                if announced:
                    self._say(f"[{self.case_id} {self.case_name}] App 已重新连接")
                return True
            if not announced:
                self._say(f"[{self.case_id} {self.case_name}] 等 App 重新连接")
                announced = True
            self._return_home()
            time.sleep(1)
        return False

    # ---------- 等升级 ----------

    def _before_version_text(self) -> str:
        """升级页还在时读「当前版本」。点升级后动画页上就没有这行了。"""
        return self._version_text()

    def _verify_firmware_applied(self, code: str, note: str) -> tuple[str, str]:
        """页面判成功后，只补充版本备注，不因版本不变改判。

        同版本循环本来就不要求固件号变化：跑完显示完成、能重连 App 即成功。
        眼镜没有 App 本地版本记录，升级页「当前版本」可读时写入备注即可。
        """
        if code != "PASS":
            return code, note
        before = getattr(self, "before_version", "") or ""
        if not before:
            return code, f"{note}（固件版本未校验：升级前没读到版本）"
        after = self._version_text() or ""
        if not after:
            return code, f"{note}（固件版本未校验：升级后没读到版本）"
        if before == after:
            return code, f"{note}；同版本循环，版本仍为 {after}"
        return code, f"{note}；固件已从 {before} 更新到 {after}"

    def _wait(self, case: dict, allow_inject: bool) -> tuple[str, str]:
        """等这一次升级结束。

        这里刻意不设「总共不能超过多少秒」：升级要多久事先规定不了，一轮 BLE
        主控升级实测 550~640 秒，卡死一个固定上限只会把正常升级判成失败
        （2026-11-10 的 T01 就是这样被误判的）。改成只看「还在不在动」：
        百分比、页面文案、App 的 BLE 日志，三路任一路有变化就继续等。
        长时间完全没有变化，才判卡住。
        """
        from server.drivers.phone_ui import case_direction, inject_due

        trigger = case.get("trigger") or {}
        threshold = int(trigger.get("progress_ge", 20))
        fallback = float(trigger.get("fallback_delay", self.settings.get("inject_fallback_delay", 15)))
        actions = self._actions(case) if allow_inject else []
        stall = float(self.settings.get("upgrade_stall_timeout") or 300)
        watch = ProgressWatch(
            stall,
            logcat_tag=str(self.settings.get("progress_logcat_tag") or "X-LOG:V"),
            serial=getattr(self.adb, "serial", "") or "",
            adb_path=self.settings.get("adb_path") or "adb",
        )
        running_since = None
        announced = False
        injected = not bool(actions)
        latest_since = None
        while True:
            if self.stop_check():
                return "stopped", "任务已停止"
            self.session.keep_awake()
            phase = self._phase()
            percent = self._percent()
            watch.see(percent=percent, page=self.cache.cached_page() or "")
            if phase == "running":
                latest_since = None
                running_since = running_since or time.time()
                if not announced:
                    self._say(f"[{self.case_id} {self.case_name}] 主控正在升级")
                    announced = True
            elif phase == "latest":
                # 一开始在升级、后来页面变回「已是最新版本」，说明这一次已经结束。
                if running_since is not None:
                    self._return_home()
                    return "success", "升级成功"
                latest_since = latest_since or time.time()
                if time.time() - latest_since >= 30:
                    return "latest", "升级页显示已是最新版本，后台没有可升级的包"
            if actions and not injected:
                elapsed = 0 if running_since is None else time.time() - running_since
                if inject_due(None, "", [], phase == "running", elapsed, threshold, fallback):
                    self.trigger = "升级中触发"
                    self._say(f"[{self.case_id} {self.case_name}] 执行干扰")
                    self._inject(actions)
                    injected = True
                    self.injected = True
                    if self.switch_failed:
                        return "inject_failed", self.switch_failed
                    if case_direction(case, self.link) == "retry":
                        return "interrupted", "干扰已打断这一次"
                    watch.note_progress()
            if phase == "failure":
                if time.time() - self._clicked_at < 15:
                    # 刚点完重试，页面可能还留着上一次的失败文案。
                    time.sleep(3)
                    continue
                return "fail", "固件升级失败"
            if phase == "success":
                if actions and not injected:
                    return "fail", "升级结束前没有执行干扰"
                self._return_home()
                return "success", "升级成功"
            if phase == "lost":
                return "fail", "升级页离开 daecho，无法确认结果"
            if phase == "need_charge":
                return "need_charge", "眼镜电量不足，App 拒绝升级"
            if phase in {"running", "latest"} and watch.stalled():
                return "fail", (
                    f"升级已经 {int(watch.idle_seconds())} 秒没有任何进展"
                    f"（最后进度 {_percent_label(percent)}），按卡住处理"
                )
            time.sleep(2)

    def _click_upgrade(self) -> None:
        if self._retry_button_visible():
            self.runner.ui.click("upgrade_retry", {"type": "description", "value": "升级失败，请重试"})
            self._say(f"[{self.case_id} {self.case_name}] 点击失败后的重试")
            self._clicked_at = time.time()
            self.cache.mark_stale()
            time.sleep(2)
            return
        self.runner.tap_upgrade()
        self._say(f"[{self.case_id} {self.case_name}] 已点击 {module_labels(self.modules)}")
        self._clicked_at = time.time()
        self.cache.mark_stale()
        time.sleep(2)

    def _retry_button_visible(self) -> bool:
        return "升级失败，请重试" in self.cache.page()

    # ---------- 页面判断 ----------

    def _phase(self) -> str:
        """一次判断只读一份页面 dump 和一次前台包名。

        判断顺序很关键：升级页本身就挂在 DeviceFirmwareActivity 上，
        「已是最新版本」和「升级至最新版本」两种按钮页都跑在这个 Activity 里。
        所以必须先看两个模块的按钮状态，再看「是不是停在升级 Activity」这种兜底。
        """
        self._ensure_screen_on()
        self._dismiss_charge_popup()
        page = self.cache.page()
        state = self._state_text()
        if CHARGE_POPUP_TITLE in page:
            return "need_charge"
        if self._matches(self.failure_texts, state) or "升级失败" in page:
            return "failure"
        if any(word in page for word in ("升级成功", "升级完成")):
            return "success"
        if self._percent(page) is not None or self._matches(self.running_texts, state) or "正在升级" in page:
            return "running"
        actions = self._module_actions(page)
        if actions == "upgrade":
            return "ready"
        if actions == "latest":
            return "latest"
        if self._on_firmware_activity() and self._in_app():
            # 点升级后会进入动画页，页面暂时没有版本卡片和百分比。
            return "running"
        return "lost" if not self._in_app() else "other"

    def _module_actions(self, dump: str | None = None) -> str:
        """所有选中模块的按钮状态：upgrade / latest / missing。只读一次 dump。"""
        dump = dump if dump is not None else self.cache.page()
        states = {self.runner._module_action(module, dump=dump) for module in self.modules}
        if states == {"upgrade"}:
            return "upgrade"
        if states == {"latest"}:
            return "latest"
        return "missing"

    def _in_app(self) -> bool:
        return self.cache.package() == self.pkg

    def _on_firmware_activity(self) -> bool:
        return FIRMWARE_ACTIVITY in self.cache.activity()

    def _percent(self, page: str | None = None) -> int | None:
        page = page if page is not None else self.cache.page()
        for module in self.modules:
            block = re.search(re.escape(module["title"]) + r".{0,500}", page, re.S)
            if not block:
                continue
            found = re.search(r"(\d+)\s*%", block.group(0))
            if found:
                return int(found.group(1))
        return None

    def _version_text(self) -> str:
        page = self.cache.page()
        versions = []
        for module in self.modules:
            found = re.search(re.escape(module["title"]) + r".{0,240}?当前版本：\s*([^<\"]+)", page, re.S)
            versions.append(found.group(1).strip() if found else "")
        return " | ".join(versions)

    def _on_device_page(self) -> bool:
        """设备卡片可见即算设备页。首页和设置页都带卡片，固件/升级页没有。"""
        if self._on_firmware_activity():
            return False
        page = self.cache.page()
        if f'package="{self.pkg}"' not in page:
            return False
        return any(f'text="{word}"' in page for word in ("已连接", "未连接", "连接中", "连接失败"))

    def _connect_state(self) -> str:
        """只读眼镜 App 自己的连接文案，不认状态栏「蓝牙已连接」。"""
        page = self.cache.page()
        block = re.search(r'package="' + re.escape(self.pkg) + r'"[\s\S]*', page)
        body = block.group(0) if block else ""
        # 眼镜同时有数据连接和音频连接。任意一路已连接就算连上；
        # 先看「未连接」会把「一路已连接、一路未连接」判成断开。
        if 'text="已连接"' in body:
            return "已连接"
        for word in ("连接中", "连接失败", "未连接"):
            if f'text="{word}"' in body:
                return word
        return ""

    # ---------- 收尾 ----------

    def _return_home(self) -> None:
        """升级页没有底部首页。先点页面自己的返回，回到设备卡片所在的页面。"""
        if self._on_device_page():
            return
        locator = {"type": "resourceId", "value": f"{self.pkg}:id/btn_title_bar_back"}
        session = self.runner.ui
        for _ in range(3):
            if not session.exists(locator):
                break
            try:
                session.click("title_back", locator)
            except Exception:
                break
            self.cache.mark_stale()
            time.sleep(0.6)
            if self._on_device_page():
                return
        home = self._exact_node("首页")
        if home is not None:
            try:
                home.click()
            except Exception:
                pass
            self.cache.mark_stale()
            time.sleep(0.4)

    def _confirm(self) -> str:
        """升级收尾：回到设备卡片，只读 App 自己的连接文案。升级后眼镜会重启，先给它回连时间。"""
        timeout = int(self.settings.get("reconnect_timeout") or 180)
        deadline = time.time() + timeout
        seen_disconnected_at = None
        while time.time() < deadline:
            self.session.prepare_for_app(self.pkg, allow_relaunch=True)
            self.runner.dismiss_app_update()
            if not self._on_device_page():
                self._return_home()
                time.sleep(0.3)
                continue
            state = self._connect_state()
            if state == "已连接":
                return "connected"
            if state in {"未连接", "连接失败", "连接中"}:
                # 刚升级完眼镜在重启，未连接只是过程，等它自己回来。
                if seen_disconnected_at is None:
                    seen_disconnected_at = time.time()
                    self._say(f"[{self.case_id} {self.case_name}] 升级后等待自动回连")
                time.sleep(2)
                continue
            time.sleep(1)
        return "disconnected" if seen_disconnected_at is not None else "unseen"
