"""DaFit 手表链路。

有新版本时，升级页只显示目标版本，当前版本留在应用上次连接时保存的记录里。
正式包没有调试标记，读不到这份记录时，只能等升级页回到「当前版本」文案。
"""

from __future__ import annotations

import re
import time

from server.drivers.phone_ui import PhoneSession, label_connected
from server.links.base_link import BaseLink, _version_only


class WatchLink(BaseLink):
    link_id = "watch"

    def prepare_home(self) -> None:
        """收起通知栏并回到 DaFit。设备页入口是底部图标，没有文字。"""
        PhoneSession(self.ui.device, self.adb).prepare_for_app(self.link["pkg"])
        if self._on_device_page() or self.ui.exists(self.link["locators"].get("upgrade_button")):
            return
        if self.ui.exists(self.link["locators"].get("upgrade_entry")):
            return
        tab = self.link["locators"].get("device_tab")
        if self.ui.exists(tab):
            self.ui.click("device_tab", tab)
            self.sleep(1)

    def _bring_foreground(self) -> None:
        pkg = self.link["pkg"]
        front = self.adb.foreground()
        if pkg in front and self.ui.exists(self.link["locators"].get("device_tab")):
            return
        self.launch()
        self.sleep(1.5)

    def _on_device_page(self) -> bool:
        return self.ui.exists(self.link["locators"].get("device_name")) or self.ui.exists(
            self.link["locators"].get("connect_state")
        )

    def open_upgrade(self) -> None:
        entry = self.link["locators"].get("upgrade_entry")
        if self.ui.exists(self.link["locators"].get("upgrade_button")):
            self.log("已在固件升级页")
            return
        self._scroll_to(entry)
        self.ui.click("upgrade_entry", entry)
        self.sleep(1)
        self.log("进入固件升级页")

    def watch(self, where: str) -> dict:
        """每步主动看当前页、连接状态和系统弹窗。"""
        page = self.ui.page_text()
        popup = ""
        for word in ("配对", "允许", "确定", "知道了"):
            if word in page:
                popup = word
                break
        connected = "已连接" in page
        state = {
            "where": where,
            "connected_text": connected,
            "upgrade": "升级" in page,
            "success": "升级成功" in page,
            "popup": popup,
        }
        self.log(f"观察 {where}: 连接文字={connected} 升级按钮={'升级' in page} 弹窗={popup or '无'}")
        if popup:
            self.ui.click("popup", {"type": "text", "value": popup})
            self.log(f"已处理弹窗: {popup}")
        return state

    def confirm_reconnect(self, timeout: int = 90) -> tuple[bool, str]:
        """升级成功后回到设备页顶部。出现配对请求就点配对，再看是否已连接。"""
        done = self.link["locators"].get("done_button")
        if done and self.ui.exists(done):
            self.ui.click("done_button", done)
            self.log("点击完成")
            self.sleep(1)
        self.prepare_home()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.watch("重连")
            if self.ui.exists({"type": "text", "value": "配对"}):
                self.ui.click("pair", {"type": "text", "value": "配对"})
                self.log("已点配对")
                self.sleep(2)
            self._scroll_top()
            if self._device_connected():
                self.log("设备页顶部已连接")
                return True, "配对后已连接"
            self.sleep(2)
        self._scroll_top()
        if self._device_connected():
            self.log("设备页顶部已连接")
            return True, "配对后已连接"
        return False, "升级成功后设备页未显示已连接"

    def _device_connected(self) -> bool:
        """读连接状态控件的文字。控件在「未连接」时也存在，不能只看控件在不在。"""
        locator = self.link["locators"].get("connect_state") or {}
        value = locator.get("value")
        resource_id = value.get("resourceId") if isinstance(value, dict) else ""
        text = ""
        if resource_id:
            text = self.ui.read_text({"type": "resourceId", "value": resource_id})
        if not text:
            text = self.ui.read_text(locator)
        judged = label_connected(text, self.link.get("ui_connected") or [], self.link.get("ui_disconnected") or [])
        return judged is True

    def _scroll_top(self) -> None:
        PhoneSession(self.ui.device, self.adb).scroll_to_top(self.link["pkg"])

    def upgrade_available(self) -> bool:
        if self.ui.exists(self.link["locators"].get("offer_text")) and self.ui.exists(
            self.link["locators"].get("upgrade_button")
        ):
            return True
        entry = self.link["locators"].get("upgrade_entry")
        if self.ui.exists(entry):
            return True
        self.prepare_home()
        return self._scroll_to(entry)

    def read_firmware(self) -> dict:
        stored = self._stored_versions()
        on_page = self._page_versions()
        if on_page.get("current"):
            reported = on_page["current"]
            announced = on_page.get("target") or stored.get("announced") or ""
        else:
            # 有升级提示时，页面上的版本号是目标版本，不是手表正在跑的版本。
            reported = stored.get("reported") or ""
            announced = on_page.get("target") or stored.get("announced") or ""
        if announced and reported and announced == reported:
            announced = stored.get("announced") or ""
        return {
            "reported": reported,
            "announced": announced if announced != reported else "",
            "githash": "",
        }

    def _page_versions(self) -> dict:
        from server.common.version_util import watch_version_display, watch_versions_from_page

        page = self.ui.page_text()
        parsed = watch_versions_from_page(
            page,
            pkg=self.link.get("pkg"),
            offer_texts=list(self.link.get("offer_texts") or []),
            latest_texts=list(self.link.get("latest_texts") or []),
        )
        current = watch_version_display(parsed.get("current") or "")
        target = watch_version_display(parsed.get("target") or "")
        if current or target:
            return {"current": current, "target": target}
        # 兜底：配置仍指向具体控件时
        current = _version_only(self.ui.read_text(self.link["locators"].get("version_field")))
        target = _version_only(self.ui.read_text(self.link["locators"].get("announced_field")))
        offer = self.ui.read_text(self.link["locators"].get("offer_text"))
        offer_words = self.link.get("offer_texts") or []
        if offer and any(word in offer for word in offer_words):
            return {"current": "", "target": target or current}
        if current and not target:
            return {"current": current, "target": ""}
        return {"current": current, "target": target}

    def _stored_versions(self) -> dict:
        if not self.adb.package_debuggable(self.link["pkg"]):
            self.log("正式包读不到应用内保存的当前版本，只使用升级页文字", "WARN")
            return {}
        files = (self.link.get("version_store") or {}).get("files") or {}
        reported = self._pref(files.get("current") or "")
        announced = self._pref(files.get("target") or "")
        return {"reported": _tail_version(reported), "announced": _tail_version(announced)}

    def _pref(self, spec: str) -> str:
        if ":" not in spec:
            return ""
        filename, key = spec.split(":", 1)
        script = f"grep 'name=\"{key}\"' shared_prefs/{filename}"
        raw = self.adb.run_as(self.link["pkg"], script)
        match = re.search(r">([^<]+)<", raw)
        return match.group(1).strip() if match else ""

    def _scroll_to(self, locator: dict) -> bool:
        if not locator or self.ui.exists(locator):
            return bool(locator)
        session = PhoneSession(self.ui.device, self.adb)
        scrolls = int(self.link.get("scrolls") or 6)
        for _ in range(scrolls):
            session.scroll_forward(self.link["pkg"])
            self.sleep(0.3)
            if self.ui.exists(locator):
                return True
        return False


def _tail_version(value: str) -> str:
    found = re.search(r"\d+(?:\.\d+)+$", value or "")
    return found.group(0) if found else _version_only(value)
