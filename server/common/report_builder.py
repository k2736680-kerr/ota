"""按任务目录写出逐轮明细、矩阵 CSV、HTML 和 result.json。"""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from server.engine.judge import matrix_cell

MATRIX_COLUMNS = [
    ("T01", "基线升级"),
    ("T02", "反复升级"),
    ("T04", "升级中杀APP"),
    ("T05", "升级中关蓝牙"),
    ("T06", "升级中关WiFi"),
    ("T09", "充电中升级"),
    ("T07", "APP放后台"),
    ("T08", "APP息屏"),
]


def write_task_report(directory: Path, task: dict) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    rounds = task.get("rounds") or []
    matrix = _matrix(task, rounds)
    stats = _stats(rounds)
    payload = {
        "task": {key: task.get(key) for key in (
            "task_id", "slug", "link", "link_name", "strategy", "phone_model",
            "android", "device_model", "githash", "app_version", "serial",
            "manual_fields", "status", "verdict",
        )},
        "rounds": rounds,
        "matrix": matrix,
        "stats": stats,
    }
    (directory / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(directory / "report.csv", matrix, rounds)
    (directory / "report.html").write_text(_html(payload), encoding="utf-8")
    return payload


def _matrix(task: dict, rounds: list[dict]) -> dict:
    grouped: dict[str, list[str]] = {}
    for item in rounds:
        grouped.setdefault(item["case_id"], []).append(item["code"])
    # T03 与 T02 都落在「反复升级」列；同时勾选时死机优先。
    repeat_codes = grouped.get("T02", []) + grouped.get("T03", [])
    cells = {}
    for case_id, _title in MATRIX_COLUMNS:
        codes = repeat_codes if case_id == "T02" else grouped.get(case_id, [])
        cells[case_id] = matrix_cell(codes) if codes else ""
    return {
        "手机型号": task.get("phone_model") or "",
        "系统版本": task.get("android") or "",
        "设备型号": task.get("device_model") or "",
        "固件githash": task.get("githash") or "",
        "APP版本": task.get("app_version") or "",
        "cells": cells,
    }


def _stats(rounds: list[dict]) -> dict:
    total = len(rounds)
    passed = sum(1 for item in rounds if item["code"] == "PASS")
    failed = sum(1 for item in rounds if item["code"] == "FAIL_UPGRADE")
    hung = sum(1 for item in rounds if item["code"] == "HUNG_DEVICE")
    elapsed = [item.get("elapsed") or 0 for item in rounds]
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "hung": hung,
        "success_rate": round(passed / total, 4) if total else 0,
        "avg_elapsed": round(sum(elapsed) / total, 2) if total else 0,
    }


def _write_csv(path: Path, matrix: dict, rounds: list[dict]) -> None:
    headers = ["手机型号", "系统版本", "设备型号", "固件githash", "APP版本"]
    headers += [title for _case_id, title in MATRIX_COLUMNS]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        row = [matrix["手机型号"], matrix["系统版本"], matrix["设备型号"], matrix["固件githash"], matrix["APP版本"]]
        row += [matrix["cells"].get(case_id, "") for case_id, _title in MATRIX_COLUMNS]
        writer.writerow(row)
        writer.writerow([])
        writer.writerow(["轮次", "场景", "触发方式", "升级前", "升级后", "公布版本", "耗时", "结果码", "尝试", "备注"])
        for index, item in enumerate(rounds, start=1):
            writer.writerow([
                index,
                item.get("case_id"),
                item.get("trigger") or "",
                item.get("before_version") or "",
                item.get("after_version") or "",
                item.get("announced") or "",
                item.get("elapsed") or "",
                item.get("code") or "",
                _attempts_label(item),
                item.get("note") or "",
            ])


def _attempts_label(item: dict) -> str:
    """把重试情况写成一列。第一次就过就留空，重试过就写清楚第几次怎么挂的。

    重试通过不等于干净的 PASS，报告里必须能一眼看出这条是靠重试才过的。
    """
    attempts = item.get("attempts") or []
    if len(attempts) <= 1:
        return ""
    return " → ".join(f"{a.get('index')}.{a.get('code')}" for a in attempts)


