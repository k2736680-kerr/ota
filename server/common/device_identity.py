"""运行时识别当前绑定的穿戴设备。

配置里的 device_name / device_mac 只是可选提示，不能当成唯一真相。
换表、换镜、换戒后不改 yaml 也要能跑：优先读 App 设备页，再从蓝牙 GATT
里按包名找最近连接，最后才用关键字匹配已配对列表。

手表/眼镜设备名常常没有 resourceId，蓝牙 dumpsys 又经常拿不到名字——
这时必须从 App 页面文案里抠。
"""

from __future__ import annotations

import re
import time

_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
_BONDED = re.compile(
    r"((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*\[[^\]]*\]\s*([^\r\n]+)"
)
_CONNECT_WORDS = ("已连接", "未连接", "连接中", "连接失败", "未配对")
_PAGE_NOISE = {
    "已连接",
    "未连接",
    "连接中",
    "连接失败",
    "未配对",
    "固件升级",
    "固件",
    "设置",
    "我的",
    "首页",
    "设备",
    "升级",
    "登录",
    "注册",
    "取消",
    "确定",
    "完成",
    "好的",
    "添加设备",
    "蓝牙",
    "Wi-Fi",
    "WIFI",
    "电量",
    "充电中",
    "正在充电",
    "已充满",
}


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


def identity_from_page(page: str, pkg: str, keywords: list[str] | None = None) -> dict:
    """从 App 界面 dump 抠设备名和 MAC。没有 resourceId 时也能用。

    优先：关键字命中的文案；其次：连接状态附近的标题；再其次：像产品名的短文案。
    不认「已连接 / 固件升级 / 当前版本」这类页面控件词。
    """
    from server.common.version_util import _page_texts

    words = [str(item).strip() for item in (keywords or []) if str(item).strip()]
    raw_texts = [str(item or "").strip() for item in _page_texts(page or "", pkg=pkg or None)]
    raw_texts = [item for item in raw_texts if item]

    mac = ""
    for item in raw_texts:
        found = normalize_mac(item)
        if found:
            mac = found
            break
    # 有的页面把 MAC 嵌在「地址：AA:BB:…」里，整段不是纯 MAC。
    if not mac:
        found = _MAC.search(page or "")
        if found and (not pkg or f'package="{pkg}"' in (page or "")):
            mac = found.group(0).upper()

    names = [_clean_name(item) for item in raw_texts]
    names = [item for item in names if item]

    name = ""
    for item in names:
        if _looks_like_device_name(item, words):
            if words and any(word.lower() in item.lower() for word in words):
                name = item
                break
    if not name:
        connect_at = -1
        for index, item in enumerate(names):
            if item in _CONNECT_WORDS:
                connect_at = index
                break
        window = names[max(0, connect_at - 6) : connect_at] if connect_at > 0 else names[:16]
        for item in window:
            if _looks_like_device_name(item, words):
                name = item
                break
    if not name:
        for item in names:
            if _looks_like_device_name(item, words):
                name = item
                break
    return {"name": name, "mac": mac}


def resolve_identity(link: dict, *, adb=None, read_text=None, page: str | None = None) -> dict:
    """返回 {name, mac, source}。source 便于日志说明识别来源。

    优先顺序：App 控件/页面文案 → 蓝牙 GATT → 配对列表关键字 → 配置提示。
    """
    hint_name = (link.get("device_name") or "").strip()
    hint_mac = normalize_mac(link.get("device_mac") or "")
    keywords = [str(item).strip() for item in (link.get("device_keywords") or []) if str(item).strip()]

    ui_name = ""
    ui_mac = ""
    if read_text is not None:
        locators = link.get("locators") or {}
        ui_name = _clean_name(_safe_read(read_text, locators.get("device_name")))
        ui_mac = normalize_mac(_safe_read(read_text, locators.get("device_mac")))

    if page:
        scraped = identity_from_page(page, link.get("pkg") or "", keywords)
        ui_name = ui_name or scraped.get("name") or ""
        ui_mac = ui_mac or scraped.get("mac") or ""

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
        # GATT 只有 MAC、名字空时，若页面稍后能补上更好；这里先原样返回。
        return {"name": name or bonded.get(mac, ""), "mac": mac, "source": "gatt"}

    matched = _match_bonded(bonded, keywords)
    if matched:
        return {"name": matched[1], "mac": matched[0], "source": "keywords"}

    if hint_mac or hint_name:
        mac = hint_mac or _mac_for_name(bonded, hint_name)
        name = hint_name or bonded.get(mac, "")
        return {"name": name, "mac": mac, "source": "config"}

    return {"name": "", "mac": "", "source": "none"}


