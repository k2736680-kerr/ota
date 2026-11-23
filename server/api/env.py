"""设备探测。刷新时读取手机和当前链路的 App，不打开界面。"""

from __future__ import annotations

import re

from server.common.config_loader import load_global, load_link
from server.drivers.adb import AdbDriver, AdbError

_MARKET_PROPS = (
    "ro.product.marketname",
    "ro.product.vendor.marketname",
    "ro.product.odm.marketname",
    "ro.vendor.oplus.market.name",
    "ro.config.marketing_name",
)


def phone_label(brand: str, market: str, model: str) -> str:
    brand = (brand or "").strip()
    market = (market or "").strip()
    model = (model or "").strip()
    if market and model and market.lower() != model.lower():
        return f"{market} ({model})"
    if market:
        return market
    if brand and model and brand.lower() not in model.lower():
        return f"{brand} {model}"
    return model or brand


def githash_from_version(version: str) -> str:
    match = re.search(r"-g([0-9a-fA-F]{7,})", version or "")
    return match.group(1) if match else ""


def bluetooth_device_name(raw: str, mac: str) -> str:
    """从 dumpsys bluetooth_manager 里按 MAC 取设备名。各家 ROM 行格式不同，多试几种。"""
    if not raw or not mac:
        return ""
    mac_re = re.escape(mac)
    patterns = (
        rf"{mac_re}\s*\[[^\]]*\]\s*([^\r\n]+)",
        rf"{mac_re}\s*:\s*([^:\r\n]+?)\s*:",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, raw, re.I):
            name = _clean_bt_name(match.group(1))
            if name:
                return name
    # name= 必须和这台设备的 MAC 在同一行。整份 dumpsys 里第一个 name= 通常是手机自己。
    for line in raw.splitlines():
        if mac.lower() not in line.lower():
            continue
        found = re.search(r'name\s*[=:]\s*"?([^"\r\n]+)"?', line, re.I)
        if not found:
            continue
        name = _clean_bt_name(found.group(1))
        if name:
            return name
    return ""


def _clean_bt_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip().strip('"'))
    if len(name) < 2:
        return ""
    if name.lower() in {"null", "true", "false", "dual", "active", "none"}:
        return ""
    if re.fullmatch(r"[0-9a-f:]{11,}", name, re.I):
        return ""
    return name


def device_view(dry_run: bool = False, link_id: str = "") -> dict:
    link = _safe_link(link_id)
    if dry_run:
        item = {
            "serial": "DRYRUN",
            "model": "dry-run-phone",
            "android": "14",
            "phone_model": "dry-run-phone",
            "app_version": "1.0.0",
            "device_model": (link or {}).get("device_name") or "",
            "githash": "",
        }
        return {
            "count": 1,
            "ok": True,
            "dry_run": True,
            "devices": [item],
            "message": "dry-run，未读取真机",
        }
    settings = load_global()
    adb = AdbDriver(settings.get("adb_path") or "adb")
    try:
        serials = adb.devices()
    except AdbError as exc:
        return {"count": 0, "ok": False, "dry_run": False, "devices": [], "message": str(exc)}
    devices = [_read_device(adb, link, serial) for serial in serials]
    return {
        "count": len(devices),
        "ok": len(devices) == 1,
        "dry_run": False,
        "devices": devices,
        "message": "可以启动" if len(devices) == 1 else "需要且仅需要一台 device 状态的手机",
    }


def _read_device(adb: AdbDriver, link: dict | None, serial: str) -> dict:
    adb.serial = serial
    brand = adb.getprop("ro.product.brand")
    model = adb.getprop("ro.product.model")
    market = ""
    for prop in _MARKET_PROPS:
        market = adb.getprop(prop)
        if market:
            break
    android = adb.getprop("ro.build.version.release")
    app_version = ""
    device_model = ""
    githash = ""
    if link:
        from server.common.device_identity import resolve_identity

        _installed, app_version = adb.package_version(link["pkg"])
        githash = githash_from_version(app_version)
        identity = resolve_identity(link, adb=adb)
        device_model = identity.get("name") or ""
        mac = identity.get("mac") or link.get("device_mac") or ""
        if not device_model and mac:
            raw = adb.shell("dumpsys bluetooth_manager", check=False)
            device_model = bluetooth_device_name(raw, mac)
    return {
        "serial": serial,
        "model": model,
        "android": android,
        "phone_model": phone_label(brand, market, model),
        "app_version": app_version,
        "device_model": device_model,
        "githash": githash,
    }


def _safe_link(link_id: str) -> dict | None:
    if not link_id:
        return None
    try:
        return load_link(link_id)
    except KeyError:
        return None


def switch_check(dry_run: bool = False) -> dict:
    """真的开关一次蓝牙和 Wi-Fi，确认这台手机可控。只动手机，不动后台。"""
    if dry_run:
        return {"ok": True, "dry_run": True, "checks": [
            {"name": "bluetooth", "ok": True, "detail": "dry-run 假设备"},
            {"name": "wifi", "ok": True, "detail": "dry-run 假设备"},
        ]}
    from server.drivers.adb import AdbDriver
    from server.drivers.phone_ui import PhoneSession
    from server.drivers.uia import UiDriver

    adb = AdbDriver()
    serials = adb.devices()
    if len(serials) != 1:
        return {"ok": False, "status": 400, "message": f"需要且仅需要一台手机，当前 {len(serials)} 台"}
    adb.serial = serials[0]
    device = UiDriver.connect(serials[0]).device
    session = PhoneSession(device, adb)
    checks = []
    for name, reader, setter in (
        ("bluetooth", "bluetooth_on", session.set_bluetooth),
        ("wifi", "wifi_on", session.set_wifi),
    ):
        original = getattr(adb, reader, lambda: None)()
        if original is None:
            checks.append({"name": name, "ok": False, "detail": "读不到当前开关"})
            continue
        off_ok = setter(False)
        off_state = getattr(adb, reader, lambda: None)()
        on_ok = setter(True)
        on_state = getattr(adb, reader, lambda: None)()
        restore_ok = setter(original)
        restored = getattr(adb, reader, lambda: None)()
        ok = bool(off_ok and on_ok and restore_ok and off_state is False and on_state is True and restored is original)
        checks.append({
            "name": name,
            "ok": ok,
            "detail": f"关={off_state} 开={on_state} 已恢复={restored}",
        })
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


def precheck(link_id: str, dry_run: bool = False) -> dict:
    link = load_link(link_id)
    view = device_view(dry_run, link_id)
    return {
        "link": link_id,
        "pkg": link["pkg"],
        "placeholder": str(link["pkg"]).startswith("com.example."),
        "device": view,
        "message": "占位包名，实机任务会被拒绝" if str(link["pkg"]).startswith("com.example.") else view["message"],
    }
