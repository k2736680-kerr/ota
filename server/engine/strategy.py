"""S1 / S2 自检。只读版本，不改后台。"""

from __future__ import annotations

from server.common.version_util import compare, higher
from server.engine import STALE_POLICY


def check_before(strategy: str | None, reported: str, announced: str, entry_ok: bool, same_version: bool = False) -> tuple[bool, str, str]:
    """返回 (通过, 结果码或空, 原因)。无策略的基线场景只要求有升级入口。"""
    if not entry_ok:
        return False, STALE_POLICY, "没有升级入口，请检查后台配置与包是否可下载"
    # 同版本循环的页面只会一直显示目标版本，读不到实报版本也允许开跑。
    if same_version and strategy == "s1":
        return True, "", ""
    if strategy in ("s1", "s2"):
        if not reported or not announced:
            return False, STALE_POLICY, "读不到设备实报版本或后台公布版本"
        if not higher(announced, reported):
            return False, STALE_POLICY, f"公布版本 {announced} 不高于实报版本 {reported}"
    return True, "", ""


def check_after_s1(before: str, after: str) -> tuple[bool, str]:
    if not before or not after:
        return False, "S1 缺少升级前后版本，无法确认前提"
    if compare(before, after) != 0:
        return False, f"S1 前提破坏：升级后实报版本从 {before} 变为 {after}"
    return True, ""


def check_s2_direction(previous: str | None, current: str) -> tuple[bool, str]:
    """current 形如 '2.0.0->2.0.1'。方向连续相同则不通过。"""
    if not previous or not current or "->" not in current or "->" not in previous:
        return True, ""
    prev_pair = previous.split("->")
    curr_pair = current.split("->")
    if prev_pair == curr_pair:
        return False, f"相邻两轮方向相同: {current}"
    # 反向：上一轮的终点应是这一轮的起点，且方向对调。
    reversed_ok = prev_pair[0] == curr_pair[1] and prev_pair[1] == curr_pair[0]
    if not reversed_ok and prev_pair[1] != curr_pair[0]:
        return False, f"相邻两轮未交替: {previous} 然后 {current}"
    return True, ""