def open_device_card(device, adb, link: dict) -> str:
    """打开 App 里能看到设备名的那一页，返回页面 dump。失败返回空串。"""
    pkg = (link or {}).get("pkg") or ""
    if not pkg or device is None:
        return ""
    try:
        from server.drivers.phone_ui import PhoneSession
        from server.drivers.ui_cache import UiCache

        cache = UiCache(device, ttl=0.5)
        session = PhoneSession(device, adb, cache)
        session.prepare_for_app(pkg, allow_relaunch=True)
        _goto_device_card(device, link, cache)
        time.sleep(0.6)
        cache.mark_stale()
        return cache.page() or ""
    except Exception:
        return ""


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


def _safe_read(read_text, locator) -> str:
    if not locator or not _locator_usable(locator):
        return ""
    try:
        return str(read_text(locator) or "")
    except Exception:
        return ""


def _locator_usable(locator: dict | None) -> bool:
    if not isinstance(locator, dict):
        return False
    value = locator.get("value")
    if isinstance(value, dict):
        return bool(
            str(value.get("resourceId") or "").strip()
            or str(value.get("text") or "").strip()
            or str(value.get("textContains") or "").strip()
        )
    return bool(str(value or "").strip())


def _looks_like_device_name(text: str, keywords: list[str] | None = None) -> bool:
    cleaned = _clean_name(text)
    if not cleaned or cleaned in _PAGE_NOISE:
        return False
    if cleaned in _CONNECT_WORDS:
        return False
    if "%" in cleaned or "版本" in cleaned or "升级" in cleaned:
        return False
    if re.search(r"(?i)当前版本|有新版本|已是最新|MOY-|USY-|githash", cleaned):
        return False
    if re.fullmatch(r"\d{1,3}", cleaned):
        return False
    if normalize_mac(cleaned):
        return False
    if len(cleaned) > 48:
        return False
    return True


def _goto_device_card(device, link: dict, cache) -> None:
    """按链路点到设备卡片所在页。眼镜在首页，戒指在「我的」，手表在设备 tab。"""
    pkg = (link or {}).get("pkg") or ""
    locators = (link or {}).get("locators") or {}

    if "glasses" in pkg:
        cancel = (link.get("app_update_cancel") or "取消") if link else "取消"
        try:
            node = device(text=str(cancel))
            if getattr(node, "exists", False):
                node.click()
                time.sleep(0.3)
        except Exception:
            pass
        try:
            home = device(text="首页")
            if getattr(home, "exists", False):
                home.click()
                time.sleep(0.4)
        except Exception:
            pass
        if cache is not None:
            cache.mark_stale()
        return

    tab = locators.get("device_tab") or {}
    value = tab.get("value") if isinstance(tab.get("value"), dict) else {}
    rid = ""
    index = 0
    if isinstance(value, dict):
        rid = str(value.get("resourceId") or "")
        index = int(value.get("index") or 0)
    elif tab.get("type") == "resourceId":
        rid = str(tab.get("value") or "")
    if not rid:
        return
    try:
        node = device(resourceId=rid, instance=index)
        if getattr(node, "exists", False):
            node.click()
            time.sleep(0.5)
            if cache is not None:
                cache.mark_stale()
    except Exception:
        return
