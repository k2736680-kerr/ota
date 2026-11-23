"""判断升级是不是「还在动」。

升级本身要多久没法预先规定：眼镜主控走 BLE，实测一轮 550~660 秒，换台机器、
换包大小都会变。所以不能用「总共不能超过 N 秒」来判失败，只能用「有没有进展」。

三路进展信号，任一路在变就说明还在动：
- 页面百分比变了（手表这类会显示进度的链路）。
- 页面文案或界面本身变了。
- App 还在写固件数据（眼镜升级时 App 的 X-LOG 一直在刷 BLE 分包回执）。

三路都长时间没有变化，才算卡住。

第三条要特别小心。2026-11-10 全量任务里 T08 被判超时，事后翻 logcat 发现那一刻
升级其实走到 77% 还在写数据，是旧代码拿总时长硬切造成的误判。所以这里的判据必须
真的能反映「在传数据」，不能只看日志条数增没增加——滚动窗口里条数会来回抖，
一抖就当成没进展，等于换了个方式误判。
"""

from __future__ import annotations

import re
import subprocess
import time

from server.drivers.adb import hidden_subprocess_kwargs


class ProgressWatch:
    """记录最后一段时间是否有进展。只看「多久没动」，不看总共花了多久。"""

    def __init__(self, stall_seconds: float, logcat_tag: str = "X-LOG:V", serial: str = "",
                 adb_path: str = "adb") -> None:
        self.stall_seconds = max(30.0, float(stall_seconds))
        self.logcat_tag = logcat_tag
        self.serial = serial
        self.adb_path = adb_path
        self.last_change = time.time()
        self.last_percent: int | None = None
        self.last_page = ""
        self.last_max_offset = -1
        self.last_progress: float | None = None
        self.last_log_stamp = ""

    def see(self, *, percent: int | None = None, page: str = "") -> bool:
        """报告一次观察结果。返回这次观察是否带来了新进展。"""
        changed = False
        if percent is not None and percent != self.last_percent:
            self.last_percent = percent
            changed = True
        if page and page != self.last_page:
            self.last_page = page
            changed = True
        if self._transfer_advanced():
            changed = True
        if changed:
            self.last_change = time.time()
        return changed

    def note_progress(self) -> None:
        """外部做了一件明确算进展的事（比如刚执行完干扰），重置静默计时。"""
        self.last_change = time.time()

    def stalled(self) -> bool:
        return time.time() - self.last_change >= self.stall_seconds

    def idle_seconds(self) -> float:
        return time.time() - self.last_change

    # ---- 第三路：固件数据还在不在传 ----

    def _transfer_advanced(self) -> bool:
        """看固件传输有没有往前走。

        用两类单调指标，避开滚动窗口条数抖动的问题：
        - `ota progress ... progress=77.3` 里的百分比数值，只在变大时算进展。
        - `offset : 1054240(0x101620)` 这类写入偏移，只在变大时算进展。
        两者都读不到时，才退回到「有没有新的传输日志行」这种弱判断。

        水位线只升不降。眼镜重传分片时 progress 会往回跳（实测见过 77.34→76.93），
        如果跟着往下记，等它再爬回 77.19 就会被当成新进展，卡住的传输也能一直续命。
        """
        text = self._read_logcat()
        if text is None:
            return False

        advanced = False

        progress = None
        for value in re.findall(r"ota progress[^\n]*?progress=([\d.]+)", text, re.I):
            try:
                progress = max(progress or 0.0, float(value))
            except ValueError:
                continue
        if progress is not None and (self.last_progress is None or progress > self.last_progress):
            advanced = True
            self.last_progress = progress

        offset = None
        for value in re.findall(r"offset\s*:\s*(\d+)", text):
            try:
                offset = max(offset or 0, int(value))
            except ValueError:
                continue
        if offset is not None and (self.last_max_offset < 0 or offset > self.last_max_offset):
            advanced = True
            self.last_max_offset = offset

        if progress is not None or offset is not None:
            return advanced

        # 读不到数值指标时的弱判断：出现了新的传输日志时间戳。
        stamps = re.findall(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d+)", text, re.M)
        if stamps:
            newest = stamps[-1]
            if newest != self.last_log_stamp:
                self.last_log_stamp = newest
                return True
        return False

    def _read_logcat(self) -> str | None:
        """只读指定 tag 的日志。没配 tag 就返回 None，表示这一路判据不可用。

        不能在没有 tag 时退化成读全系统日志——那样任何进程打个日志都会让时间戳变，
        于是「永远有进展」，卡住检测等于失效。手表链路就是没配 tag 的情况，
        它靠页面百分比判进展就够了，第三路直接关掉。
        """
        if not self.logcat_tag:
            return None
        command = [self.adb_path]
        if self.serial:
            command.extend(["-s", self.serial])
        command.extend(["logcat", "-d", "-t", "600", "-s", self.logcat_tag])
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                **hidden_subprocess_kwargs(),
            )
        except Exception:
            return None
        return (result.stdout or "") + (result.stderr or "")

