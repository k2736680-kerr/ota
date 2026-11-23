"""任务接口：创建、停止、继续、查状态、拉日志。"""

from __future__ import annotations

from server.engine.scheduler import Scheduler, SchedulerBusy, TaskRejected


class TaskApi:
    def __init__(self, scheduler: Scheduler, dry_run: bool) -> None:
        self.scheduler = scheduler
        self.dry_run = dry_run

    def create(self, payload: dict) -> dict:
        try:
            task = self.scheduler.create(payload, dry_run=self.dry_run or bool(payload.get("dry_run")))
        except SchedulerBusy as exc:
            return {"ok": False, "status": 409, "message": str(exc)}
        except (TaskRejected, KeyError, ValueError) as exc:
            return {"ok": False, "status": 400, "message": str(exc)}
        return {"ok": True, "status": 200, "task": task.public()}

    def status(self, task_id: str) -> dict:
        try:
            return {"ok": True, "task": self.scheduler.get(task_id).public()}
        except KeyError:
            return {"ok": False, "status": 404, "message": "任务不存在"}

    def log(self, task_id: str, offset: int) -> dict:
        try:
            lines, next_offset = self.scheduler.logger(task_id).since(offset)
        except KeyError:
            return {"ok": False, "status": 404, "message": "任务不存在"}
        return {"ok": True, "lines": lines, "offset": next_offset}

    def stop(self, task_id: str) -> dict:
        try:
            self.scheduler.stop(task_id)
        except KeyError:
            return {"ok": False, "status": 404, "message": "任务不存在"}
        return {"ok": True}

    def resume(self, task_id: str) -> dict:
        try:
            self.scheduler.resume(task_id)
        except KeyError:
            return {"ok": False, "status": 404, "message": "任务不存在"}
        except TaskRejected as exc:
            return {"ok": False, "status": 400, "message": str(exc)}
        return {"ok": True}
