"""报告列表与文件读取。只允许 output/reports 之内。"""

from __future__ import annotations

from server.engine.scheduler import list_report_slugs, report_file


def index() -> dict:
    return {"reports": [{"slug": slug, "html": f"/api/reports/{slug}/html", "csv": f"/api/reports/{slug}/csv"} for slug in list_report_slugs()]}


def load(slug: str, kind: str) -> tuple[bytes, str]:
    name = "report.html" if kind == "html" else "report.csv"
    path = report_file(slug, name)
    media = "text/html; charset=utf-8" if kind == "html" else "text/csv; charset=utf-8"
    return path.read_bytes(), media
