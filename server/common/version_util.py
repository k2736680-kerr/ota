"""版本号按位比较；以及从页面 dump 里用稳定文案抠版本。"""

from __future__ import annotations

import re

# 穿戴固件常见形态：MOY-BZH2-2.0.3 / USY-I403-1.1.9；也兼容纯 2.0.3。
_FIRMWARE_TOKEN = re.compile(
    r"(?:MOY|USY)-[A-Za-z0-9]+-\d+(?:\.\d+)+|[A-Za-z]{2,}[A-Za-z0-9]*-[A-Za-z0-9]+-\d+(?:\.\d+)+|\d+(?:\.\d+)+"
)
_RING_CURRENT = re.compile(r"当前版本[:：]\s*([A-Za-z0-9][A-Za-z0-9.\-]*)")


def parse_version(value: str) -> tuple:
    parts = []
    for raw in str(value or "").strip().split("."):
        token = raw.strip()
        if token.isdigit():
            parts.append(int(token))
        else:
            parts.append(token)
    return tuple(parts)


def compare(left: str, right: str) -> int:
    a = parse_version(left)
    b = parse_version(right)
    size = max(len(a), len(b))
    for index in range(size):
        av = a[index] if index < len(a) else 0
        bv = b[index] if index < len(b) else 0
        if type(av) is not type(bv):
            av, bv = str(av), str(bv)
        if av < bv:
            return -1
        if av > bv:
            return 1
    return 0


def higher(left: str, right: str) -> bool:
    return compare(left, right) > 0


def firmware_token(text: str) -> str:
    """从一段控件文案里取出完整固件号；没有就空串。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    ring = _RING_CURRENT.search(raw)
    if ring:
        return ring.group(1).rstrip(".")
    found = _FIRMWARE_TOKEN.search(raw)
    return found.group(0).rstrip(".") if found else ""


def ring_version_from_page(page: str, pkg: str | None = None) -> str:
    """戒指升级页：固定看「当前版本:…」文案，不绑 resourceId。"""
    for text in _page_texts(page, pkg=pkg):
        token = firmware_token(text)
        if token and "当前版本" in text:
            return token
    found = _RING_CURRENT.search(page or "")
    return found.group(1).rstrip(".") if found else ""


def watch_versions_from_page(
    page: str,
    *,
    pkg: str | None = None,
    offer_texts: list[str] | None = None,
    latest_texts: list[str] | None = None,
) -> dict:
    """手表升级页：用场景文案区分实报/目标，版本号本身就是节点文字。

    - 「您的固件是最新版本」→ 页面上的固件号是实报
    - 「有新版本可以更新」→ 页面上的固件号是后台目标
    只认 App 自己的节点，躲开状态栏电量「100.0」。
    """
    body = page or ""
    tokens = _firmware_tokens(body, pkg=pkg)
    offer_words = offer_texts or ["有新版本可以更新", "发现新版本", "新版本"]
    latest_words = latest_texts or ["您的固件是最新版本", "已是最新版本"]
    is_offer = any(word in body for word in offer_words)
    is_latest = any(word in body for word in latest_words)
    first = tokens[0] if tokens else ""
    if is_latest:
        return {"current": first, "target": ""}
    if is_offer:
        return {"current": "", "target": first}
    if len(tokens) >= 2:
        return {"current": tokens[0], "target": tokens[1]}
    return {"current": first, "target": ""}


def watch_version_display(token: str) -> str:
    """手表历史备注里常用尾号（2.0.3）；完整号也保留可解析。"""
    raw = str(token or "").strip()
    if not raw:
        return ""
    tail = re.search(r"\d+(?:\.\d+)+$", raw)
    return tail.group(0) if tail else raw


def _firmware_tokens(page: str, pkg: str | None = None) -> list[str]:
    preferred: list[str] = []
    plain: list[str] = []
    for text in _page_texts(page, pkg=pkg):
        if any(word in text for word in ("最新版本", "新版本", "固件升级", "升级成功", "升级失败", "当前版本")):
            if "当前版本" not in text and not _FIRMWARE_TOKEN.search(text):
                continue
        token = firmware_token(text)
        if not token or _noise_version(token):
            continue
        bucket = preferred if token.startswith(("MOY-", "USY-")) else plain
        if token not in bucket:
            bucket.append(token)
    return preferred or plain


def _noise_version(token: str) -> bool:
    """状态栏电量、单纯整数、进度占位 0.0，不能当固件号。"""
    return bool(
        re.fullmatch(r"100(?:\.0+)?", token)
        or re.fullmatch(r"\d{1,3}", token)
        or re.fullmatch(r"0+(?:\.0+)+", token)
    )


def _page_texts(page: str, pkg: str | None = None) -> list[str]:
    if not page:
        return []
    if "<node" not in page:
        return [page]
    texts = []
    for tag in re.findall(r"<node\b[^>]*>", page):
        package = _xml_attr(tag, "package")
        if package.startswith("com.android."):
            continue
        if pkg and package and package != pkg:
            continue
        text = _xml_attr(tag, "text")
        desc = _xml_attr(tag, "content-desc")
        if text:
            texts.append(text)
        if desc:
            texts.append(desc)
    return texts


def _xml_attr(tag: str, name: str) -> str:
    found = re.search(rf'\b{name}="([^"]*)"', tag)
    return found.group(1) if found else ""
