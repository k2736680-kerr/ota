"""链路、场景、默认参数。页面只读这些，不把修改写回配置。"""

from __future__ import annotations

from server.common.config_loader import list_links, load_cases, load_global, load_link
from server.drivers.phone_ui import case_direction, direction_label, pass_rule
from server.links.firmware_select import firmware_modules, parse_firmware_targets


def catalog() -> dict:
    settings = load_global()
    cases = []
    for item in load_cases():
        direction = case_direction(item)
        strategy = item.get("strategy") or ""
        cases.append({
            "id": item["id"],
            "name": item["name"],
            "column": item.get("column"),
            "strategy": strategy,
            "require_charging": bool(item.get("require_charging")),
            "direction": direction_label(direction),
            "pass_rule": pass_rule(direction, item),
            "default_checked": strategy != "s2",
        })
    links = []
    for item in list_links():
        link = load_link(item["id"])
        modules = firmware_modules(link) if link.get("firmware_modules") else []
        selected = parse_firmware_targets(None, link) if modules else []
        links.append({
            **item,
            "firmware_modules": modules,
            "firmware_targets": selected,
        })
    return {
        "links": links,
        "cases": cases,
        "defaults": {
            "loops": 1,
            "probe_interval": settings["probe_interval"],
            "drop_window": settings["drop_window"],
            "reconnect_timeout": settings["reconnect_timeout"],
            "s2_model": settings["s2_model"],
        },
        "notice": "本工具不修改后台升级配置。",
    }
