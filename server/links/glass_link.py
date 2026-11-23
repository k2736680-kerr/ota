"""daecho 眼镜链路。

路径是首页 → 设置 → 固件 → 升级。
升级页同时有主控和影音，任务开始前按勾选只点需要的那一块。
启动弹窗是 App 自己的更新，只点取消，不点下载。
"""

from __future__ import annotations

import re

from server.drivers.phone_ui import PhoneSession
from server.links.base_link import BaseLink
from server.links.firmware_select import module_labels, selected_modules


class GlassLink(BaseLink):
    link_id = "glass"

    def __init__(self, link: dict, adb, ui, logger, sleeper=None) -> None:
        super().__init__(link, adb, ui, logger, sleeper)
        self.firmware_targets = [item["id"] for item in selected_modules(link)]

    def set_firmware_targets(self, targets: list[str]) -> None:
        self.firmware_targets = list(targets)
        self.log("本次固件: " + module_labels(selected_modules(self.link, targets)))

    def read_firmware(self) -> dict:
        """按模块标题旁「当前版本：」读版本，不依赖 resourceId。"""
        page = self.ui.page_text()
        versions = []
        for module in selected_modules(self.link, self.firmware_targets):
            found = re.search(
                re.escape(module["title"]) + r".{0,240}?当前版本：\s*([^<\"]+)",
                page,
                re.S,
            )
            versions.append(found.group(1).strip() if found else "")
        if any(versions):
            return {
                "reported": " | ".join(versions),
                "announced": "",
                "githash": self.ui.read_text(self.link["locators"].get("githash_field")),
            }
        # dry-run 假页面仍可能是「实报/公布」文案，或只剩「当前版本：」节点。
        fallback = super().read_firmware()
        if fallback.get("reported"):
            return fallback
        reported = re.search(r"实报[:：]\s*([^\s]+)", page)
        announced = re.search(r"公布[:：]\s*([^\s]+)", page)
        return {
            "reported": reported.group(1) if reported else "",
            "announced": announced.group(1) if announced else "",
            "githash": fallback.get("githash") or "",
        }

    def prepare_home(self) -> None:
        PhoneSession(self.ui.device, self.adb).prepare_for_app(self.link["pkg"])
        self.dismiss_app_update()

    def dismiss_app_update(self) -> bool:
        """关掉 App 自身更新。标题里带版本号，不能当成眼镜固件。"""
        locators = self.link.get("locators") or {}
        cancel = locators.get("app_update_cancel") or {"type": "text", "value": "取消"}
        titles = self.link.get("app_update_titles") or ["发现新版本"]
        page = self.ui.page_text()
        if not any(title in page for title in titles) and not self.ui.exists(cancel):
            return False
        if "取消" not in page and not self.ui.exists(cancel):
            return False
        self.ui.click("app_update_cancel", cancel)
        self.log("已关闭 App 自身更新弹窗")
        self.sleep(0.8)
        return True

    def open_upgrade(self) -> None:
        locators = self.link["locators"]
        self.prepare_home()
        if self.ui.exists(locators.get("upgrade_button")) and self._on_module_page():
            self.log("已在固件升级页")
            return
        if not self.ui.exists(locators.get("firmware_menu")):
            settings = locators.get("settings_tab")
            if settings and self.ui.exists(settings):
                self.ui.click("settings_tab", settings)
                self.sleep(1)
        menu = locators.get("firmware_menu")
        if menu and self.ui.exists(menu) and not self.ui.exists(locators.get("upgrade_entry")):
            self.ui.click("firmware_menu", menu)
            self.sleep(1)
        entry = locators.get("upgrade_entry") or locators.get("upgrade_entry_text")
        if entry and self.ui.exists(entry):
            self.ui.click("upgrade_entry", entry)
            self.sleep(1)
        self.log("进入固件升级页，准备升级 " + module_labels(selected_modules(self.link, self.firmware_targets)))

    def tap_upgrade(self) -> None:
        """按模块标题定位各自按钮。旧链路没有模块布局时，退回原来的升级按钮。"""
        if not self._on_module_page():
            super().tap_upgrade()
            return
        clicked = []
        for module in selected_modules(self.link, self.firmware_targets):
            action = self._module_action(module)
            if action == "latest":
                raise RuntimeError(f"{module['label']}显示已是最新版本，后台没有可升级的包")
            if action != "upgrade":
                raise RuntimeError(f"升级页没有可点击的{module['label']}升级按钮")
            self.ui.click(
                f"upgrade_{module['id']}",
                {"type": "text_or_description", "value": module.get("action") or "升级至最新版本"},
            )
            clicked.append(module["label"])
            self.log(f"点击{module['label']}升级")
            self.sleep(1)
        if not clicked:
            raise RuntimeError("没有点到任何已选择的固件升级按钮")

    def _module_action(self, module: dict, dump: str | None = None) -> str:
        """用标题和按钮的纵向位置配对，避免两个模块共用同一文案时点错。

        dump 可以由调用方传进来。轮询时页面只抓一次，多个模块共用同一份。
        """
        if dump is None:
            dump = self.ui.dump()
        title = _node_bounds(dump, module["title"])
        if title is None:
            return "missing"
        action = module.get("action") or "升级至最新版本"
        latest = module.get("latest") or "已是最新版本"
        candidates = _nodes_with_label(dump, action) + _nodes_with_label(dump, latest)
        below = [item for item in candidates if item["top"] >= title[1] - 20]
        if not below:
            return "missing"
        chosen = min(below, key=lambda item: item["top"])
        return "upgrade" if chosen["description"] == action and chosen["clickable"] else "latest"

    def _on_module_page(self) -> bool:
        page = self.ui.page_text()
        return any(module["title"] in page for module in selected_modules(self.link, self.firmware_targets))


def _node_bounds(dump: str, value: str) -> tuple[int, int, int, int] | None:
    """标题可能写在 text，也可能写在 content-desc，两种都认。"""
    import re

    for tag in re.findall(r"<node\b[^>]*>", dump or ""):
        if value in (_xml_attr(tag, "text"), _xml_attr(tag, "content-desc")):
            return _bounds(tag)
    return None


def _nodes_with_label(dump: str, label: str) -> list[dict]:
    """按钮文案可能落在 content-desc 或 text 上，两种都算命中。"""
    import re

    found = []
    for tag in re.findall(r"<node\b[^>]*>", dump or ""):
        if label not in (_xml_attr(tag, "content-desc"), _xml_attr(tag, "text")):
            continue
        bounds = _bounds(tag)
        if bounds is None:
            continue
        found.append({
            "description": label,
            "top": bounds[1],
            "clickable": _xml_attr(tag, "clickable") == "true",
        })
    return found


def _bounds(tag: str) -> tuple[int, int, int, int] | None:
    import re

    match = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
    if not match:
        return None
    return tuple(int(item) for item in match.groups())


def _xml_attr(tag: str, name: str) -> str:
    import re

    match = re.search(rf'\b{name}="([^"]*)"', tag)
    return match.group(1) if match else ""
