"""DaFit 真机流程。

手机上的通知栏、锁屏和 App 页面分开处理：
- 通知栏用系统命令收起，不再从上往下滑动。
- 只有 dumpsys 显示锁屏时，才从屏幕偏下的位置上滑。
- 连接状态只读设备页控件。状态栏「蓝牙已连接」和「未连接」都不算已连接。
- 进度数字出现并且达到用例阈值后才注入；还停在升级按钮页时不杀进程、不关蓝牙。
- 看到「升级成功」并且设备页是「已连接」才记 PASS。升级失败但还能连上记 FAIL_UPGRADE。
  设备页明确写着未连接才记 HUNG_DEVICE。停在通知栏或其他设置页不算死机。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from server.common.config_loader import case_by_id, load_global, load_link
from server.common.safe_print import safe_print
from server.drivers.adb import AdbDriver
from server.drivers.bt_probe import BluetoothProbe
from server.drivers.phone_ui import (
    PhoneSession,
    case_direction,
    classify_round,
    direction_label,
    expected_switch,
    inject_due,
    label_connected,
    round_note,
    switch_action,
)
from server.drivers.uia import UiDriver
from server.drivers.ui_cache import UiCache
from server.drivers.progress_watch import ProgressWatch

PLAN = ["T02", "T02", "T04", "T07", "T08", "T06", "T09", "T05"]
LOST_PAGE_SECONDS = 45
READY_STALL_SECONDS = 90
# 被切到后台：先温和等（不打断可能还在跑的升级）；超时后强拉前台；再超时整轮失败，禁止无限等。
BACKGROUND_SOFT_SECONDS = 60
BACKGROUND_HARD_SECONDS = 180
# 息屏保持期间每隔多久打一次点。黑屏读不到页面，只能靠日志/心跳证明工具还活着。
SCREEN_OFF_PULSE_SECONDS = 10

# 值得自动重拉的失败。这些多半是设备侧偶发（DFU 自己断、握手没准备好、单轮代码异常），
# 重新走一遍通常就过去了。
#
# 故意不包含：
#   ABORT_ENV   —— 没包、电量不够、App 没登录、设备掉线，重试也还是同一个结果
#   HUNG_DEVICE —— 设备真的失联了，该立刻停下来看现场，不能反复重试掩盖问题
#   SKIP        —— 这一轮本来就没算有效执行
RETRYABLE_CODES = {"FAIL_UPGRADE", "ERROR"}


def current_serial() -> str:
    serials = AdbDriver("adb").devices()
    if len(serials) != 1:
        raise RuntimeError("需要正好一台手机，当前: " + ",".join(serials))
    return serials[0]


def main() -> None:
    import uiautomator2 as u2

    stamp = time.strftime("%Y%m%d_%H%M%S")
    from server.common import paths

    folder = paths.OUTPUT_LOGS / f"dafit_flow_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    serial = current_serial()
    safe_print("PHONE " + str(serial))
    safe_print("计划")
    for case_id in PLAN:
        case = case_by_id(case_id)
        label = direction_label(case_direction(case))
        safe_print(f"  {case_id} {label} {case['name']}")
    device = u2.connect(serial)
    rows = []
    for case_id in PLAN:
        try:
            row = run_case(device, folder, case_id)
        except Exception as exc:
            row = {
                "case_id": case_id,
                "name": "",
                "direction": "",
                "code": "ERROR",
                "note": str(exc),
                "elapsed": 0,
            }
        rows.append(row)
        safe_print(f"结果 {row['case_id']} {row['direction']} {row['name']} {row['code']} {row['note']}")
        (folder / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    safe_print("SUMMARY " + str(folder / "summary.json"))


def run_case(device, folder: Path, case_id: str, settings: dict | None = None, logger=None,
             stop_check=None) -> dict:
    return DafitFlow(device, folder, settings, logger, stop_check=stop_check).run(case_id)


class DafitFlow:
    def __init__(self, device, folder: Path, settings: dict | None, logger=None,
                 stop_check=None) -> None:
        self.device = device
        self.folder = folder
        self.logger = logger
        # 停止信号。网页上点「停止任务」时，等待循环必须能中途退出，
        # 否则要等这一轮升级跑完（十几分钟），看上去就像卡死了。
        self.stop_check = stop_check or (lambda: False)
        self.settings = settings or load_global()
        self.link = load_link("watch")
        self.pkg = self.link["pkg"]
        serial = getattr(device, "serial", "") or ""
        if not serial:
            serial = current_serial()
        self.adb = AdbDriver(self.settings.get("adb_path") or "adb", serial, timeout=40)
        try:
            self.device.settings["wait_timeout"] = 2
        except Exception:
            pass
        self.cache = UiCache(device, ttl=float(self.settings.get("ui_cache_ttl") or 2.0))
        self.session = PhoneSession(device, self.adb, self.cache)
        self.case_id = ""
        self.case_name = ""
        self.direction = "forward"
        self.injected = False
        self.switch_failed = ""
        self.trigger = ""
        self._clicked_upgrade = False
        # 判断页面前是否已确认屏幕亮过。T08 自己会息屏，不能每轮都去点亮。
        self._screen_lit = False
        # 这一轮真正看到过的最后一个进度。用来判断「是否真的开始刷写」。
        self._last_percent_seen = None
        self.before_version = ""
        self.after_version = ""
        self.announced = ""
        locators = self.link.get("locators") or {}
        self.entry_text = _locator_text(locators.get("upgrade_entry"), "固件升级")
        self.button_text = _locator_text(locators.get("upgrade_button"), "升级")
        # done_button 在手表上是成功页的 resourceId。失败弹窗只有文字「完成」，
        # 写在 done_button_text。不能把 resourceId 当成要点的文字。
        self.done_text = _locator_text(locators.get("done_button_text"), "") or _locator_text(
            locators.get("done_button"), "完成"
        )
        if ":id/" in self.done_text:
            self.done_text = "完成"
        self.running_texts = list(self.link.get("running_texts") or [])
        self.failure_texts = list(self.link.get("failure_texts") or ["固件升级失败", "升级失败"])
        self.offer_texts = list(self.link.get("offer_texts") or [])
        self.connected_words = list(self.link.get("ui_connected") or ["已连接"])
        self.disconnected_words = list(self.link.get("ui_disconnected") or ["未连接"])
        self._identity_bound = False

    def _bind_device_identity(self) -> None:
        """按当前手机上真正绑定的设备补齐 name/mac，不依赖 yaml 写死的某一台。

        蓝牙 dumpsys 经常只有 MAC、没有名字。手表设备名又没有 resourceId。
        所以先打开设备页，从 App 页面文案里填；蓝牙只作补充。
        """
        if getattr(self, "_identity_bound", False):
            return
        from server.common.device_identity import apply_identity, resolve_identity

        # 单测用 __new__ 造的流程没有 adb/link，直接跳过。
        if not getattr(self, "adb", None) or not getattr(self, "link", None):
            self._identity_bound = True
            return
        try:
            self._reveal_for_identity()
            page = ""
            try:
                page = self.cache.page() or ""
            except Exception:
                page = ""

            def read_text(locator):
                rid = _resource_id(locator)
                if rid:
                    return self._text_of_id(rid) or ""
                return ""

            identity = resolve_identity(self.link, adb=self.adb, read_text=read_text, page=page)
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
        elif not name:
            self._say("未识别到穿戴设备名称（蓝牙和 App 设备页都没有），报告设备栏可能为空")

    def _reveal_for_identity(self) -> None:
        """打开能看见设备名的页面。手表是设备 tab。"""
        self.session.prepare_for_app(self.pkg, allow_relaunch=True)
        self._back_to_tabs()
        self._click_tab()
        time.sleep(0.6)
        self._touch()

    def run(self, case_id: str) -> dict:
        """跑一条用例，失败后按配置自动重拉。

        每次尝试都留一条 attempts 记录，最终结果里带上 retried / attempts 字段。
        重试通过**不会**变成干净的 PASS：code 仍是 PASS，但 attempts 里能看到
        前几次是失败的，调度器和报告都会标出「第 N 次才通过」。
        这一点很重要——设备偶发不稳定必须留痕，不能被重试抹平。

        但有一条硬限制：**已经真的往设备里写过固件的失败，绝不自动重刷**。
        2026-11-12 一台 Da Ring S4 升级反复在 5% 掉线，工具按 FAIL_UPGRADE 自动重拉，
        App 又从 0% 重传，几轮之后戒指停在 bootloader、开不了机了。
        写入中断过的固件再整包重刷，是最容易把设备刷死的操作。写过了就停下来
        交给人看现场，没写过（页面都没进去）才允许自动重试。
        """
        started = time.time()
        self.case_id = case_id
        self._result_restarted = False
        case = case_by_id(case_id)
        self._bind_device_identity()
        attempts: list[dict] = []
        row = self._attempt(case, started, attempts, index=1)
        if row["code"] in RETRYABLE_CODES and self._touched_firmware():
            row["attempts"] = attempts
            row["retried"] = False
            row["note"] = f"{row['note']}；本轮已经写过固件，为避免刷死设备，不再自动重刷"
            self._say(f"[{case_id}] 本轮进过刷写，失败后不再自动重试，请人工确认设备状态")
            return row
        total = 1
        limit = max(1, int(self.settings.get("case_retry_times") or 1))
        while row["code"] in RETRYABLE_CODES and total < limit:
            if self.stop_check():
                break
            total += 1
            self._say(f"[{case_id}] 第 {total} 次尝试（上一次 {row['code']}：{row['note']}）")
            # 重拉前给设备一点恢复时间，并回到干净的主页。
            gap = float(self.settings.get("case_gap_seconds") or 0)
            if gap > 0:
                end = time.time() + gap
                while time.time() < end and not self.stop_check():
                    time.sleep(0.5)
            row = self._attempt(case, started, attempts, index=total)
        row["attempts"] = attempts
        row["retried"] = total > 1
        if total > 1 and row["code"] == "PASS":
            row["note"] = f"第 {total} 次尝试才通过（前 {total - 1} 次失败）"
        elif total > 1:
            row["note"] = f"重试 {total - 1} 次仍失败：{row['note']}"
        return row

    def _touched_firmware(self) -> bool:
        """这一轮有没有真的开始往设备里写固件。

        判据是「看到过进度数字」，不是「点过升级按钮」。
        两者差别很关键：点了升级但页面没起来、或者黑屏读不到，其实一个字节都没写，
        这种情况重试是安全的，不该被「禁止重刷」挡住。
        手表会一路报百分号，所以用进度判断最准。
        """
        return getattr(self, "_last_percent_seen", None) is not None

    def _attempt(self, case: dict, started: float, attempts: list[dict], *, index: int) -> dict:
        """跑一次，并把这一次的结论记进 attempts。"""
        attempt_started = time.time()
        try:
            row = self._run(case, started)
        except Exception as exc:
            try:
                self._save_log("error")
            except Exception:
                pass
            row = self._row(case["id"], "ERROR", str(exc), started)
        attempts.append({
            "index": index,
            "code": row["code"],
            "note": row["note"],
            "elapsed": round(time.time() - attempt_started, 1),
        })
        return row

    def _run(self, case: dict, started: float) -> dict:
        case_id = case["id"]
        name = case.get("name") or case_id
        # 必须带上当前链路。眼镜 T06 关 Wi-Fi 是可重试，手表和戒指仍是必须成功。
        # 不传链路时眼镜会被当成必须成功：等待循环已经按打断返回，这里却不重新点升级。
        direction = case_direction(case, self.link)
        self.case_name = name
        self.direction = direction
        self._announce(case)
        # 手机电量只用来防止手机自己断电；T09 的「充电中」看的是穿戴设备，不是手机 USB。
        level, _phone_charging = self.adb.battery()
        minimum = int(self.settings.get("min_battery") or 20)
        if level is not None and level < minimum:
            return self._row(case_id, "ABORT_ENV", f"手机电量 {level} 低于 {minimum}", started)
        if case.get("require_charging"):
            hit = self._wearable_charging_label()
            if not hit:
                return self._row(
                    case_id,
                    "SKIP",
                    "穿戴设备未在充电（App 未显示充电中），这条正向用例不执行",
                    started,
                )
            self._say(f"[{case_id} {name}] 穿戴设备充电状态：{hit}")
        self.session.prepare_for_app(self.pkg)
        if self._login_blocking():
            return self._row(case_id, "ABORT_ENV", "APP 未登录", started)
        phase = self._open_ready()
        if phase == "running":
            self._shot("no_entry")
            return self._row(case_id, "FAIL_UPGRADE", "上一轮升级未结束，无法开始本轮", started)
        if phase == "latest":
            # 后台没有可升级的包。这不是升级失败，继续跑下去每条用例都会假失败。
            self._shot("no_entry")
            return self._row(case_id, "ABORT_ENV", self._latest_note(), started)
        if phase == "need_charge":
            self._shot("no_entry")
            return self._row(case_id, "ABORT_ENV", "眼镜电量或充电状态不满足，App 拒绝升级", started)
        if phase != "ready":
            state = self._read_link_state()
            if state == "disconnected":
                self._shot("no_entry")
                self._save_log("no_entry")
                return self._row(case_id, "HUNG_DEVICE", "未能进入固件升级，设备页未连接", started)
            self._shot("no_entry")
            self._save_log("no_entry")
            return self._row(case_id, "FAIL_UPGRADE", "没有进入固件升级页", started)
        # 点升级前读版本。点下去就进入进度/动画页，眼镜的「当前版本」会消失。
        self.before_version = self._before_version_text()
        self._click_upgrade()
        outcome, detail = self._wait(case, allow_inject=True)
        self.after_version = self._version_text()
        self._shot("result")
        if outcome == "stopped":
            # 人点了停止。这一轮不算通过也不算失败，直接记 SKIP 交给调度器收尾。
            return self._row(case_id, "SKIP", "任务已停止", started)
        if outcome in {"latest", "need_charge"} and not (
            direction == "retry" and self.injected
        ):
            # 升级过程中页面退回「已是最新版本」或弹出补电提示，属于环境/后台配置问题。
            # 例外：可重试用例已经打过干扰（如 T04 杀 App）后看到「最新」——多半是打断后的假象，
            # 不能整任务 ABORT_ENV 把后续全 SKIP；交给下面的重试路径。
            self._shot("abort")
            self._save_log("abort")
            return self._row(case_id, "ABORT_ENV", detail, started)
        if outcome == "inject_failed":
            self._shot("inject_failed")
            # 干扰没打上，但这一次升级还在跑。等它自己结束，再收尾，不留给下一轮。
            self._say(f"[{case_id} {name}] 干扰未生效，等这一次升级自行结束")
            self._wait(case, allow_inject=False)
            state = self._confirm()
            self._say(f"[{case_id} {name}] 已收尾，设备页 {state}")
            self._save_log("round")
            return self._row(case_id, "SKIP", detail + "，这一轮不计入", started)
        if direction == "retry" and case.get("inject") and not self.injected:
            state = self._confirm()
            self._shot("connect")
            self._save_log("round")
            return self._row(case_id, "FAIL_UPGRADE", "没有在升级过程中执行干扰，不能开始重试", started)
        if direction == "retry" and outcome != "success":
            if outcome in {"latest", "need_charge"}:
                self._say(
                    f"[{case_id} {name}] 干扰后页面变成「已是最新/需充电」，按打断处理并重试"
                )
            self._say(f"[{case_id} {name}] 这一次已被打断，重新升级")
            self._click_done()
            # 蓝牙刚打开时，升级页上的「升级」按钮还在，不代表手表已经回连。
            # 先回设备页等「已连接」，再进升级页点升级。反过来会空等无进展，
            # 失败后才去看连接，顺序反了，也浪费一整段超时。
            if not self._wait_until_connected():
                self._shot("retry")
                self._shot("connect")
                self._save_log("round")
                return self._row(
                    case_id,
                    "FAIL_UPGRADE",
                    "干扰恢复后设备页一直未连接，没有重新点击升级",
                    started,
                )
            phase = self._open_ready()
            if phase == "running":
                outcome, detail = self._wait(case, allow_inject=False)
            elif phase != "ready":
                outcome, detail = "fail", "干扰后没有回到升级入口，无法重试"
            else:
                self._click_upgrade()
                outcome, detail = self._wait(case, allow_inject=False)
            self._shot("retry")
        state = self._confirm()
        self._shot("connect")
        code = classify_round(outcome, state, direction)
        note = round_note(outcome, detail, state, direction)
        if code == "HUNG_DEVICE":
            ok, reason = self._env_ok()
            if not ok:
                code, note = "ABORT_ENV", reason
        # 同版本循环：页面完成 + 能重连即成功；版本备注只作参考，不改判。
        if code == "PASS":
            code, note = self._verify_firmware_applied(code, note)
        if code != "PASS":
            self._save_log("round")
        return self._row(case_id, code, note, started)

    # ---------- 固件是否真的刷进去了 ----------

    def _pref_value(self, spec: str) -> str:
        """按 'config.xml:band_version' 这种写法读一个 shared_prefs 值。

        只有调试包可读。读不到返回空串，调用方据此跳过校验，
        绝不能因为读不到就判失败。
        """
        if not spec or ":" not in spec:
            return ""
        filename, key = spec.split(":", 1)
        try:
            raw = self.adb.run_as(self.pkg, f"grep 'name=\"{key}\"' shared_prefs/{filename}")
        except Exception:
            return ""
        match = re.search(r">([^<]+)<", raw or "")
        return match.group(1).strip() if match else ""

    def _stored_version(self) -> str:
        """从 App 本地存储读设备实报固件版本。读不到返回空串。

        同时看 current 和 target 两个来源里配的键，多个键都读到时要求一致：
        不一致说明 App 自己都没对齐，这时不敢据此下结论，返回当前值但会记入备注。
        """
        files = (self.link.get("version_store") or {}).get("files") or {}
        # current 是权威来源；再补 last_band_firmware 做交叉验证（配了才读）。
        specs = [files.get("current") or ""]
        extra = files.get("last") or ""
        if extra:
            specs.append(extra)
        values = [self._pref_value(spec) for spec in specs if spec]
        values = [item for item in values if item]
        if not values:
            return ""
        return values[0]

    def _target_version(self) -> str:
        """后台配给这台设备的目标固件版本。"""
        files = (self.link.get("version_store") or {}).get("files") or {}
        return self._pref_value(files.get("target") or "")

    def _verify_firmware_applied(self, code: str, note: str) -> tuple[str, str]:
        """页面判成功后，只补充版本备注，不因版本对不上改判。

        同版本循环本来就不要求固件号变化：跑完显示完成、能重连 App 即成功。
        正式包读不到 prefs 时，退回升级页/设备页上的实报文案再记一笔。
        """
        if code != "PASS":
            return code, note
        stored = self._stored_version()
        target = self._target_version()
        source = "prefs"
        if not stored:
            # 换机常见：非调试包 run-as 读不到 shared_prefs。
            ui_ver = (self.after_version or self._version_text() or "").strip()
            # 控件没读到真版本时，有的实现会回落成 resourceId 字符串，不能当版本。
            if ui_ver and ":" not in ui_ver and re.search(r"\d", ui_ver):
                stored = ui_ver
                source = "ui"
        if not stored:
            return code, f"{note}（固件版本未校验：读不到 App 本地记录）"
        if not target:
            return code, f"{note}（固件版本未校验：读不到目标版本）"
        if stored == target:
            return code, f"{note}；固件已更新到 {stored}"
        if source == "ui":
            return code, (
                f"{note}（固件版本未校验：页面实报 {stored}，prefs 不可读）"
            )
        # 同版本循环允许实报与目标不一致，只记备注，不改判失败。
        return code, (
            f"{note}；同版本循环，设备固件仍为 {stored}"
            f"（目标 {target}，不要求变化）"
        )

    def _say(self, message: str) -> None:
        # 控制台输出必须走 safe_print：stdout 是管道时直接 print 会阻塞住整个任务线程。
        safe_print(f"  {message}")
        if self.logger is not None:
            self.logger.log(message)

    def _latest_note(self) -> str:
        """升级页显示已是最新版本时的说明。让人知道要去后台换包，而不是反复重跑。"""
        return (
            "升级页显示已是最新版本，后台没有可升级的包。"
            "若本任务前面用例已经升成功过，多半是可升级包已被用完；"
            "请到 PHP 后台换上新包（或准备 A↔B）后再开任务"
        )

    def _announce(self, case: dict) -> None:
        actions = [step.get("action") for step in case.get("inject") or []]
        action_text = "、".join(actions) if actions else "不额外干扰"
        if self.direction == "retry":
            rule = "进度到阈值后执行干扰。这一次可以失败，重新点击升级必须成功，设备页必须已连接。"
        else:
            rule = "升级必须成功，并且设备页重新已连接。这一次失败不算通过。"
        self._say(f"用例 {case['id']} {direction_label(self.direction)} {case.get('name') or case['id']}")
        self._say(f"动作 {action_text}")
        self._say(f"通过标准 {rule}")

    def _open_ready(self) -> str:
        phase = self._open_upgrade()
        if phase == "running":
            self._say("[上一轮升级还没结束，先等它跑完再开始本轮]")
            self._wait({"id": self.case_id, "inject": [], "trigger": {}}, allow_inject=False)
            self._click_done()
            time.sleep(1)
            phase = self._open_upgrade()
        if phase in {"success", "failure"}:
            self._click_done()
            time.sleep(1)
            phase = self._open_upgrade()
        if phase == "latest":
            # 页面明确说已是最新版本。先重启一次 App，排除页面没刷新的可能。
            self._restart_app()
            phase = self._open_upgrade()
        if phase == "absent":
            time.sleep(3)
            phase = self._open_upgrade()
        return phase

    def _open_upgrade(self) -> str:
        self.session.prepare_for_app(self.pkg, allow_relaunch=self._phase() != "running")
        self._accept_pair()
        phase = self._phase()
        if phase in {"success", "failure"}:
            self._click_done()
            time.sleep(1)
            phase = self._phase()
        if phase in {"running", "ready"}:
            return phase
        self._goto_entry()
        phase = self._phase()
        if phase in {"running", "ready", "success", "failure", "latest"}:
            return phase
        return "absent"

    def _goto_entry(self) -> None:
        self._back_to_tabs()
        self._click_tab()
        time.sleep(0.8)
        if not self._reveal(self.entry_text):
            return
        node = self._exact_node(self.entry_text)
        if node is not None:
            node.click()
            time.sleep(1.2)

    def _back_to_tabs(self, *, leave_upgrade: bool = False) -> None:
        rid = _resource_id(self.link["locators"].get("device_tab"))
        for _ in range(6):
            on_upgrade = self._phase() in {"running", "ready", "success", "failure"}
            if self._on_device_page() and not on_upgrade:
                return
            if rid and self._exists_id(rid) and not (leave_upgrade and on_upgrade):
                return
            if on_upgrade and not leave_upgrade:
                return
            if not self.session.foreground(self.pkg):
                break
            try:
                self.device.press("back")
            except Exception:
                break
            self._touch()
            time.sleep(0.5)
            self.session.collapse()
        self.session.prepare_for_app(self.pkg)

    def _click_tab(self) -> None:
        locator = self.link["locators"].get("device_tab") or {}
        value = locator.get("value") if isinstance(locator.get("value"), dict) else {}
        rid = value.get("resourceId") or ""
        index = int(value.get("index") or 0)
        if not rid:
            return
        try:
            node = self.device(resourceId=rid, instance=index)
        except Exception:
            return
        if getattr(node, "exists", False):
            node.click()

    def _restart_app(self) -> None:
        """杀掉 App 再拉起来。

        升级页状态没刷新、失败弹窗点不干净、或者收尾时切不回设备页，
        都可以用这一招回到干净的主页。三条链路共用。

        2026-11-13：这个方法原来只写在 GlassFlow 里，手表链路调它直接抛
        `'DafitFlow' object has no attribute '_restart_app'`，把 T08 的一轮打成 ERROR。
        """
        self._say(f"[{self.case_id} {self.case_name}] 重启 App 再看")
        try:
            self.adb.shell(f"am force-stop {self.pkg}", check=False)
        except Exception:
            pass
        time.sleep(1.5)
        self._touch()
        try:
            self.session.prepare_for_app(self.pkg, allow_relaunch=True)
        except Exception:
            pass
        time.sleep(1.5)
        self._touch()

    def _click_upgrade(self) -> None:
        node = self._exact_node(self.button_text)
        if node is None:
            raise RuntimeError("升级页没有升级按钮")
        node.click()
        self._clicked_upgrade = True
        self._say(f"[{self.case_id} {self.case_name}] 点击升级")
        time.sleep(2)

    def _click_done(self) -> None:
        """点掉「完成」。

        失败弹窗的「完成」盖在升级页上。底下的 btn_upgrade_complete 可能还在，
        先点它等于点到被挡住的按钮，弹窗关不掉，后面会在这个页面空等重连。
        所以先点看得见的文字，再点 content-desc，再点弹窗按钮，最后才点成功页按钮。

        点完必须让 UiCache 失效：否则紧接着的 `_phase()` 还会读到旧 dump 里的
        「升级成功」，导航乱套，日志误报「页面没回到设备页」。
        """
        if self._click_node(self._exact_node(self.done_text)):
            return
        if self._click_node(self._described_node(self.done_text)):
            return
        for rid in (
            f"{self.pkg}:id/md_buttonDefaultPositive",
            "android:id/button1",
            _resource_id((self.link.get("locators") or {}).get("done_button")),
        ):
            if not rid:
                continue
            try:
                node = self.device(resourceId=rid)
            except Exception:
                continue
            if self._click_node(node if getattr(node, "exists", False) else None):
                return

    def _click_node(self, node) -> bool:
        if node is None:
            return False
        try:
            node.click()
        except Exception:
            return False
        self._touch()
        time.sleep(0.8)
        return True

    def _wait(self, case: dict, allow_inject: bool) -> tuple[str, str]:
        """等这一次升级结束。只看有没有进展，不设总时长上限。

        原来这里卡 600 秒总超时，正常升级只要慢一点就被判失败。升级快慢由包大小
        和链路决定，事先规定不了，所以改成：百分比、页面文案、App 日志三路只要有
        一路在变就继续等，长时间完全不动才判卡住。
        """
        trigger = case.get("trigger") or {}
        threshold = int(trigger.get("progress_ge", 20))
        fallback = float(trigger.get("fallback_delay", self.settings.get("inject_fallback_delay", 15)))
        keywords = list(dict.fromkeys(self.running_texts + list(trigger.get("keywords") or [])))
        actions = self._actions(case) if allow_inject else []
        watch = ProgressWatch(
            float(self.settings.get("upgrade_stall_timeout") or 300),
            logcat_tag=str(self.settings.get("progress_logcat_tag") or ""),
            serial=getattr(self.adb, "serial", "") or "",
            adb_path=self.settings.get("adb_path") or "adb",
        )
        page_since = None
        last_percent = None
        injected = False
        resumed = False
        off_since = None
        ready_since = None
        last_recover = 0.0
        background_noted = False
        bg_since = None
        while True:
            if self.stop_check():
                return "stopped", "任务已停止"
            now = time.time()
            self.session.keep_awake()
            self._accept_pair()
            # 每圈先丢掉页面缓存：快机上升级几秒就结束，若还拿 2 秒前的 dump，
            # 手机已到成功/设备页，日志却还停在九十几%。
            self._touch()
            phase = self._phase()
            percent = self._percent()
            page = self.cache.cached_page() or ""
            watch.see(percent=percent, page=page)
            if percent is not None and percent != last_percent:
                last_percent = percent
                self._last_percent_seen = percent
                self._say(f"[{self.case_id} {self.case_name}] 进度 {percent}%")
            elif phase == "success" and last_percent is not None and last_percent < 100:
                # 手机已成功但百分比控件没了：补打一枪，避免日志停在 96% 像卡住。
                self._say(f"[{self.case_id} {self.case_name}] 进度 100%（页面已升级成功）")
                last_percent = 100
                self._last_percent_seen = 100
            if phase == "failure":
                return "fail", self._failure_text(last_percent)
            if phase == "success":
                return "success", "升级成功"
            if phase == "latest":
                # 一开始在升级、后来页面变回「您的固件是最新版本」，说明这一次已经结束。
                # 若从头就是这个页面，交给 _open_ready 处理，这里不下结论。
                if page_since is not None:
                    return "success", "升级成功"
                return "latest", self._latest_note()
            if phase == "running":
                page_since = page_since or now
                off_since = None
                ready_since = None
            elif phase == "ready":
                page_since = None
                off_since = None
                ready_since = ready_since or now
            else:
                page_since = None
                ready_since = None
                off_since = off_since or now
            if actions and not injected:
                elapsed = 0.0 if page_since is None else now - page_since
                if inject_due(percent, self._state_text(), keywords, phase == "running", elapsed, threshold, fallback):
                    self._say(f"[{self.case_id} {self.case_name}] 干扰 进度 {percent if percent is not None else '无'}%")
                    self.trigger = f"进度:{percent}" if percent is not None else "兜底触发"
                    self._inject(actions, watch=watch)
                    injected = True
                    self.injected = True
                    if self.switch_failed:
                        # 干扰没打上，这一轮不成立。不能拿「升级照样成功」当通过。
                        return "inject_failed", self.switch_failed
                    page_since = None
                    off_since = None
                    ready_since = None
                    watch.note_progress()
                    continue
            retry = case_direction(case, self.link) == "retry"
            if retry and injected and phase == "ready" and ready_since and now - ready_since >= 15:
                return "interrupted", f"干扰后回到升级入口，最后进度 {_percent_label(last_percent)}"
            if (
                phase == "ready"
                and injected
                and not retry
                and not resumed
                and ready_since
                and now - ready_since >= 20
            ):
                self._say(f"[{self.case_id} {self.case_name}] 再次点击升级")
                self._click_upgrade()
                resumed = True
                ready_since = None
                page_since = None
                watch.note_progress()
                continue
            if phase == "ready" and ready_since and now - ready_since >= READY_STALL_SECONDS:
                return "fail", f"升级没有继续，最后进度 {_percent_label(last_percent)}"
            if phase == "other":
                # 先分清两种「看不到页面」：
                #   A. App 被切到后台（有人手动切走、别的 App 抢前台）——升级还在跑，不能判失败
                #   B. App 在前台但升级页真的没了——才是真丢了
                # 2026-11-13：升级到 69% 时有人把 DaFit 切到后台，页面读不到，
                # 旧的 45 秒超时直接判「升级页丢失 -> 固件升级失败」，
                # 其实升级一直在后台正常跑到 73%。这种误判会把好设备记成坏设备。
                if self.session.foreground(self.pkg):
                    if off_since and now - off_since >= LOST_PAGE_SECONDS:
                        return "fail", f"升级页丢失，最后进度 {_percent_label(last_percent)}"
                    if now - last_recover >= 8:
                        self._goto_entry()
                        last_recover = time.time()
                else:
                    # 被切走了：先温和等；别的 App（如眼镜）抢前台时 soft 拉不回来，
                    # 超时后强拉，禁止像「卡在眼镜界面」那样无限等。
                    if bg_since is None:
                        bg_since = now
                    if not background_noted:
                        self._say(
                            f"[{self.case_id} {self.case_name}] App 被切到后台，"
                            f"升级应该还在跑（最后进度 {_percent_label(last_percent)}），等它回来"
                        )
                        background_noted = True
                    off_since = None
                    bg_elapsed = now - bg_since
                    if bg_elapsed >= BACKGROUND_HARD_SECONDS:
                        return "fail", (
                            f"App 被切到后台超过 {int(BACKGROUND_HARD_SECONDS)} 秒仍未回到前台"
                            f"（最后进度 {_percent_label(last_percent)}）"
                        )
                    if now - last_recover >= 15:
                        force = bg_elapsed >= BACKGROUND_SOFT_SECONDS
                        if force:
                            self._say(
                                f"[{self.case_id} {self.case_name}] 后台已 {int(bg_elapsed)} 秒，"
                                f"强拉回本 App"
                            )
                        self.session.prepare_for_app(self.pkg, allow_relaunch=force)
                        last_recover = time.time()
            else:
                background_noted = False
                bg_since = None
            if phase == "running" and watch.stalled():
                return "fail", (
                    f"升级已经 {int(watch.idle_seconds())} 秒没有任何进展"
                    f"（最后进度 {_percent_label(last_percent)}），按卡住处理"
                )
            # 快机进度跳动大：轮询越短，日志越贴手机；0.5s 足够，别再空等 2 秒。
            time.sleep(0.5 if phase == "running" else 1.0)

    def _inject(self, actions: list[tuple[str, str, float]], watch=None) -> None:
        screen_was_off = False
        for kind, command, wait in actions:
            if kind == "unlock":
                self._say(f"[{self.case_id}] 解锁")
                self.session.wake_and_unlock()
            elif kind == "screen_off":
                self._say(f"[{self.case_id}] 息屏，保持 {int(wait) if wait else 0} 秒并继续监测升级")
                self.session.screen_off()
                screen_was_off = True
            elif kind == "screen_on":
                self._say(f"[{self.case_id}] 亮屏")
                self.session.screen_on()
            elif kind in {"bt_off", "bt_on"}:
                self._say(f"[{self.case_id}] {command}，失败时改走设置页开关")
                self.session.set_bluetooth(kind == "bt_on")
            elif kind in {"wifi_off", "wifi_on"}:
                self._say(f"[{self.case_id}] {command}，失败时改走设置页开关")
                self.session.set_wifi(kind == "wifi_on")
            else:
                self._say(f"[{self.case_id}] {command}")
                self.adb.shell(command, check=False)
            if switch_action(kind) is not None:
                self._verify_switch(kind)
            if self.switch_failed:
                return
            if wait > 0:
                # 息屏这段不能干睡：黑屏读不到页面，要用日志盯着还在不在传，并定时打点。
                if kind == "screen_off":
                    self._hold_screen_off(wait, watch)
                else:
                    time.sleep(wait)
        # 杀 App / 亮屏后其它 App（眼镜、广告）常抢前台；只 collapse 不够，要强拉回来。
        kinds = {kind for kind, _command, _wait in actions}
        if screen_was_off or "shell" in kinds:
            self.session.prepare_for_app(self.pkg, allow_relaunch=True)
            if screen_was_off:
                self._inspect_after_screen_on()
        else:
            self.session.collapse()

    def _hold_screen_off(self, seconds: float, watch=None) -> None:
        """息屏保持：期间禁止读页面（会触发点亮），只靠日志/心跳证明还在跑。"""
        deadline = time.time() + max(0.0, float(seconds))
        last_pulse = 0.0
        while time.time() < deadline:
            if self.stop_check():
                self._say(f"[{self.case_id}] 息屏等待被停止")
                return
            now = time.time()
            moved = False
            if watch is not None:
                try:
                    moved = bool(watch.see(percent=None, page=""))
                except Exception:
                    moved = False
            pulse = ""
            try:
                pulse = self._screen_off_pulse() or ""
            except Exception:
                pulse = ""
            if pulse and watch is not None:
                watch.note_progress()
                moved = True
            remain = max(0, int(deadline - now))
            if moved or now - last_pulse >= SCREEN_OFF_PULSE_SECONDS:
                if pulse:
                    self._say(f"[{self.case_id}] 息屏中，{pulse}（还剩 {remain} 秒）")
                elif moved:
                    self._say(f"[{self.case_id}] 息屏中，传输仍在推进（还剩 {remain} 秒）")
                else:
                    idle = int(watch.idle_seconds()) if watch is not None else 0
                    self._say(
                        f"[{self.case_id}] 息屏中，已 {idle} 秒未见新进展（还剩 {remain} 秒）"
                    )
                last_pulse = now
            time.sleep(2.0)

    def _screen_off_pulse(self) -> str:
        """息屏时子类可从日志读进度。默认靠 ProgressWatch 的传输日志。"""
        return ""

    def _inspect_after_screen_on(self) -> None:
        """息屏结束主动亮屏后，立刻看升级还在不在、成没成。"""
        self._touch()
        try:
            phase = self._phase()
        except Exception as exc:
            self._say(f"[{self.case_id}] 亮屏复查失败: {exc}")
            return
        percent = None
        try:
            percent = self._percent()
        except Exception:
            percent = None
        if phase == "running":
            self._say(
                f"[{self.case_id}] 亮屏复查：升级仍在进行，进度 {_percent_label(percent)}"
            )
        elif phase == "success":
            self._say(f"[{self.case_id}] 亮屏复查：升级已成功")
        elif phase == "failure":
            self._say(f"[{self.case_id}] 亮屏复查：升级已失败")
        else:
            self._say(
                f"[{self.case_id}] 亮屏复查：页面={phase} 进度 {_percent_label(percent)}"
            )

    def _verify_switch(self, kind: str) -> None:
        """命令返回成功不代表开关真的变了。要趁它还没被下一步开回去时立刻读。"""
        switch = switch_action(kind)
        if switch is None:
            return
        reader, expect = switch
        expected = expect == "True"
        actual = None
        for _ in range(3):
            actual = getattr(self.adb, reader, lambda: None)()
            if actual is expected_switch(expected):
                break
            time.sleep(1)
        if actual is None:
            self._say(f"[{self.case_id}] {kind} 开关状态读不到，按未生效处理")
            self.switch_failed = f"{kind} 读不到系统开关，无法确认干扰生效"
            return
        if actual is not expected_switch(expected):
            self._say(f"[{self.case_id}] {kind} 没有生效，开关仍是 {actual}")
            self.switch_failed = f"{kind} 没有生效，命令被系统拒绝（实际开关 {actual}）"
            return
        self._say(f"[{self.case_id}] {kind} 已生效")

    def _actions(self, case: dict) -> list[tuple[str, str, float]]:
        steps = []
        commands = self.link.get("commands") or {}
        for step in case.get("inject") or []:
            name = step.get("action") or ""
            wait = float(step.get("wait_after") or 0)
            if name == "unlock":
                steps.append((name, "", wait))
                continue
            template = commands.get(name)
            if not template:
                raise RuntimeError(f"链路未配置动作 {name}")
            # bt/wifi/screen 开关需要保留动作名，注入后要回读系统开关确认生效。
            kind = name if name in {"bt_off", "bt_on", "wifi_off", "wifi_on", "screen_off", "screen_on"} else "shell"
            steps.append((kind, template.format(pkg=self.pkg), wait))
        return steps

    def _before_version_text(self) -> str:
        """点升级前的实报版本。

        手表待升级页上的固件号是后台目标，不是设备正在跑的版本，不能记进「升级前」。
        眼镜、戒指各自覆盖，读自己页面上的「当前版本」。
        """
        from server.common.version_util import watch_version_display, watch_versions_from_page

        page = ""
        try:
            page = self.cache.page() or ""
        except Exception:
            page = ""
        parsed = watch_versions_from_page(
            page,
            pkg=self.pkg,
            offer_texts=list(self.link.get("offer_texts") or []),
            latest_texts=list(self.link.get("latest_texts") or []),
        )
        shown = watch_version_display(parsed.get("current") or "")
        return "" if _placeholder_version(shown) else shown

    def _version_text(self) -> str:
        """读版本号：优先页面文案，不绑 resourceId。

        最新页「您的固件是最新版本」旁是实报；待升级页「有新版本可以更新」旁是目标。
        备注里仍用尾号（2.0.3），和历史报告一致。
        """
        from server.common.version_util import watch_version_display, watch_versions_from_page

        page = ""
        try:
            page = self.cache.page() or ""
        except Exception:
            page = ""
        parsed = watch_versions_from_page(
            page,
            pkg=self.pkg,
            offer_texts=list(self.link.get("offer_texts") or []),
            latest_texts=list(self.link.get("latest_texts") or []),
        )
        token = parsed.get("current") or parsed.get("target") or ""
        shown = watch_version_display(token) if token else ""
        # 进度页上的「0.0」不是固件号。记进去会让报告看起来像升过一个不存在的版本。
        if token and not _placeholder_version(shown):
            return shown
        # 兜底：旧配置若仍写了 resourceId
        for key in ("version_field", "announced_field"):
            rid = _resource_id(self.link["locators"].get(key))
            if not rid:
                continue
            text = self._text_of_id(rid) or ""
            found = re.search(r"\d+(?:\.\d+)+", text)
            if found and not _placeholder_version(found.group(0)):
                return found.group(0)
            if text.strip() and not _placeholder_version(text.strip()):
                return text.strip()
        return ""

    def _wait_until_connected(self) -> bool:
        """重新升级前：点设备页，滑到顶部，读到「已连接」才准进升级页。

        蓝牙恢复慢时，设备页中间的「固件升级」入口还在，不代表手表已经连上。
        连接文案在页面最上面。不滑到顶就进升级页，等于没看连接。
        """
        raw_timeout = self.settings.get("reconnect_timeout")
        timeout = 180 if raw_timeout is None else int(raw_timeout)
        self._say(f"[{self.case_id}] 先点设备页并滑到顶部，确认已连接后再进入升级页")
        deadline = time.time() + timeout
        announced = False
        last_text = ""
        while True:
            if self.stop_check():
                return False
            self._focus_device_top()
            last_text = (self._connect_text() or "").strip()
            if label_connected(last_text, self.connected_words, self.disconnected_words) is True:
                self._say(f"[{self.case_id}] 设备页顶部已连接（{last_text}），进入升级页")
                return True
            if time.time() >= deadline:
                break
            if not announced:
                shown = last_text or "读不到"
                self._say(f"[{self.case_id}] 设备页顶部还不是已连接（{shown}），继续等，先不进升级页")
                announced = True
            time.sleep(2)
        shown = last_text or "读不到"
        self._say(f"[{self.case_id}] 等了 {timeout} 秒，顶部仍是「{shown}」，不进入升级页")
        return False

    def _focus_device_top(self) -> None:
        """关掉失败弹窗，回到设备页顶部。连接状态只在最上面。"""
        self._dismiss_result_dialog()
        self._leave_result_pages()
        if self._phase() in {"success", "failure"} and not getattr(self, "_result_restarted", False):
            # 弹窗点不掉时按返回也出不去。只重启一次，避免每 2 秒杀一次 App。
            self._result_restarted = True
            self._say(f"[{self.case_id}] 结果弹窗还在，重启 App 再回设备页")
            self._restart_app()
            self._click_tab()
            self._touch()
            time.sleep(0.6)
        on_upgrade = self._phase() in {"ready", "running", "success", "failure"}
        if on_upgrade or not self._wait_device_page(attempts=2, pause=0.4):
            self._back_to_tabs(leave_upgrade=True)
            self._click_tab()
            self._touch()
            time.sleep(0.6)
        self.session.scroll_to_top(self.pkg)
        self._touch()

    def _confirm(self) -> str:
        """收尾：确认升级后设备页仍是已连接。

        升级结束或眼镜重启后，App 常常被系统丢到后台，甚至停在桌面。
        这种情况下页面永远读不到连接文案，只能干等到超时。所以按节奏主动把
        App 拉回前台再读，而不是一直重复读同一张桌面截图。
        """
        timeout = int(self.settings.get("reconnect_timeout") or 180)
        confirms = max(1, int(self.settings.get("hung_confirm_times") or 3))
        deadline = time.time() + timeout
        last = "unseen"
        last_relaunch = 0.0
        while time.time() < deadline:
            last = self._read_link_state()
            if last == "connected":
                return "connected"
            if last == "unseen" and time.time() - last_relaunch >= 10:
                # 读不到 App 页面，说明 App 不在前台。主动拉起来，别空转。
                self.session.prepare_for_app(self.pkg, allow_relaunch=True)
                last_relaunch = time.time()
            time.sleep(2)
        for _ in range(confirms):
            last = self._read_link_state()
            if last == "connected":
                return "connected"
            time.sleep(2)
        return "disconnected" if last == "disconnected" else "unseen"

    def _read_link_state(self) -> str:
        """读设备页连接文案。原来每次判定都要重读好几遍页面，一趟能花掉四十多秒。"""
        running = self._phase() == "running"
        self.session.prepare_for_app(self.pkg, allow_relaunch=not running)
        self._accept_pair()
        self._leave_result_pages()
        if not self._wait_device_page(attempts=4) or self._phase() in {"ready", "success", "failure"}:
            self._back_to_tabs(leave_upgrade=True)
            self._click_tab()
            self._touch()
            time.sleep(1.0)
        if not self._wait_device_page(attempts=6, pause=0.7):
            # 过渡帧常误判；多等几拍仍不在，再重启。日志写清楚是过渡失败。
            self._say(
                f"[{self.case_id} {self.case_name}] "
                f"收尾过渡后仍未识别到设备页，重启 App 再确认"
            )
            self._restart_app()
            self._click_tab()
            self._touch()
            time.sleep(1.2)
        if not self._wait_device_page(attempts=4):
            self._say(f"[{self.case_id} {self.case_name}] 重启后仍不在设备页")
            return "unseen"
        self.session.scroll_to_top(self.pkg)
        judged = label_connected(self._connect_text(), self.connected_words, self.disconnected_words)
        if judged is True:
            return "connected"
        if judged is False:
            return "disconnected"
        return "unseen"

    def _leave_result_pages(self) -> None:
        """点掉成功/失败页，并等到离开结果页再往下走。"""
        for _ in range(5):
            phase = self._phase()
            if phase not in {"success", "failure"}:
                return
            self._click_done()
            self._touch()
            time.sleep(0.9)
            if self._phase() in {"success", "failure"}:
                self._click_back_upgrade()
                time.sleep(0.6)

    def _wait_device_page(self, *, attempts: int = 3, pause: float = 0.6) -> bool:
        """设备页控件刚出来时会有空窗；短等几拍再判，减少误重启。"""
        for index in range(max(1, attempts)):
            if self._on_device_page():
                return True
            if index + 1 >= attempts:
                break
            self._touch()
            time.sleep(pause)
        return self._on_device_page()

    def _click_back_upgrade(self) -> None:
        """退出升级页。

        优先点标题栏返回箭头 iv_title_back（升级页是独立 Activity，返回键也等价）。
        点不到才按返回键。这样失败弹窗关掉后能干净地退回设备页。
        """
        rid = _resource_id(self.link["locators"].get("back_button")) or (
            f"{self.pkg}:id/iv_title_back"
        )
        try:
            node = self.device(resourceId=rid)
            if getattr(node, "exists", False):
                node.click()
                self._touch()
                time.sleep(0.8)
                return
        except Exception:
            pass
        try:
            self.device.press("back")
        except Exception:
            return
        self._touch()
        time.sleep(0.8)

    def _touch(self) -> None:
        """界面将要变化时让缓存失效。基类没有缓存时是空操作。"""
        cache = getattr(self, "cache", None)
        if cache is not None:
            cache.mark_stale()

    def _phase(self) -> str:
        self._ensure_screen_on()
        # 失败弹窗下面往往还留着 22% 的进度条。先认弹窗，否则会当成还在升级，
        # 不去点「完成」，接着在这个弹窗上等三分钟「已连接」。
        if self._failure_dialog_visible():
            return "failure"
        state = self._state_text()
        if self._matches(self.failure_texts, state) or "失败" in state:
            return "failure"
        if "升级成功" in state or "升级完成" in state or self._matches(["升级成功", "升级完成"], state):
            return "success"
        if self._percent() is not None or self._matches(self.running_texts, state) or state.startswith("正在"):
            return "running"
        # 「您的固件是最新版本!」：后台已经没有可升级的包。
        # 这不是升级失败，也不该反复重试——重试多少次都是同一个页面。
        if self._latest_page():
            return "latest"
        if self._exact_node(self.button_text) is not None and (self._offer_visible() or self._has(self.entry_text)):
            return "ready"
        return "other"

    def _latest_page(self) -> bool:
        """升级页显示「您的固件是最新版本!」。

        2026-11-13 实测 Ultima Flex：升级成功后版本从目标 2.0.3 变成实报 2.0.2，
        页面只剩「您的固件是最新版本!」，连升级按钮都没有了。
        这个页面必须单独认出来，否则会被当成「没进升级页」反复重试。
        """
        for phrase in self.link.get("latest_texts") or ["您的固件是最新版本", "已是最新版本"]:
            if phrase and self._has(phrase):
                return True
        return False

    def _ensure_screen_on(self) -> None:
        """屏幕灭着就先点亮，再判断页面。

        屏幕关掉时 uiautomator 只能抓到一份没有 App 文案的空页面，
        `_phase()` 会把它当成「既不在升级、也没有按钮」，进而误判成失败。
        2026-11-12 手表 T08 就是这样挂的：息屏注入后日志里刷了 105 次
        `Could not detect idle state`、68 次 `Node not found`，
        工具据此报「固件升级失败」，可设备其实好好的、页面已连接。

        只保证「判断时屏幕是亮的」。用例自己要测的息屏由 _wait 控制，
        注入动作期间不经过这里，不会被提前点亮。

        注意不要做成「点亮一次就永久跳过」：T08 中途真的会再息屏，
        那时如果不复查，后半程又会读黑屏。所以只在亮着的时候省掉后续开销，
        一旦读到灭屏就立即点亮。
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
        self._say(f"[{self.case_id} {self.case_name}] 屏幕是灭的，先点亮再判断")
        self.session.wake_and_unlock()
        self._touch()
        self._screen_lit = True

    def _offer_visible(self) -> bool:
        for phrase in self.offer_texts:
            if not phrase or "最新" in phrase or len(phrase) < 5:
                continue
            if self._has(phrase):
                return True
        return False

    def _failure_text(self, percent: int | None) -> str:
        for phrase in self.failure_texts:
            if phrase and self._has(phrase):
                return phrase
        return f"升级失败，最后进度 {_percent_label(percent)}"

    def _wearable_charging_label(self) -> str:
        """T09：穿戴设备是否在充电。只认 App 页面，不认手机 dumpsys battery。"""
        from server.drivers.phone_ui import wearable_charging_from_page

        self._reveal_for_charging()
        page = ""
        try:
            page = self.cache.page() or ""
        except Exception:
            try:
                page = self.cache.cached_page() or ""
            except Exception:
                page = ""
        texts = list(self.link.get("charging_texts") or [])
        hit = wearable_charging_from_page(page, self.pkg, texts)
        if hit:
            return hit
        locator = (self.link.get("locators") or {}).get("charging_state")
        if locator:
            value = locator.get("value") if isinstance(locator, dict) else ""
            needle = ""
            if isinstance(value, dict):
                needle = str(value.get("text") or value.get("textContains") or value.get("resourceId") or "")
            else:
                needle = str(value or "")
            if needle and (self._has(needle) or self._exists_id(needle)):
                return needle
        return ""

    def _reveal_for_charging(self) -> None:
        """把设备页（带电量/充电文案的那页）露出来，再读充电状态。"""
        self.session.prepare_for_app(self.pkg, allow_relaunch=True)
        self._back_to_tabs()
        self._click_tab()
        time.sleep(0.6)
        self._touch()

    def _on_device_page(self) -> bool:
        """是否停在设备页。

        只认 App 自己的控件或当前识别到的设备名，**不能**用连接文案兜底：
        状态栏在别的 tab 上也写着「蓝牙已连接」，用它会误判成「已经在设备页」，
        于是不切 tab，接着读不到连接控件，最后判成「没回到设备页」。
        2026-11-13 新表 Ultima Flex 上 T01 升级 100% 成功却被判 FAIL，就是这个原因。

        设备名不能写死在定位里：换表后旧名字会对不上。
        优先 resourceId；没有 id 时用本轮识别到的名字；最后靠连接状态控件。
        升级结果页尚未卸掉时，即使有底部 tab 也不算设备页。
        """
        if self._phase() in {"success", "failure", "running", "ready"}:
            return False
        name_id = _resource_id(self.link["locators"].get("device_name"))
        if name_id and self._exists_id(name_id):
            return True
        device_name = self.link.get("device_name") or ""
        if device_name and self._has_exact(device_name):
            return True
        # 设备页特有的控件：连接状态（有 resourceId 时最稳）。
        state_id = _resource_id(self.link["locators"].get("connect_state"))
        if state_id and self._exists_id(state_id):
            return True
        # 底部设备 tab 已在，且包内能看到「已连接/未连接」字样（仍避免只认状态栏）。
        tab_id = _resource_id(self.link["locators"].get("device_tab"))
        if tab_id and self._exists_id(tab_id):
            page = ""
            try:
                page = self.cache.cached_page() or self.cache.page() or ""
            except Exception:
                page = ""
            body = page
            if self.pkg and f'package="{self.pkg}"' in page:
                # 粗切：只在本包节点附近找连接文案
                idx = page.find(f'package="{self.pkg}"')
                body = page[idx : idx + 8000] if idx >= 0 else page
            for word in self.connected_words + self.disconnected_words:
                if word and f'text="{word}"' in body:
                    return True
        return False

    def _connect_text(self) -> str:
        """读 App 内的连接文案。

        先按 resourceId 读；退回到文字时**必须限定包名**，
        否则会读到状态栏的「蓝牙已连接」，把没连接也判成已连接。
        """
        rid = _resource_id(self.link["locators"].get("connect_state"))
        if rid:
            text = self._text_of_id(rid)
            if text:
                return text
        for word in self.connected_words + self.disconnected_words:
            if not word:
                continue
            try:
                node = self.device(text=word, packageName=self.pkg)
                if getattr(node, "exists", False):
                    return word
            except Exception:
                continue
        return ""

    def _state_text(self) -> str:
        rid = _resource_id(self.link["locators"].get("upgrade_state"))
        if not rid:
            return ""
        return self._text_of_id(rid) or ""

    def _percent(self) -> int | None:
        rid = _resource_id(self.link["locators"].get("upgrade_percent"))
        if not rid:
            return None
        text = self._text_of_id(rid)
        if text is None:
            return None
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return None
        return int(digits)

    def _login_blocking(self) -> bool:
        for word in self.link.get("login_texts") or []:
            if word and self._has_exact(word):
                return True
        return False

    def _accept_pair(self) -> bool:
        """点掉配对弹窗。

        `exists` 会真的去服务端查节点，节点不在时会抛异常，所以整段都要兜住。
        这个函数在升级等待循环里每两秒调一次，抛一次就能把整个任务打挂。
        """
        for phrase in ("配对", "配对并连接"):
            try:
                node = self.device(text=phrase)
                if not getattr(node, "exists", False):
                    continue
                node.click()
            except Exception:
                continue
            safe_print("  accepted pair")
            time.sleep(1)
            return True
        return False

    def _reveal(self, phrase: str) -> bool:
        if self._has(phrase):
            return True
        for _ in range(int(self.link.get("scrolls") or 6)):
            self.session.scroll_forward(self.pkg)
            if self._has(phrase):
                return True
        node = self.session._scroller(self.pkg)
        scroll = getattr(node, "scroll", None) if node is not None and getattr(node, "exists", False) else None
        if scroll is not None:
            try:
                scroll.toEnd(steps=20)
                time.sleep(0.3)
            except Exception:
                pass
        if self._has(phrase):
            return True
        if self.session.foreground(self.pkg):
            for _ in range(3):
                self.session._swipe_up()
                time.sleep(0.4)
                if self._has(phrase):
                    return True
        return False

    def _matches(self, phrases: list[str], extra: str) -> bool:
        for phrase in phrases:
            if phrase and (phrase in (extra or "") or self._has(phrase)):
                return True
        return False

    def _has(self, phrase: str) -> bool:
        """页面文字里找关键词。

        原来每个关键词都要发一次 JSON-RPC 查询（一次约 0.4 秒），一轮判定十来个
        关键词就要四五秒。现在优先在已抓到的页面 dump 里找，命中不了才退回控件查询。
        """
        if not phrase:
            return False
        cache = getattr(self, "cache", None)
        if cache is not None:
            page = cache.cached_page()
            if page is not None:
                return phrase in page
        try:
            if self.device(textContains=phrase, packageName=self.pkg).exists:
                return True
        except Exception:
            pass
        if self.session.foreground(self.pkg):
            try:
                return bool(self.device(textContains=phrase).exists)
            except Exception:
                return False
        return False

    def _has_exact(self, phrase: str) -> bool:
        return self._exact_node(phrase) is not None

    def _dismiss_result_dialog(self) -> None:
        """进度条还在时，失败/成功弹窗不一定写进升级状态控件。先点掉，点不掉就重启一次。"""
        if not self._failure_dialog_visible() and not self._result_dialog_live():
            return
        self._click_done()
        self._touch()
        if not self._result_dialog_live():
            return
        if getattr(self, "_result_restarted", False):
            return
        self._result_restarted = True
        self._say(f"[{self.case_id}] 结果弹窗还在，重启 App 再回设备页")
        self._restart_app()
        self._click_tab()
        self._touch()

    def _failure_dialog_visible(self) -> bool:
        """这份页面 dump 里已经能看到失败文案。不额外发查询。"""
        page = ""
        cache = getattr(self, "cache", None)
        if cache is not None:
            page = cache.cached_page() or ""
            if not page:
                try:
                    page = cache.page() or ""
                except Exception:
                    page = ""
        return any(phrase and phrase in page for phrase in self.failure_texts)

    def _result_dialog_live(self) -> bool:
        """dump 经常漏掉盖在上面的弹窗，重连前再实查一次文字和 content-desc。"""
        phrases = list(self.failure_texts) + ["升级成功", "升级完成"]
        for phrase in phrases:
            if phrase and self._live_phrase(phrase):
                return True
        return False

    def _live_phrase(self, phrase: str) -> bool:
        for kwargs in (
            {"textContains": phrase, "packageName": self.pkg},
            {"descriptionContains": phrase, "packageName": self.pkg},
        ):
            try:
                node = self.device(**kwargs)
            except Exception:
                continue
            if getattr(node, "exists", False):
                return True
        return False

    def _described_node(self, phrase: str):
        if not phrase:
            return None
        try:
            node = self.device(description=phrase, packageName=self.pkg)
            if getattr(node, "exists", False):
                return node
        except Exception:
            return None
        return None

    def _exact_node(self, phrase: str):
        if not phrase:
            return None
        try:
            node = self.device(text=phrase, packageName=self.pkg)
            if getattr(node, "exists", False):
                return node
        except Exception:
            pass
        if not self.session.foreground(self.pkg):
            return None
        try:
            node = self.device(text=phrase)
        except Exception:
            return None
        return node if getattr(node, "exists", False) else None

    def _text_of_id(self, rid: str) -> str | None:
        """读指定控件的文字。控件不存在、或者查询过程本身抛异常，都当读不到处理。

        这里必须把 `exists`、`info` 一起包进 try：uiautomator2 的 exists 不是纯属性，
        它会真的去服务端查节点，节点不在时抛 UiObjectNotFoundException(-32001)。
        2026-11-11 手表 T04 就是这样把整个任务打挂的——重新点击升级后页面还停在入口页，
        没有进度控件，工具却按「运行中」去读百分比。
        """
        try:
            node = self.device(resourceId=rid)
            if not getattr(node, "exists", False):
                return None
            return _node_text(node)
        except Exception:
            return None

    def _exists_id(self, rid: str) -> bool:
        try:
            return bool(self.device(resourceId=rid).exists)
        except Exception:
            return False

    def _env_ok(self) -> tuple[bool, str]:
        probe = BluetoothProbe(self.adb, UiDriver(self.device), self.link, self.settings.get("patterns"))
        return probe.env_ok()

    def _shot(self, label: str) -> None:
        path = _allocate(self.folder, f"{self.case_id}_{label}.png")
        try:
            self.device.screenshot(str(path))
        except Exception:
            pass

    def _save_log(self, label: str) -> None:
        path = _allocate(self.folder, f"{self.case_id}_{label}.log")
        try:
            text = self.adb.shell("logcat -d -t 1500", check=False)
        except Exception:
            return
        path.write_text(text or "", encoding="utf-8")

    def _row(self, case_id: str, code: str, note: str, started: float) -> dict:
        return {
            "case_id": case_id,
            "name": self.case_name,
            "direction": direction_label(self.direction),
            "code": code,
            "note": note,
            "elapsed": round(time.time() - started, 1),
            "trigger": self.trigger,
            "before_version": self.before_version,
            "after_version": self.after_version,
            "announced": self.announced,
        }


