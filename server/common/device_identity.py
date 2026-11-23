"""运行时识别当前绑定的穿戴设备。

配置里的 device_name / device_mac 只是可选提示，不能当成唯一真相。
换表、换镜、换戒后不改 yaml 也要能跑：优先读 App 页面，再从蓝牙 GATT
里按包名找最近连接，最后才用关键字匹配已配对列表。
"""

from __future__ import annotations

import re

_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
_BONDED = re.compile(
    r"((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*\[[^\]]*\]\s*([^\r\n]+)"
)


def looks_like_phone(name: str, phone_model: str) -> bool:
    """设备型号栏里填的是手机名，不是穿戴设备。

    刷新设备信息时，蓝牙 dumpsys 里第一个 name= 经常是手机自己。
    页面会把这个名字预填进「设备型号」，开始任务时再盖掉真正识别到的戒指/手表。
    """
    device = (name or "").strip()
    phone = (phone_model or "").strip()
    if not device or not phone:
        return False
    if device == phone:
        return True
    head = phone.split("(")[0].strip()
    return bool(head) and (device == head or phone.startswith(device))


def normalize_mac(value: str) -> str:
    text = (value or "").strip().upper()
    match = _MAC.search(text)
    return match.group(0).upper() if match else ""


def bonded_devices(raw: str) -> dict[str, str]:
    """MAC -> 蓝牙广播名。"""
    found: dict[str, str] = {}
    for match in _BONDED.finditer(raw or ""):
        mac = match.group(1).upper()
        name = _clean_name(match.group(2))
        if mac and name:
            found[mac] = name
    return found


def gatt_macs_for_package(raw: str, pkg: str) -> list[str]:
    """按 App 包名取出 GATT Connections 里出现过的 MAC，最近的在前。

    同一个包名会在 dumpsys 里出现多次（Scanner Map / Client Map 等）。
    只认带 `Connections:` 的那段，避免扫到只有扫描统计的空段。
    """
    if not raw or not pkg:
        return []
    macs: list[str] = []
    seen: set[str] = set()
    for block in _package_blocks(raw, pkg):
        section = re.search(r"Connections:\s*\d+\s*\n((?:\s+[^\n]+\n)*)", block)
        if not section:
            continue
        for match in _MAC.finditer(section.group(1)):
            mac = match.group(0).upper()
            if mac in seen:
                continue
            seen.add(mac)
            macs.append(mac)
    return macs


def resolve_identity(link: dict, *, adb=None, read_text=None) -> dict:
    """返回 {name, mac, source}。source 便于日志说明识别来源。"""
    hint_name = (link.get("device_name") or "").strip()
    hint_mac = normalize_mac(link.get("device_mac") or "")
    keywords = [str(item).strip() for item in (link.get("device_keywords") or []) if str(item).strip()]

    ui_name = ""
    ui_mac = ""
    if read_text is not None:
        locators = link.get("locators") or {}
        ui_name = _clean_name(read_text(locators.get("device_name")))
        ui_mac = normalize_mac(read_text(locators.get("device_mac")))

    raw = ""
    if adb is not None:
        try:
            raw = adb.shell("dumpsys bluetooth_manager", check=False) or ""
        except Exception:
            raw = ""
    bonded = bonded_devices(raw)
    gatt = gatt_macs_for_package(raw, link.get("pkg") or "")

    if ui_mac or ui_name:
        mac = ui_mac or _mac_for_name(bonded, ui_name) or (gatt[0] if gatt else "") or hint_mac
        name = ui_name or bonded.get(mac, "") or hint_name
        return {"name": name, "mac": mac, "source": "ui"}

    if gatt:
        mac = gatt[0]
        name = bonded.get(mac, "") or _name_by_keywords(bonded, keywords) or hint_name
        # 多个 GATT 记录时，关键字能收窄就收窄。
        if keywords and len(gatt) > 1:
            for candidate in gatt:
                label = bonded.get(candidate, "")
                if label and any(word.lower() in label.lower() for word in keywords):
                    mac = candidate
                    name = label
                    break
        return {"name": name or bonded.get(mac, ""), "mac": mac, "source": "gatt"}

    matched = _match_bonded(bonded, keywords)
    if matched:
        return {"name": matched[1], "mac": matched[0], "source": "keywords"}

    if hint_mac or hint_name:
        mac = hint_mac or _mac_for_name(bonded, hint_name)
        name = hint_name or bonded.get(mac, "")
        return {"name": name, "mac": mac, "source": "config"}

    return {"name": "", "mac": "", "source": "none"}


def apply_identity(link: dict, identity: dict) -> dict:
    """把识别结果写回本轮使用的 link 副本，供探针和报告使用。"""
    name = (identity or {}).get("name") or ""
    mac = normalize_mac((identity or {}).get("mac") or "")
    if name:
        link["device_name"] = name
    if mac:
        link["device_mac"] = mac
    return link


def _package_blocks(raw: str, pkg: str) -> list[str]:
    """取出 dumpsys 里该包名的所有段落（可能有 Scanner/Client 多段）。"""
    if not raw or not pkg:
        return []
    header = re.compile(rf"(?m)^[ \t]*{re.escape(pkg)}(?:[ \t]+\(Registered\))?[ \t]*$")
    starts = [match.start() for match in header.finditer(raw)]
    if not starts:
        alt = re.search(rf"(?m)^[ \t]*{re.escape(pkg)}\b.*$", raw)
        if not alt:
            return []
        starts = [alt.start()]
    blocks: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(raw)
        chunk = raw[start:end]
        # 仍可能截到下一个其它包名。
        first_nl = chunk.find("\n")
        after = chunk[first_nl + 1 :] if first_nl >= 0 else ""
        nxt = re.search(r"(?m)^[ \t]*com\.[a-zA-Z0-9_.]+(?:[ \t]+\(Registered\))?[ \t]*$", after)
        if nxt is not None:
            chunk = chunk[: first_nl + 1 + nxt.start()]
        blocks.append(chunk)
    return blocks


def _package_block(raw: str, pkg: str) -> str:
    """兼容旧调用：优先返回带 Connections 的段落。"""
    blocks = _package_blocks(raw, pkg)
    for block in blocks:
        if "Connections:" in block:
            return block
    return blocks[0] if blocks else ""


def _match_bonded(bonded: dict[str, str], keywords: list[str]) -> tuple[str, str] | None:
    if not bonded:
        return None
    if keywords:
        hits = [
            (mac, name)
            for mac, name in bonded.items()
            if any(word.lower() in name.lower() for word in keywords)
        ]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            # 多个命中时不瞎猜，交给上层用 UI。
            return hits[0]
    return None


def _name_by_keywords(bonded: dict[str, str], keywords: list[str]) -> str:
    matched = _match_bonded(bonded, keywords)
    return matched[1] if matched else ""


def _mac_for_name(bonded: dict[str, str], name: str) -> str:
    target = _clean_name(name).lower()
    if not target:
        return ""
    for mac, label in bonded.items():
        if label.lower() == target or target in label.lower() or label.lower() in target:
            return mac
    return ""


def _clean_name(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip().strip('"'))
    if len(text) < 2:
        return ""
    if text.lower() in {"null", "true", "false", "dual", "active", "none"}:
        return ""
    if _MAC.fullmatch(text):
        return ""
    return text
