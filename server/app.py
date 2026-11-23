"""本地服务。只听 127.0.0.1，页面与接口都从这里出去。"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from server.api.configs import catalog
from server.api.env import device_view, precheck, switch_check
from server.api.reports import index as report_index
from server.api.reports import load as load_report
from server.api.tasks import TaskApi
from server.common import paths
from server.engine.scheduler import Scheduler

HOST = "127.0.0.1"
PORT = 8765


def create_app(dry_run: bool = False) -> FastAPI:
    paths.ensure_runtime()
    paths.ensure_output()
    app = FastAPI(title="穿戴 OTA 自动化", docs_url=None, redoc_url=None)
    app.state.dry_run = dry_run
    app.state.scheduler = Scheduler()
    app.state.tasks = TaskApi(app.state.scheduler, dry_run)
    if paths.WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=paths.WEB_DIR), name="static")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost"}:
            return JSONResponse({"ok": False, "message": "只接受本机访问"}, status_code=403)
        return await call_next(request)

    @app.get("/")
    def home():
        page = paths.WEB_DIR / "index.html"
        return FileResponse(
            page,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.middleware("http")
    async def no_store_web_assets(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path or ""
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/env/device")
    def env_device(link: str = ""):
        return device_view(app.state.dry_run, link)

    @app.get("/api/env/precheck")
    def env_precheck(link: str):
        try:
            return precheck(link, app.state.dry_run)
        except KeyError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    @app.get("/api/config/apps")
    def config_apps():
        return catalog()

    @app.post("/api/tasks")
    async def create_task(request: Request):
        payload = await request.json()
        result = app.state.tasks.create(payload)
        status = result.get("status", 200)
        return JSONResponse(result, status_code=status)

    @app.get("/api/tasks/{task_id}")
    def task_status(task_id: str):
        result = app.state.tasks.status(task_id)
        if not result.get("ok"):
            return JSONResponse(result, status_code=result.get("status", 404))
        return result

    @app.get("/api/tasks/{task_id}/log")
    def task_log(task_id: str, offset: int = 0):
        result = app.state.tasks.log(task_id, offset)
        if not result.get("ok"):
            return JSONResponse(result, status_code=result.get("status", 404))
        return result

    @app.post("/api/tasks/{task_id}/stop")
    def task_stop(task_id: str):
        result = app.state.tasks.stop(task_id)
        if not result.get("ok"):
            return JSONResponse(result, status_code=result.get("status", 404))
        return result

    @app.post("/api/tasks/{task_id}/resume")
    def task_resume(task_id: str):
        result = app.state.tasks.resume(task_id)
        if not result.get("ok"):
            return JSONResponse(result, status_code=result.get("status", 400))
        return result

    @app.get("/api/reports")
    def reports():
        return report_index()

    @app.get("/api/reports/{slug}/html")
    def report_html(slug: str):
        try:
            body, media = load_report(slug, "html")
        except FileNotFoundError:
            return JSONResponse({"ok": False, "message": "报告不存在"}, status_code=404)
        return HTMLResponse(body)

    @app.get("/api/reports/{slug}/csv")
    def report_csv(slug: str):
        try:
            body, media = load_report(slug, "csv")
        except FileNotFoundError:
            return JSONResponse({"ok": False, "message": "报告不存在"}, status_code=404)
        return Response(body, media_type=media, headers={"Content-Disposition": f"attachment; filename={slug}.csv"})

    @app.post("/api/system/cleanup")
    def cleanup():
        paths.ensure_runtime()
        removed = []
        for child in paths.RUNTIME_DIR.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed.append(child.name)
        paths.ensure_runtime()
        return {"ok": True, "removed": removed, "rebuilt": [path.name for path in paths.RUNTIME_CHILDREN]}

    @app.post("/api/env/switch_check")
    def switch_check_route():
        result = switch_check(app.state.dry_run)
        code = int(result.get("status") or 200)
        if code != 200:
            return JSONResponse(result, status_code=code)
        return result

    @app.get("/api/system/dirs")
    def dirs():
        return {
            "logs": str(paths.OUTPUT_LOGS),
            "evidence": str(paths.OUTPUT_EVIDENCE),
            "reports": str(paths.OUTPUT_REPORTS),
            "runtime": str(paths.RUNTIME_DIR),
        }

    return app
