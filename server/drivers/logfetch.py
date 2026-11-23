"""日志采集按三级降级：APP 导出 → 按进程 logcat → 系统 logcat。都不成功就记「日志缺失」。"""

from __future__ import annotations

from pathlib import Path


class LogFetcher:
    def __init__(self, adb, ui, link: dict) -> None:
        self.adb = adb
        self.ui = ui
        self.link = link

    def collect(self, dest_dir: Path, reason: str) -> dict:
        dest_dir.mkdir(parents=True, exist_ok=True)
        record = {"reason": reason, "level": "", "files": [], "missing": False}
        export = self.link.get("log_export") or {}
        if export.get("support") and export.get("steps") and self.ui is not None:
            pulled = self._export_app(dest_dir, export)
            if pulled:
                record["level"] = "app_export"
                record["files"].extend(pulled)
                return record
        pkg = self.link["pkg"]
        pid = self.adb.pid_of(pkg)
        if pid:
            text = self.adb.shell(f"logcat -d --pid={pid}", check=False)
            if text.strip():
                path = dest_dir / "logcat_app.txt"
                path.write_text(text, encoding="utf-8")
                record["level"] = "logcat_pid"
                record["files"].append(str(path))
                return record
        system = self.adb.shell("logcat -d -t 400", check=False)
        if system.strip():
            path = dest_dir / "logcat_system.txt"
            path.write_text(system, encoding="utf-8")
            record["level"] = "logcat_system"
            record["files"].append(str(path))
            return record
        record["missing"] = True
        record["level"] = "missing"
        (dest_dir / "LOG_MISSING.txt").write_text("三级日志采集都失败\n", encoding="utf-8")
        return record

    def _export_app(self, dest_dir: Path, export: dict) -> list[str]:
        try:
            for step in export.get("steps") or []:
                self.ui.click("log_step", {"type": "text", "value": step})
        except Exception:
            return []
        remote = (export.get("device_path") or "").rstrip("/")
        if not remote:
            return []
        local = dest_dir / "app_export"
        local.mkdir(parents=True, exist_ok=True)
        try:
            self.adb.pull(remote, str(local))
        except Exception:
            return []
        files = [str(item) for item in local.rglob("*") if item.is_file()]
        return files
