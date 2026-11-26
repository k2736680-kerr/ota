"""单轮状态机 S0～S7。时间由调用方传入，方便单测快进。"""

from __future__ import annotations

from dataclasses import dataclass, field

from server.engine import ABORT_ENV, FAIL_UPGRADE, HUNG_DEVICE, PASS, SKIP, STALE_POLICY
from server.engine.judge import HungTracker, finish_round
from server.engine.strategy import check_after_s1, check_before, check_s2_direction


STATES = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]


@dataclass
class RoundResult:
    code: str
    note: str
    state: str
    trigger: str = ""
    before_version: str = ""
    after_version: str = ""
    announced: str = ""
    direction: str = ""
    elapsed: float = 0
    saw_disconnect: bool = False
    evidence: dict = field(default_factory=dict)


class RoundMachine:
    def __init__(self, link, case, settings, link_runner, probe, clock) -> None:
        self.link = link
        self.case = case
        self.settings = settings
        self.runner = link_runner
        self.probe = probe
        self.clock = clock
        self.state = "S0"
        self.tracker = HungTracker(
            settings["drop_window"],
            settings["reconnect_timeout"],
            settings["hung_confirm_times"],
            settings["probe_interval"],
        )
        self.started = clock()
        self.upgrade_clicked_at: float | None = None
        self.injected = False
        self.inject_failed = False
        self.trigger = ""
        self.saw_disconnect = False
        self.reconnected = False
        self.upgrade_seen = False
        self.before_version = ""
        self.after_version = ""
        self.announced = ""
        self.direction = ""
        self.note = ""
        self.code = ""
        self.inject_actions = list(case.get("inject") or [])

    def run(self, stop_check, previous_direction: str | None = None) -> RoundResult:
        self._s0(previous_direction)
        if self.code:
            return self._finish()
        self._s1(stop_check)
        if self.code:
            return self._finish()
        self._s2_to_s5(stop_check)
        if self.code:
            return self._finish()
        self._s6(previous_direction)
        return self._finish()

    def _s0(self, previous_direction: str | None) -> None:
        self.state = "S0"
        ok, reason = self.probe.env_ok()
        if not ok:
            self._stop(ABORT_ENV, reason)
            return
        if hasattr(self.runner, "watch"):
            self.runner.watch("开轮")
        env = self.runner.precheck()
        if env.get("battery") is not None and env["battery"] < self.settings["min_battery"]:
            self._stop(ABORT_ENV, f"手机电量 {env['battery']} 低于 {self.settings['min_battery']}")
            return
        if self.case.get("require_charging") and not env.get("charging"):
            self._stop(SKIP, "穿戴设备未在充电，T09 不执行")
            return
        if not env.get("logged_in", True):
            self._stop(ABORT_ENV, "APP 未登录")
            return
        self.before_version = env.get("reported") or ""
        self.announced = env.get("announced") or ""
        passed, code, why = check_before(
            self.case.get("strategy"),
            self.before_version,
            self.announced,
            env.get("entry", False),
            same_version=bool(self.case.get("same_version")),
        )
        if not passed:
            self._stop(code, why)
            return
        if self.case.get("strategy") == "s2" and previous_direction:
            direction = f"{self.before_version}->{self.announced}"
            ok_dir, why_dir = check_s2_direction(previous_direction, direction)
            if not ok_dir:
                if self.settings.get("s2_model") == "single":
                    self._stop("PAUSE", why_dir)
                else:
                    self.note = why_dir

    def _s1(self, stop_check) -> None:
        self.state = "S1"
        if stop_check():
            self._stop(SKIP, "任务已停止")
            return
        try:
            self.runner.open_upgrade()
            if hasattr(self.runner, "watch"):
                self.runner.watch("升级前")
            self.runner.tap_upgrade()
            if hasattr(self.runner, "watch"):
                self.runner.watch("升级后")
        except Exception as exc:
            self._stop(FAIL_UPGRADE, f"进入升级或点击失败: {exc}")
            return
        self.upgrade_clicked_at = self.clock()

    def _s2_to_s5(self, stop_check) -> None:
        deadline = self.upgrade_clicked_at + self.settings["upgrade_total_timeout"]
        while self.clock() < deadline:
            if stop_check():
                self._stop(SKIP, "任务已停止")
                return
            now = self.clock()
            if self.inject_actions and not self.injected:
                hit, how = self.runner.inject_ready(self.case, now - self.upgrade_clicked_at)
                if hit:
                    self.state = "S3"
                    self.trigger = how
                    ok, why = self.runner.inject(self.inject_actions)
                    self.injected = True
                    if not ok:
                        self.inject_failed = True
                        self._stop(SKIP, why or "注入命令失败")
                        return
                    self.upgrade_seen = False
            probe = self.probe.connected()
            # 升级页已经给出进度或结果时，以页面为准，不用系统蓝牙快照抢先判死机。
            progress = self.runner.ui.progress_percent(self.link["locators"].get("progress_text"))
            page = self.runner.ui.page_text()
            if (self.runner.upgrade_success() or progress is not None) and "未连接" not in page:
                probe.connected = True
            verdict = self.tracker.see(probe.connected, now)
            if verdict == "connected":
                if self.saw_disconnect:
                    self.reconnected = True
                    self.state = "S6"
                    return
                if self.runner.upgrade_success() and not self._needs_retry():
                    self.upgrade_seen = True
                    # 增量包可能不断连。没有注入、或注入已经做完，就可以收口。
                    if not self.inject_actions or self.injected:
                        self.state = "S6"
                        return
            else:
                self.saw_disconnect = True
                self.state = "S4" if verdict == "waiting" else "S5"
                if verdict == "hung":
                    env_ok, env_reason = self.probe.env_ok()
                    if not env_ok:
                        self._stop(ABORT_ENV, env_reason)
                        return
                    self._stop(HUNG_DEVICE, "断开后超时未重连")
                    return
            self.runner.sleep(self.settings["probe_interval"])
        if self._needs_retry():
            self._retry(stop_check)
            return
        self._stop(FAIL_UPGRADE, "单轮升级超时")

    def _needs_retry(self) -> bool:
        from server.drivers.phone_ui import allows_retry

        return bool(self.injected and self.inject_actions and allows_retry(self.case, self.link) and not self.upgrade_seen)

    def _retry(self, stop_check) -> None:
        """干扰已经打断这一次。回到升级入口再点一次，这一次必须成功。"""
        self.note = "这一次已被打断，重新点击升级"
        try:
            self.runner.finish_upgrade()
            self.runner.open_upgrade()
            self.runner.tap_upgrade()
        except Exception as exc:
            self._stop(FAIL_UPGRADE, f"干扰后重新升级失败: {exc}")
            return
        self.upgrade_clicked_at = self.clock()
        self.upgrade_seen = False
        self.saw_disconnect = False
        self.reconnected = False
        self.tracker.reset()
        deadline = self.upgrade_clicked_at + self.settings["upgrade_total_timeout"]
        while self.clock() < deadline:
            if stop_check():
                self._stop(SKIP, "任务已停止")
                return
            now = self.clock()
            probe = self.probe.connected()
            progress = self.runner.ui.progress_percent(self.link["locators"].get("progress_text"))
            page = self.runner.ui.page_text()
            if (self.runner.upgrade_success() or progress is not None) and "未连接" not in page:
                probe.connected = True
            verdict = self.tracker.see(probe.connected, now)
            if verdict == "connected":
                if self.saw_disconnect:
                    self.reconnected = True
                    self.state = "S6"
                    return
                if self.runner.upgrade_success():
                    self.upgrade_seen = True
                    self.state = "S6"
                    return
            else:
                self.saw_disconnect = True
                self.state = "S4" if verdict == "waiting" else "S5"
                if verdict == "hung":
                    env_ok, env_reason = self.probe.env_ok()
                    if not env_ok:
                        self._stop(ABORT_ENV, env_reason)
                        return
                    self._stop(HUNG_DEVICE, "重新升级后断开超时未重连")
                    return
            self.runner.sleep(self.settings["probe_interval"])
        self._stop(FAIL_UPGRADE, "重新升级超时")

    def _s6(self, previous_direction: str | None) -> None:
        self.state = "S6"
        after = self.runner.read_versions()
        self.after_version = after.get("reported") or self.before_version
        if after.get("announced"):
            self.announced = after["announced"]
        self.direction = f"{self.before_version}->{self.after_version or self.announced}"
        if self.runner.upgrade_success():
            self.upgrade_seen = True
        version_ok = True
        if self.case.get("strategy") == "s1" and self.case.get("same_version", True):
            # 同版本循环只要求还能升级，不要求实报版本变化。
            if not self.upgrade_seen:
                self._stop(FAIL_UPGRADE, "S1 未看到升级成功")
                return
            self.runner.finish_upgrade()
            if hasattr(self.runner, "confirm_reconnect"):
                ok, why = self.runner.confirm_reconnect(self.settings["reconnect_timeout"])
                if not ok:
                    self._stop(HUNG_DEVICE, why)
                    return
                self.reconnected = True
            if not self.runner.upgrade_available():
                self._stop(STALE_POLICY, "S1 完成后升级入口消失，同版本循环已中断")
                return
        elif self.case.get("strategy") == "s1":
            version_ok, why = check_after_s1(self.before_version, self.after_version)
            if not version_ok:
                self._stop(STALE_POLICY, why)
                return
        if self.case.get("strategy") == "s2" and previous_direction:
            ok_dir, why_dir = check_s2_direction(previous_direction, f"{self.before_version}->{self.announced}")
            if not ok_dir and not self.note:
                self.note = why_dir
        if self.saw_disconnect and not self.reconnected:
            version_ok = False
        code, note = finish_round(
            saw_disconnect=self.saw_disconnect,
            reconnected=self.reconnected,
            version_ok=version_ok,
            env_ok=True,
            env_reason="",
            upgrade_seen=self.upgrade_seen,
            injected=self.injected,
            inject_failed=self.inject_failed,
        )
        self.code = code
        self.note = note if not self.note else f"{note}；{self.note}"

    def _stop(self, code: str, note: str) -> None:
        self.code = code
        self.note = note

    def _finish(self) -> RoundResult:
        self.state = "S7"
        return RoundResult(
            code=self.code or FAIL_UPGRADE,
            note=self.note,
            state=self.state,
            trigger=self.trigger,
            before_version=self.before_version,
            after_version=self.after_version,
            announced=self.announced,
            direction=self.direction,
            elapsed=round(self.clock() - self.started, 2),
            saw_disconnect=self.saw_disconnect,
        )
