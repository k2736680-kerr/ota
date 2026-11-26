"""公共流程：拉起 APP、进升级页、点升级、等待、读版本。"""

from __future__ import annotations

import time


class BaseLink:
    def __init__(self, link: dict, adb, ui, logger, sleeper=None) -> None:
        self.link = link
        self.adb = adb
        self.ui = ui
        self.logger = logger
        self.sleeper = sleeper or time.sleep
        self._hierarchy_override = None

    def log(self, message: str, level: str = "INFO") -> None:
        if self.logger:
            self.logger.log(message, level)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleeper(seconds)

    def shell(self, template: str) -> str:
        command = template.format(pkg=self.link["pkg"])
        self.log(f"shell {command}")
        return self.adb.shell(command, check=False)

    def precheck(self) -> dict:
        self.launch()
        self.prepare_home()
        versions = self.read_firmware()
        page = self.ui.page_text()
        logged_in = not any(word in page for word in self.link.get("login_texts") or [])
        entry = self.upgrade_available()
        # battery 仍是手机电量（防手机自己断电）；charging 改成穿戴设备是否在充电。
        level, _phone_charging = self.adb.battery()
        from server.drivers.phone_ui import wearable_charging_from_page

        device_charging = bool(
            wearable_charging_from_page(
                page,
                self.link.get("pkg") or "",
                list(self.link.get("charging_texts") or []),
            )
        )
        device_name = self.ui.read_text(self.link["locators"].get("device_name"))
        return {
            "reported": versions.get("reported") or "",
            "announced": versions.get("announced") or "",
            "logged_in": logged_in,
            "entry": entry,
            "battery": level,
            "charging": device_charging,
            "githash": versions.get("githash") or "",
            "device_model": device_name or self.link.get("device_name") or "",
        }

    def prepare_home(self) -> None:
        """收起通知栏、点亮已锁屏的手机，再保证本 App 在前台。"""
        try:
            from server.drivers.phone_ui import PhoneSession

            PhoneSession(self.ui.device, self.adb).prepare_for_app(self.link["pkg"])
        except Exception as exc:
            self.log(f"回到前台失败: {exc}", "WARN")

    def upgrade_available(self) -> bool:
        return self.ui.exists(self.link["locators"].get("upgrade_button")) or self.ui.exists(
            self.link["locators"].get("upgrade_entry")
        )

    def read_firmware(self) -> dict:
        reported = _version_only(self.ui.read_text(self.link["locators"].get("version_field")))
        announced = _version_only(self.ui.read_text(self.link["locators"].get("announced_field")))
        if announced and not reported:
            reported = announced
            announced = ""
        return {
            "reported": reported,
            "announced": announced,
            "githash": self.ui.read_text(self.link["locators"].get("githash_field")),
        }

    def launch(self) -> None:
        self.shell(self.link["commands"]["launch"])

    def open_upgrade(self) -> None:
        entry = self.link["locators"].get("upgrade_entry")
        if entry:
            self.ui.click("upgrade_entry", entry)
            self.log("进入升级页")

    def tap_upgrade(self) -> None:
        how = self.ui.click("upgrade_button", self.link["locators"]["upgrade_button"])
        self.log(f"点击升级 ({how})")

    def confirm_reconnect(self, timeout: int = 90) -> tuple[bool, str]:
        return True, ""

    def finish_upgrade(self) -> None:
        """成功页还有「完成」时点掉，下一轮才能回到升级入口。"""
        done = (self.link.get("locators") or {}).get("done_button")
        if done and self.ui.exists(done):
            self.ui.click("done_button", done)
            self.log("点击完成")
            self.sleep(1)

    def upgrade_success(self) -> bool:
        return bool(self.ui.contains_any(self.link.get("success_texts") or []))

    def read_versions(self) -> dict:
        versions = self.read_firmware()
        return {"reported": versions.get("reported") or "", "announced": versions.get("announced") or ""}

    def inject_ready(self, case: dict, elapsed: float) -> tuple[bool, str]:
        from server.drivers.phone_ui import inject_due

        trigger = case.get("trigger") or {}
        keywords = list(trigger.get("keywords") or self.link["locators"].get("progress_keywords") or [])
        progress = self.ui.progress_percent(self.link["locators"].get("progress_text"))
        threshold = int(trigger.get("progress_ge", 20))
        fallback = float(trigger.get("fallback_delay", 15))
        page = self.ui.page_text()
        on_page = progress is not None or bool(self.ui.contains_any(keywords))
        if not inject_due(progress, page, keywords, on_page, elapsed, threshold, fallback):
            return False, ""
        if progress is not None and progress >= threshold:
            return True, f"进度:{progress}"
        hit = self.ui.contains_any(keywords)
        if hit:
            return True, f"关键词:{hit}"
        return True, "兜底触发"

    def inject(self, actions: list[dict]) -> tuple[bool, str]:
        for step in actions:
            name = step["action"]
            if name == "unlock":
                try:
                    from server.drivers.phone_ui import PhoneSession

                    self.log("注入 unlock: 按锁屏状态上滑")
                    PhoneSession(self.ui.device, self.adb).wake_and_unlock()
                except Exception as exc:
                    return False, f"unlock 失败: {exc}"
                self.sleep(float(step.get("wait_after") or 0))
                continue
            if name in {"screen_off", "screen_on"}:
                try:
                    from server.drivers.phone_ui import PhoneSession

                    session = PhoneSession(self.ui.device, self.adb)
                    hold = float(step.get("wait_after") or 0)
                    if name == "screen_off":
                        self.log(f"注入 screen_off，保持 {int(hold)} 秒并继续监测")
                        session.screen_off()
                        self._sleep_screen_off(hold)
                    else:
                        self.log("注入 screen_on")
                        session.screen_on()
                        self.sleep(hold)
                except Exception as exc:
                    return False, f"{name} 失败: {exc}"
                continue
            template = (self.link.get("commands") or {}).get(name)
            if not template:
                return False, f"链路未配置动作 {name}"
            try:
                command = template.format(pkg=self.link["pkg"])
                self.log(f"注入 {name}: {command}")
                self.adb.shell(command, check=True)
            except Exception as exc:
                return False, f"{name} 失败: {exc}"
            self.sleep(float(step.get("wait_after") or 0))
        self.log("注入完成: " + ",".join(item["action"] for item in actions))
        return True, ""


    def _sleep_screen_off(self, seconds: float) -> None:
        """dry-run / 状态机路径：息屏等待也要打点，别一整段静默像死机。"""
        deadline = time.time() + max(0.0, float(seconds))
        last = 0.0
        while time.time() < deadline:
            now = time.time()
            if now - last >= 10:
                self.log(f"息屏中，还剩 {max(0, int(deadline - now))} 秒")
                last = now
            self.sleep(min(2.0, max(0.0, deadline - time.time())))


def _version_only(text: str) -> str:
    import re

    if not text:
        return ""
    found = re.search(r"\d+(?:\.\d+)+", text)
    return found.group(0) if found else ""