def _locator_text(locator, default: str) -> str:
    if not isinstance(locator, dict):
        return default
    value = locator.get("value")
    return value if isinstance(value, str) and value else default


def _resource_id(locator) -> str:
    if not isinstance(locator, dict):
        return ""
    value = locator.get("value")
    if locator.get("type") == "resourceId" and isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("resourceId") or ""
    return ""


def _node_text(node) -> str:
    """取节点文字。取 `info` 这一步也会去服务端查节点，节点没了就抛异常，所以整段兜住。"""
    try:
        info = getattr(node, "info", None)
        if callable(info):
            try:
                info = info()
            except Exception:
                info = {}
        if isinstance(info, dict):
            return str(info.get("text") or info.get("contentDescription") or "")
    except Exception:
        return ""
    return ""


def _placeholder_version(value: str) -> bool:
    """进度条占位和电量，不是固件号。"""
    text = (value or "").strip()
    if not text:
        return True
    return bool(re.fullmatch(r"0+(?:\.0+)+", text) or re.fullmatch(r"100(?:\.0+)?", text))


def _percent_label(percent: int | None) -> str:
    return "无" if percent is None else f"{percent}%"


def _allocate(folder: Path, name: str) -> Path:
    path = folder / name
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    index = 2
    while True:
        candidate = folder / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


if __name__ == "__main__":
    main()