def _html(payload: dict) -> str:
    task = payload["task"]
    matrix = payload["matrix"]
    stats = payload["stats"]
    heads = "".join(f"<th>{html.escape(title)}</th>" for _i, title in MATRIX_COLUMNS)
    cells = "".join(
        f"<td class='{html.escape(matrix['cells'].get(case_id, ''))}'>{html.escape(matrix['cells'].get(case_id, ''))}</td>"
        for case_id, _title in MATRIX_COLUMNS
    )
    detail_rows = []
    for index, item in enumerate(payload["rounds"], start=1):
        attempts = _attempts_label(item)
        # 重试才过的标黄，让人一眼看到这条不稳。
        note_class = " class='retry'" if attempts and item.get("code") == "PASS" else ""
        detail_rows.append(
            "<tr>"
            f"<td>{index}</td><td>{html.escape(str(item.get('case_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('trigger') or ''))}</td>"
            f"<td>{html.escape(str(item.get('before_version') or ''))}</td>"
            f"<td>{html.escape(str(item.get('after_version') or ''))}</td>"
            f"<td>{html.escape(str(item.get('code') or ''))}</td>"
            f"<td>{html.escape(attempts)}</td>"
            f"<td{note_class}>{html.escape(str(item.get('note') or ''))}</td>"
            "</tr>"
        )
    manual = "、".join(task.get("manual_fields") or []) or "无"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{html.escape(str(task.get('slug') or 'report'))}</title>
<style>
body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; color: #1c2430; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0 28px; }}
th, td {{ border: 1px solid #d5dbe3; padding: 8px 10px; font-size: 13px; text-align: center; }}
th {{ background: #f4f1e6; }}
.PASS {{ background: #d9f5e5; color: #0d7a45; font-weight: 700; }}
.FAIL {{ background: #fde2e1; color: #a12622; font-weight: 700; }}
.HUNG {{ background: #f8d0d0; color: #8d1212; font-weight: 700; }}
.SKIP {{ background: #eceff3; color: #667085; }}
.retry {{ background: #fff4d6; color: #8a5a00; }}
.meta {{ color: #526070; font-size: 14px; line-height: 1.7; }}
</style></head><body>
<h1>OTA 任务报告</h1>
<p class="meta">任务 {html.escape(str(task.get('task_id') or ''))} · 链路 {html.escape(str(task.get('link_name') or ''))} · 策略 {html.escape(str(task.get('strategy') or ''))}<br>
手机 {html.escape(str(matrix['手机型号']))} / Android {html.escape(str(matrix['系统版本']))} · 设备 {html.escape(str(matrix['设备型号']))} · githash {html.escape(str(matrix['固件githash']))} · APP {html.escape(str(matrix['APP版本']))}<br>
人工填写字段：{html.escape(manual)}<br>
轮次 {stats['total']} · 成功 {stats['passed']} · 失败 {stats['failed']} · 死机 {stats['hung']} · 成功率 {stats['success_rate']} · 平均耗时 {stats['avg_elapsed']}s</p>
<table><tr><th>手机型号</th><th>系统</th><th>设备</th><th>githash</th><th>APP</th>{heads}</tr>
<tr><td>{html.escape(str(matrix['手机型号']))}</td><td>{html.escape(str(matrix['系统版本']))}</td><td>{html.escape(str(matrix['设备型号']))}</td><td>{html.escape(str(matrix['固件githash']))}</td><td>{html.escape(str(matrix['APP版本']))}</td>{cells}</tr></table>
<h2>逐轮明细</h2>
<table><tr><th>#</th><th>场景</th><th>触发</th><th>升级前</th><th>升级后</th><th>结果</th><th>尝试</th><th>备注</th></tr>
{''.join(detail_rows)}</table>
</body></html>"""
