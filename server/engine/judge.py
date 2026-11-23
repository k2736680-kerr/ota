"""死机确认与一轮结果收口。"""

from __future__ import annotations

from server.engine import ABORT_ENV, FAIL_UPGRADE, HUNG_DEVICE, PASS, SKIP


class HungTracker:
    """从首次断连开始计时。观察窗内回来算正常；超时后再连续失败才确认死机。"""

    def __init__(self, drop_window: int, reconnect_timeout: int, confirm_times: int, interval: int) -> None:
        self.drop_window = drop_window
        self.reconnect_timeout = reconnect_timeout
        self.confirm_times = confirm_times
        self.interval = max(1, interval)
        self.t0: float | None = None
        self.fail_streak = 0

    def reset(self) -> None:
        self.t0 = None
        self.fail_streak = 0

    def see(self, connected: bool, now: float) -> str:
        """返回 connected / waiting / confirming / hung。"""
        if connected:
            self.t0 = None
            self.fail_streak = 0
            return "connected"
        if self.t0 is None:
            self.t0 = now
        elapsed = now - self.t0
        if elapsed < self.reconnect_timeout:
            self.fail_streak = 0
            return "waiting"
        self.fail_streak += 1
        if self.fail_streak >= self.confirm_times:
            return "hung"
        return "confirming"

    @property
    def in_drop_window(self) -> bool:
        return False


def finish_round(
    *,
    saw_disconnect: bool,
    reconnected: bool,
    version_ok: bool,
    env_ok: bool,
    env_reason: str,
    upgrade_seen: bool,
    injected: bool,
    inject_failed: bool,
) -> tuple[str, str]:
    if not env_ok:
        return ABORT_ENV, env_reason or "环境异常"
    if inject_failed:
        return SKIP, "注入命令失败"
    if not upgrade_seen and not injected:
        return FAIL_UPGRADE, "未观察到升级完成"
    if saw_disconnect and not reconnected:
        return HUNG_DEVICE, "断开后超时未重连"
    if not version_ok:
        return FAIL_UPGRADE, "版本校验未通过"
    if not upgrade_seen:
        return FAIL_UPGRADE, "未观察到升级成功"
    return PASS, "升级成功且重连成功" if saw_disconnect else "升级成功（本轮未断连）"


def matrix_cell(codes: list[str]) -> str:
    """一列多轮：有死机记 HUNG，有升级失败记 FAIL，全过才 PASS，其余 SKIP。"""
    if any(code == HUNG_DEVICE for code in codes):
        return "HUNG"
    if any(code == FAIL_UPGRADE for code in codes):
        return "FAIL"
    if codes and all(code == PASS for code in codes):
        return "PASS"
    if any(code == SKIP for code in codes):
        return "SKIP"
    if any(code == ABORT_ENV for code in codes):
        return "SKIP"
    return ""
