"""一次观察内的界面快照。

uiautomator2 的每次 exists 都是一次 JSON-RPC 调用，app_current 还要额外起一个
adb 进程（实测 450ms 左右）。原来的判定函数每轮要读十几次前台包名、好几十次
控件存在性，一次探测就能花掉 40 秒，升级本身才十分钟。

这里把同一时刻的页面 dump 和前台包名缓存起来：一次观察只真正读一次页面、
一次前台包名，后续判断全部走缓存。数据过期就显式清掉，不会拿旧页面下结论。
"""

from __future__ import annotations

import time


class UiCache:
    """页面 dump 和前台包名的短时缓存。真机与假设备都走这一层。"""

    def __init__(self, device, *, ttl: float = 2.0) -> None:
        self.device = device
        self.ttl = max(0.0, float(ttl))
        self._page_at = 0.0
        self._page = ""
        self._page_valid = False
        self._current_at = 0.0
        self._current: dict | None = None
        self._current_valid = False

    # ---- 页面 ----

    def page(self) -> str:
        if self._page_valid and time.monotonic() - self._page_at <= self.ttl:
            return self._page
        self.mark_stale()
        try:
            self._page = self.device.dump_hierarchy()
        except Exception:
            self._page = ""
        self._page_at = time.monotonic()
        self._page_valid = True
        return self._page

    def cached_page(self) -> str | None:
        """只读缓存，不触发新的 dump。过期了就当没有，绝不用旧页面下结论。"""
        if self._page_valid and time.monotonic() - self._page_at <= self.ttl:
            return self._page
        return None

    # ---- 前台包名 ----

    def current(self) -> dict:
        if self._current_valid and time.monotonic() - self._current_at <= self.ttl:
            return self._current or {}
        reader = getattr(self.device, "app_current", None)
        info: dict = {}
        if callable(reader):
            try:
                info = reader() or {}
            except Exception:
                info = {}
        self._current = dict(info) if isinstance(info, dict) else {}
        self._current_at = time.monotonic()
        self._current_valid = True
        return self._current

    def package(self) -> str:
        return str(self.current().get("package") or "")

    def activity(self) -> str:
        return str(self.current().get("activity") or "")

    # ---- 失效 ----

    def mark_stale(self) -> None:
        """界面将要变化时调用：点完按钮、按了返回键、切了前后台。"""
        self._page_valid = False
        self._current_valid = False
