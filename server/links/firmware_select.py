"""按链路的固件模块选择。

只有配了 firmware_modules 的链路（目前是眼镜，主控 / 影音）才有模块可选。
没配的链路（DaFit、戒指）就是没有模块，不要求选，也不在日志里写固件名。
这里只决定要点哪些，不负责点击。
"""

from __future__ import annotations


def firmware_modules(link: dict | None) -> list[dict]:
    """这条链路配置的固件模块。没配就是没有模块，不回退到别的链路。

    以前没配时会回退到一组眼镜的默认模块（主控/影音），结果 DaFit 这类
    根本没有固件模块的链路也显示出一个固件选择区，还会在日志里写「本次固件 主控」。
    """
    raw = (link or {}).get("firmware_modules") or {}
    if isinstance(raw, dict) and raw:
        items = []
        for key, value in raw.items():
            body = value if isinstance(value, dict) else {}
            items.append({
                "id": str(key),
                "label": str(body.get("label") or key),
                "title": str(body.get("title") or body.get("label") or key),
                "action": str(body.get("action") or "升级至最新版本"),
                "latest": str(body.get("latest") or "已是最新版本"),
            })
        return items
    return []


def parse_firmware_targets(raw, link: dict | None = None) -> list[str]:
    """把任务里的勾选收成模块 id。空表示用链路默认。"""
    modules = firmware_modules(link)
    known = {item["id"]: item["id"] for item in modules}
    for item in modules:
        known[item["label"]] = item["id"]
    if raw in (None, ""):
        return _defaults(link, modules)
    if isinstance(raw, str):
        pieces = [part.strip() for part in raw.replace("，", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        pieces = [str(part).strip() for part in raw]
    else:
        raise ValueError("固件选择格式不正确")
    picked = []
    for piece in pieces:
        if not piece:
            continue
        if piece in {"all", "全部", "*"}:
            return [item["id"] for item in modules]
        module_id = known.get(piece)
        if module_id is None:
            names = "、".join(item["label"] for item in modules)
            raise ValueError(f"未知固件模块: {piece}。可选 {names}，或全部")
        if module_id not in picked:
            picked.append(module_id)
    if not picked:
        raise ValueError("至少选择一个固件模块")
    return picked


def _defaults(link: dict | None, modules: list[dict]) -> list[str]:
    # 这条链路没有配 firmware_modules，说明它根本没有固件模块可选（DaFit 就是）。
    # 不能拿 DEFAULT_MODULES 顶上——那是眼镜的主控/影音，会导致手表任务显示「本次固件 主控」。
    if not (link or {}).get("firmware_modules"):
        return []
    configured = (link or {}).get("firmware_targets")
    if configured:
        return parse_firmware_targets(configured, {"firmware_modules": {
            item["id"]: {
                "label": item["label"],
                "title": item["title"],
                "action": item.get("action") or "",
                "latest": item.get("latest") or "",
            } for item in modules
        }})
    return [modules[0]["id"]] if modules else []


def selected_modules(link: dict | None, targets: list[str] | None = None) -> list[dict]:
    modules = firmware_modules(link)
    wanted = targets if targets is not None else _defaults(link, modules)
    by_id = {item["id"]: item for item in modules}
    return [by_id[item] for item in wanted if item in by_id]


def module_labels(modules: list[dict]) -> str:
    return "、".join(item["label"] for item in modules) or "未选择"
