"""uiautomator2 封装。定位只用文字、描述或 resourceId，不使用坐标。"""

from __future__ import annotations

import re
from pathlib import Path


class UiError(RuntimeError):
    pass


class UiDriver:
    """device 由外部注入。dry-run 与单测传入假设备，实机才连接 uiautomator2。"""

    def __init__(self, device, fallback: dict | None = None) -> None:
        self.device = device
        self.fallback = fallback or {}

    @classmethod
    def connect(cls, serial: str, fallback: dict | None = None) -> "UiDriver":
        try:
            import uiautomator2 as u2
        except ImportError as exc:
            raise UiError("未安装 uiautomator2") from exc
        try:
            device = u2.connect(serial) if serial else u2.connect()
        except Exception as exc:
            raise UiError(f"连接 uiautomator2 失败: {exc}") from exc
        return cls(device, fallback)

    def _selector(self, locator: dict):
        kind = (locator or {}).get("type")
        value = (locator or {}).get("value")
        if kind == "text":
            return self.device(text=value)
        if kind == "textContains":
            return self.device(textContains=value)
        if kind == "resourceId":
            return self.device(resourceId=value)
        if kind == "regex":
            return self.device(textMatches=value)
        if kind == "description":
            return self.device(description=value)
        if kind == "text_or_description":
            return self._text_or_description(value)
        if kind == "text_then_id":
            return self._text_then_id(value)
        if kind == "index":
            resource_id = (value or {}).get("resourceId") if isinstance(value, dict) else ""
            index = int((value or {}).get("index", 0)) if isinstance(value, dict) else 0
            return self.device(resourceId=resource_id, instance=index)
        raise UiError(f"不支持的定位类型: {kind}")

    def _text_then_id(self, value: dict):
        text = (value or {}).get("text") or ""
        contains = (value or {}).get("contains") or ""
        resource_id = (value or {}).get("resourceId") or ""
        if text and self.device(text=text).exists:
            return self.device(text=text)
        if contains and self.device(textContains=contains).exists:
            return self.device(textContains=contains)
        if resource_id and self.device(resourceId=resource_id).exists:
            return self.device(resourceId=resource_id)
        if text:
            return self.device(text=text)
        if contains:
            return self.device(textContains=contains)
        return self.device(resourceId=resource_id)

    def _text_or_description(self, value: str):
        """按钮文案可能挂在 text，也可能挂在 content-desc。先按 text 找，再按描述找。"""
        by_text = self.device(text=value)
        if _truthy_exists(by_text):
            return by_text
        return self.device(description=value)

    def exists(self, locator: dict, timeout: float = 0) -> bool:
        if not locator:
            return False
        try:
            node = self._selector(locator)
            if timeout:
                return bool(node.wait(timeout=timeout))
            return bool(node.exists)
        except UiError:
            raise
        except Exception:
            return False

    def click(self, name: str, locator: dict) -> str:
        try:
            node = self._selector(locator)
            if node.exists:
                node.click()
                return "locator"
        except UiError:
            raise
        except Exception as exc:
            raise UiError(f"点击 {name} 失败: {exc}") from exc
        raise UiError(f"找不到文字或资源: {name}")

    def read_text(self, locator: dict) -> str:
        if not locator:
            return ""
        try:
            node = self._selector(locator)
            if not node.exists:
                return ""
            info = node.info or {}
            return str(info.get("text") or "")
        except Exception:
            return ""

    def dump(self) -> str:
        try:
            return self.device.dump_hierarchy()
        except Exception as exc:
            return f"<!-- dump failed: {exc} -->"

    def save_dump(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dump(), encoding="utf-8")
        return path

    def page_text(self) -> str:
        return self.dump()

    def progress_percent(self, locator: dict) -> int | None:
        dump = self.dump()
        if "<node" in (dump or ""):
            # 状态栏电量也写着百分号。只认 App 自己的进度，避免把电量 100% 当成升级进度。
            found = _app_percents(dump)
            return found[-1] if found else None
        found = re.findall(r"(\d+)\s*%", dump or "")
        if not found:
            return None
        return int(found[-1])

    def contains_any(self, words: list[str]) -> str:
        text = self.page_text()
        for word in words:
            if word and word in text:
                return word
        return ""


def _truthy_exists(node) -> bool:
    try:
        return bool(getattr(node, "exists", False))
    except Exception:
        return False


def _app_percents(dump: str) -> list[int]:
    found: list[int] = []
    for tag in re.findall(r"<node\b[^>]*>", dump or ""):
        if _xml_attr(tag, "package") == "com.android.systemui":
            continue
        for value in (_xml_attr(tag, "text"), _xml_attr(tag, "content-desc")):
            for item in re.findall(r"(\d+)\s*%", value or ""):
                found.append(int(item))
    return found


def _xml_attr(tag: str, name: str) -> str:
    match = re.search(rf'\b{name}="([^"]*)"', tag)
    return match.group(1) if match else ""
