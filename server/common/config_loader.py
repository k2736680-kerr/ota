"""读取 config/ 下的 yaml，补默认值并校验必填项。运行期不回写。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from server.common.paths import APP_FILES, CASES_DIR, CONFIG_DIR

CASE_ORDER = ["T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09"]

REQUIRED_LOCATORS = (
    "upgrade_entry",
    "upgrade_button",
    "version_field",
    "announced_field",
)

DEFAULT_GLOBAL = {
    "runtime_root": "runtime",
    "probe_interval": 5,
    "drop_window": 120,
    "reconnect_timeout": 180,
    "hung_confirm_times": 3,
    "upgrade_total_timeout": 600,
    "inject_fallback_delay": 15,
    "min_battery": 20,
    "max_consecutive_skip": 3,
    "case_gap_seconds": 20,
    "case_retry_times": 2,
    "max_consecutive_error": 2,
    "adb_path": "adb",
    "s2_model": "multi",
    "screenshot_on": ["upgrade_start", "inject", "reconnect", "judge"],
    "patterns": {},
}


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"配置不存在: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置必须是映射: {path}")
    return data


def load_global() -> dict:
    data = deepcopy(DEFAULT_GLOBAL)
    data.update(_read_yaml(CONFIG_DIR / "global.yaml"))
    data["probe_interval"] = int(data["probe_interval"])
    data["drop_window"] = int(data["drop_window"])
    data["reconnect_timeout"] = int(data["reconnect_timeout"])
    data["hung_confirm_times"] = int(data["hung_confirm_times"])
    data["upgrade_total_timeout"] = int(data["upgrade_total_timeout"])
    data["inject_fallback_delay"] = int(data["inject_fallback_delay"])
    data["min_battery"] = int(data["min_battery"])
    data["max_consecutive_skip"] = int(data["max_consecutive_skip"])
    data["case_gap_seconds"] = int(data.get("case_gap_seconds") or 0)
    if data["case_gap_seconds"] < 0:
        raise ValueError("case_gap_seconds 不能是负数")
    data["case_retry_times"] = max(1, int(data.get("case_retry_times") or 1))
    data["max_consecutive_error"] = max(1, int(data.get("max_consecutive_error") or 1))
    if data["reconnect_timeout"] < data["drop_window"]:
        raise ValueError("reconnect_timeout 不能小于 drop_window")
    if data["s2_model"] not in ("multi", "single"):
        raise ValueError("s2_model 只能是 multi 或 single")
    return data


def load_link(link_id: str) -> dict:
    path = APP_FILES.get(link_id)
    if path is None:
        raise KeyError(f"未知链路: {link_id}")
    data = _read_yaml(path)
    missing = [key for key in ("name", "pkg", "commands", "locators") if not data.get(key)]
    if missing:
        raise ValueError(f"{path.name} 缺少必填项: {', '.join(missing)}")
    locators = data["locators"]
    absent = [key for key in REQUIRED_LOCATORS if key not in locators]
    if absent:
        raise ValueError(f"{path.name} 缺少定位: {', '.join(absent)}")
    data["id"] = link_id
    data.setdefault("device_keywords", [])
    data.setdefault("ui_connected", ["已连接"])
    data.setdefault("ui_disconnected", ["未连接"])
    data.setdefault("success_texts", ["升级成功", "升级完成"])
    data.setdefault("login_texts", ["登录"])
    data.setdefault("log_export", {"support": False, "steps": [], "device_path": ""})
    data.setdefault("fallback", {})
    data.setdefault("device_name", "")
    data.setdefault("device_mac", "")
    data.setdefault("transport", "bluetooth")
    return data


def load_cases() -> list[dict]:
    found: dict[str, dict] = {}
    for path in sorted(CASES_DIR.glob("*.yaml")):
        data = _read_yaml(path)
        case_id = str(data.get("id") or "").strip()
        if not case_id or not data.get("name"):
            raise ValueError(f"{path.name} 缺少 id 或 name")
        data.setdefault("inject", [])
        data.setdefault("expect", {"reconnect": True, "upgrade_final": "success"})
        data.setdefault("column", data["name"])
        data.setdefault("strategy", None)
        data["file"] = path.name
        found[case_id] = data
    ordered = [found[item] for item in CASE_ORDER if item in found]
    extras = [found[key] for key in found if key not in CASE_ORDER]
    return ordered + extras


def case_by_id(case_id: str) -> dict:
    for case in load_cases():
        if case["id"] == case_id:
            return case
    raise KeyError(f"未知用例: {case_id}")


def list_links() -> list[dict]:
    items = []
    for link_id in ("watch", "ring", "glass"):
        link = load_link(link_id)
        if not link.get("enabled"):
            continue
        items.append({
            "id": link_id,
            "name": link.get("app_name") or link["name"],
            "pkg": link["pkg"],
        })
    return items
