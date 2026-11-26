"""单手机锁、场景串行、每场景跑满轮次。同一时刻只跑一个任务。"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from server.common import paths
from server.common.config_loader import case_by_id, load_cases, load_global, load_link
from server.common.logger import TaskLogger
from server.common.report_builder import write_task_report
from server.drivers.adb import AdbDriver
from server.drivers.bt_probe import BluetoothProbe
from server.drivers.fake import FakeAdb, FakeUiDevice
from server.drivers.logfetch import LogFetcher
from server.drivers.uia import UiDriver
from server.engine.state_machine import RoundMachine
from server.links import build_link
from server.links.firmware_select import module_labels, parse_firmware_targets, selected_modules
from server.drivers.phone_ui import case_direction, direction_label

PLACEHOLDER_PKG = "com.example."


class SchedulerBusy(RuntimeError):
    pass


class TaskRejected(RuntimeError):
    pass


@dataclass
class TaskRecord:
    task_id: str
    slug: str
    link: str
    link_name: str
    strategy: str
    cases: list[str]
    firmware_targets: list = field(default_factory=list)
    firmware_label: str = ""
    plan: list = field(default_factory=list)
    loops: int = 1
    case_name: str = ""
    case_round: int = 0
    case_loops: int = 0
    status: str = "pending"
    verdict: str = ""
    state: str = ""
    case_id: str = ""
    round_index: int = 0
    round_total: int = 0
    counts: dict = field(default_factory=lambda: {"PASS": 0, "FAIL_UPGRADE": 0, "HUNG_DEVICE": 0, "SKIP": 0})
    rounds: list = field(default_factory=list)
    phone_model: str = ""
    android: str = ""
    device_model: str = ""
    githash: str = ""
    app_version: str = ""
    serial: str = ""
    manual_fields: list = field(default_factory=list)
    message: str = ""
    paused: bool = False
    created_at: str = ""

    def public(self) -> dict:
        return {
            "task_id": self.task_id,
            "slug": self.slug,
            "link": self.link,
            "link_name": self.link_name,
            "strategy": self.strategy,
            "cases": self.cases,
            "firmware_targets": self.firmware_targets,
            "firmware_label": self.firmware_label,
            "plan": self.plan,
            "loops": self.loops,
            "case_name": self.case_name,
            "case_round": self.case_round,
            "case_loops": self.case_loops,
            "status": self.status,
            "verdict": self.verdict,
            "state": self.state,
            "case_id": self.case_id,
            "round_index": self.round_index,
            "round_total": self.round_total,
            "counts": self.counts,
            "phone_model": self.phone_model,
            "android": self.android,
            "device_model": self.device_model,
            "githash": self.githash,
            "app_version": self.app_version,
            "serial": self.serial,
            "manual_fields": self.manual_fields,
            "message": self.message,
            "paused": self.paused,
            "rounds": self.rounds,
            "report": f"/api/reports/{self.slug}/html" if self.status in {"finished", "hung", "stopped", "error", "paused"} or self.rounds else "",
        }


class Scheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskRecord] = {}
        self._loggers: dict[str, TaskLogger] = {}
        self._stop_flags: dict[str, threading.Event] = {}
        self._resume_flags: dict[str, threading.Event] = {}
        self._active: str | None = None
        self._threads: dict[str, threading.Thread] = {}
        self._beats: dict[str, float] = {}
        # 看门狗的心跳上限。一轮用例本来就可能跑十几分钟，心跳按用例粒度更新，
        # 所以这里给得很宽：一小时没有任何推进才认为是真卡死。
        self.settings_heartbeat_timeout = 3600.0
        self.watchdog_interval = 5.0

    def create(self, payload: dict, *, dry_run: bool = False) -> TaskRecord:
        link_id = payload.get("link") or ""
        plan = _parse_plan(payload)
        case_ids = [item["id"] for item in plan]
        known = {item["id"]: item for item in load_cases()}
        link = load_link(link_id)
        firmware_targets = _firmware_of(payload, link)
        strategy = _strategy_of(case_ids, known)
        if not dry_run and str(link["pkg"]).startswith(PLACEHOLDER_PKG):
            raise TaskRejected(f"{link['name']} 仍是占位包名，请先按 docs/元素校准.md 改 config/app_{_file_id(link_id)}.yaml")
        settings = load_global()
        for key in ("probe_interval", "drop_window", "reconnect_timeout"):
            if payload.get(key) not in (None, ""):
                settings[key] = int(payload[key])
        if settings["reconnect_timeout"] < settings["drop_window"]:
            raise TaskRejected("重连超时不能小于断开观察窗")

        with self._lock:
            if self._active and self._tasks[self._active].status in {"pending", "running", "paused"}:
                raise SchedulerBusy("已有任务在跑，同一时刻只控制一台手机")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = paths.task_slug(link_id, strategy, stamp)
            task_id = slug
            record = TaskRecord(
                task_id=task_id,
                slug=slug,
                link=link_id,
                link_name=link["name"],
                strategy=strategy,
                cases=case_ids,
                firmware_targets=firmware_targets,
                firmware_label=module_labels(selected_modules(link, firmware_targets)),
                plan=plan,
                loops=plan[0]["loops"] if len({item["loops"] for item in plan}) == 1 else 0,
                round_total=sum(item["loops"] for item in plan),
                created_at=stamp,
            )
            self._tasks[task_id] = record
            self._active = task_id
            self._stop_flags[task_id] = threading.Event()
            self._resume_flags[task_id] = threading.Event()
            paths.ensure_runtime()
            logger = TaskLogger(paths.RUNTIME_LOGCACHE / f"{slug}.log")
            logger.bind_archive(paths.log_dir(slug) / "run.log")
            self._loggers[task_id] = logger

        thread = threading.Thread(
            target=self._execute_watch if link_id in {"watch", "glass", "ring"} and not dry_run else self._execute,
            args=(record, link, settings, dry_run, payload.get("manual") or {}),
            name=f"ota-{task_id}",
            daemon=True,
        )
        self._threads[task_id] = thread
        self._beats[task_id] = time.time()
        thread.start()
        threading.Thread(
            target=self._watchdog,
            args=(task_id, self._loggers[task_id]),
            name=f"ota-watchdog-{task_id}",
            daemon=True,
        ).start()
        return record

    def get(self, task_id: str) -> TaskRecord:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def latest(self) -> TaskRecord | None:
        if self._active and self._active in self._tasks:
            return self._tasks[self._active]
        if not self._tasks:
            return None
        return list(self._tasks.values())[-1]

    def list_tasks(self) -> list[dict]:
        return [item.public() for item in self._tasks.values()]

    def logger(self, task_id: str) -> TaskLogger:
        if task_id not in self._loggers:
            raise KeyError(task_id)
        return self._loggers[task_id]

    def stop(self, task_id: str) -> None:
        flag = self._stop_flags.get(task_id)
        if flag is None:
            raise KeyError(task_id)
        flag.set()
        resume = self._resume_flags.get(task_id)
        if resume:
            resume.set()

    def resume(self, task_id: str) -> None:
        task = self.get(task_id)
        if task.status != "paused":
            raise TaskRejected("当前任务没有在等待继续")
        task.paused = False
        task.status = "running"
        self._resume_flags[task_id].set()

    def _stopped(self, task_id: str) -> bool:
        return self._stop_flags[task_id].is_set()

    def _beat(self, task_id: str) -> None:
        """任务线程还活着就更新心跳时间。"""
        self._beats[task_id] = time.time()

    def _sleep_with_stop(self, task_id: str, seconds: float) -> bool:
        """等待指定秒数，期间随时可以被停止打断。返回 False 表示被叫停了。"""
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            if self._stopped(task_id):
                return False
            time.sleep(0.5)
        return True

    def _watchdog(self, task_id: str, logger: TaskLogger) -> None:
        """看着任务线程，发现它真的卡死了就把任务标记为 error。

        为什么需要这个：线程卡死时 `record.status` 会永远停在 running，
        界面上点「开始任务」只会一直报「已有任务在跑」，日志也不再增长，
        但 CPU 是空的、HTTP 接口还正常——2026-11-11 就是这样，让人以为设备死机。

        判据是「线程对象已经不 alive，或者超过上限没有心跳」。故意给足余量，
        因为一轮升级本来就可能跑十几分钟，心跳按用例粒度更新。
        """
        heartbeat_timeout = float(self.settings_heartbeat_timeout)
        while True:
            time.sleep(self.watchdog_interval)
            thread = self._threads.get(task_id)
            if thread is not None and not thread.is_alive():
                return
            beat = self._beats.get(task_id, 0.0)
            if beat and time.time() - beat < heartbeat_timeout:
                continue
            task = self._tasks.get(task_id)
            if task is None or task.status not in {"running", "pending"}:
                return
            task.status = "error"
            task.verdict = "ERROR"
            task.message = (
                f"任务线程超过 {int(heartbeat_timeout)} 秒没有推进，已按卡死处理。"
                "请检查服务是否被控制台输出阻塞，或设备是否长时间无响应。"
            )
            logger.log(task.message, "ERROR")
            logger.log("看门狗终止任务", "ERROR")
            try:
                self._write(task)
            except Exception:
                pass
            return

    def _execute_watch(self, record: TaskRecord, link: dict, settings: dict, dry_run: bool, manual: dict) -> None:
        """当前 App 走真机流程。注入动作来自用例配置。"""
        logger = self._loggers[record.task_id]
        try:
            if record.link == "glass":
                from server.scripts.run_glass_flow import run_glass_case as run_case
            elif record.link == "ring":
                from server.scripts.run_ring_flow import run_ring_case as run_case
            else:
                from server.scripts.run_dafit_flow import run_case

            record.status = "running"
            adb, ui = self._open_device(record, link, settings, dry_run, manual, logger)
            # 先打开 App 设备页再识别：蓝牙经常只有 MAC 没有名字，名字要从页面填。
            self._prime_device_card(adb, ui, link, logger)
            self._capture_identity(record, adb, None, link, manual, ui=ui)
            folder = paths.log_dir(record.slug)
            device = ui.device
            summary = "，".join(f"{item['id']} {item['direction']} {item['name']}×{item['loops']}" for item in record.plan)
            logger.log(f"任务开始 {record.link_name} {record.slug} {summary}")
            if record.firmware_targets:
                logger.log(f"本次固件 {record.firmware_label}")
            # 心跳：线程只要还在推进就更新它。看门狗靠这个判断线程是不是卡死了。
            self._beat(record.task_id)
            consecutive_skip = 0
            consecutive_error = 0
            first_round = True
            for item in record.plan:
                if self._stopped(record.task_id):
                    record.status = "stopped"
                    record.verdict = "已停止"
                    break
                case_id = item["id"]
                for index in range(1, item["loops"] + 1):
                    if self._stopped(record.task_id):
                        record.status = "stopped"
                        record.verdict = "已停止"
                        break
                    # 相邻两轮之间留一点恢复时间。
                    # 手表连续升级时偶尔来不及准备下一次握手，上一轮刚结束就点升级会直接
                    # 弹「固件升级失败」（2026-11-11 全量里 T04/T05/T08 就是这样挂的，
                    # 同样的用例单独跑就完全正常）。第一条不用等。
                    if not first_round:
                        gap = float(settings.get("case_gap_seconds") or 0)
                        if gap > 0 and not self._sleep_with_stop(record.task_id, gap):
                            record.status = "stopped"
                            record.verdict = "已停止"
                            break
                        logger.log(f"用例间隔 {gap:.0f} 秒，等设备恢复")
                    first_round = False
                    record.case_id = case_id
                    record.case_name = item["name"]
                    record.case_round = index
                    record.case_loops = item["loops"]
                    record.round_index += 1
                    self._beat(record.task_id)
                    logger.log(f"用例 {case_id} {item['direction']} {item['name']} 第 {index}/{item['loops']} 次")
                    if record.link == "glass":
                        row = run_case(
                            device, folder, case_id, settings=settings, logger=logger,
                            firmware_targets=record.firmware_targets,
                            stop_check=lambda tid=record.task_id: self._stopped(tid),
                        )
                    else:
                        row = run_case(
                            device, folder, case_id, settings=settings, logger=logger,
                            stop_check=lambda tid=record.task_id: self._stopped(tid),
                        )
                    row["round"] = index
                    record.rounds.append(row)
                    record.counts[row["code"]] = record.counts.get(row["code"], 0) + 1
                    # 重试通过不能记成干净的 PASS，要在日志里点明尝试次数。
                    if row.get("retried"):
                        detail = "、".join(f"第{a['index']}次 {a['code']}" for a in row.get("attempts") or [])
                        logger.log(f"结果 {row['code']} {row['note']}（尝试：{detail}）", "WARN")
                    else:
                        logger.log(f"结果 {row['code']} {row['note']}")
                    if row["code"] == "HUNG_DEVICE":
                        record.status = "hung"
                        record.verdict = "HUNG"
                        record.message = row["note"]
                        logger.log("设备未能重连，终止后续场景", "ERROR")
                        self._mark_remaining_skip(record, case_id, index)
                        break
                    if row["code"] == "ABORT_ENV":
                        record.status = "finished"
                        record.verdict = "ABORT_ENV"
                        record.message = row["note"]
                        logger.log("环境或后台配置不满足，终止任务", "ERROR")
                        self._mark_remaining_skip(record, case_id, index)
                        break
                    if row["code"] == "SKIP":
                        consecutive_skip += 1
                        if consecutive_skip >= settings["max_consecutive_skip"]:
                            record.status = "finished"
                            record.verdict = "SKIP"
                            record.message = "连续多轮未能有效执行，请先看日志中的干扰和页面报错"
                            logger.log("连续 SKIP 达到阈值，终止任务", "ERROR")
                            break
                    else:
                        consecutive_skip = 0
                    if row["code"] == "ERROR":
                        # 单条用例偶发异常不该毁掉整场全量：记下来，继续跑下一条。
                        # 连续多条都异常说明环境或代码真有问题，那才终止。
                        consecutive_error += 1
                        logger.log("单轮执行异常，记 ERROR 后继续", "ERROR")
                        if consecutive_error >= settings["max_consecutive_error"]:
                            record.status = "error"
                            record.verdict = "ERROR"
                            record.message = f"连续 {consecutive_error} 条单轮执行异常，终止任务：{row['note']}"
                            logger.log(record.message, "ERROR")
                            self._mark_remaining_skip(record, case_id, index)
                            break
                    else:
                        consecutive_error = 0
                    if row["code"] not in {"PASS", "FAIL_UPGRADE", "HUNG_DEVICE", "SKIP", "ABORT_ENV", "ERROR"}:
                        record.status = "error"
                        record.verdict = "ERROR"
                        record.message = row["note"] or "单轮执行异常"
                        logger.log("单轮执行异常，终止任务", "ERROR")
                        break
                if record.status in {"hung", "finished", "stopped", "error"} and record.verdict in {"HUNG", "ABORT_ENV", "SKIP", "ERROR", "已停止"}:
                    break
            if record.status == "running":
                record.status = "finished"
                record.verdict = "DONE"
            self._reset_device(device, link, logger)
            self._write(record)
            (folder / "summary.json").write_text(json.dumps(record.rounds, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.log(f"任务结束 status={record.status} verdict={record.verdict}")
        except Exception as exc:
            record.status = "error"
            record.verdict = "ERROR"
            record.message = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            logger.log(record.message, "ERROR")
            self._write(record)

    def _reset_device(self, device, link: dict, logger: TaskLogger) -> None:
        """任务收尾时把 App 从升级页带回主页。

        为什么需要：任务被停止、或某一轮异常中断后，App 会停在「正在升级」的
        僵死页面上，升级其实早就没了。下一条用例进去第一眼就是这个页面，直接报
        「上一轮升级未结束」，整条任务就废了。
        2026-11-11 连着踩了两次，每次都要手工杀 App 才能继续。

        这里在收尾时强制重启一次 App，保证下一次任务从干净的主页开始。
        """
        pkg = link.get("pkg") or ""
        if not pkg or device is None:
            return
        try:
            adb = AdbDriver("adb", getattr(device, "serial", "") or "", timeout=20)
            adb.shell(f"am force-stop {pkg}", check=False)
            time.sleep(1.5)
            adb.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1", check=False)
            time.sleep(1.5)
            logger.log("任务收尾：已重启 App，下一轮从主页开始")
        except Exception as exc:
            logger.log(f"任务收尾时重启 App 失败: {exc}", "WARN")

    def _execute(self, record: TaskRecord, link: dict, settings: dict, dry_run: bool, manual: dict) -> None:
        logger = self._loggers[record.task_id]
        try:
            record.status = "running"
            adb, ui = self._open_device(record, link, settings, dry_run, manual, logger)
            probe = BluetoothProbe(adb, ui, link, settings.get("patterns"))
            clock = _Clock(dry_run)
            runner = build_link(link["id"], link, adb, ui, logger, sleeper=clock.sleep)
            if hasattr(runner, "set_firmware_targets"):
                runner.set_firmware_targets(record.firmware_targets)
            self._capture_identity(record, adb, runner, link, manual)
            summary = "，".join(f"{item['id']} {item['direction']} {item['name']}×{item['loops']}" for item in record.plan)
            logger.log(f"任务开始 {record.slug} {summary}")
            previous = None
            consecutive_skip = 0
            first_round = True
            for item in record.plan:
                if self._stopped(record.task_id) or record.status in {"hung", "paused"}:
                    break
                case_id = item["id"]
                case = case_by_id(case_id)
                for index in range(1, item["loops"] + 1):
                    if self._stopped(record.task_id):
                        record.status = "stopped"
                        record.verdict = "已停止"
                        break
                    # 和 _execute_watch 保持一致：相邻两轮之间留恢复时间。
                    # 这里走虚拟时钟，dry-run 下不会真的等。
                    if not first_round:
                        gap = float(settings.get("case_gap_seconds") or 0)
                        if gap > 0:
                            clock.sleep(gap)
                            logger.log(f"用例间隔 {gap:.0f} 秒，等设备恢复")
                    first_round = False
                    record.case_id = case_id
                    record.case_name = item["name"]
                    record.case_round = index
                    record.case_loops = item["loops"]
                    record.round_index += 1
                    record.state = "S0"
                    logger.log(f"用例 {case_id} {item['direction']} {item['name']} 第 {index}/{item['loops']} 次")
                    machine = RoundMachine(link, case, settings, runner, probe, clock=clock)
                    result = machine.run(lambda: self._stopped(record.task_id), previous)
                    record.state = result.state
                    if result.code == "PAUSE":
                        self._pause_for_policy(record, logger, result.note)
                        if self._stopped(record.task_id):
                            record.status = "stopped"
                            break
                        machine = RoundMachine(link, case, settings, runner, probe, clock=clock)
                        result = machine.run(lambda: self._stopped(record.task_id), previous)
                        record.state = result.state
                    row = {
                        "case_id": case_id,
                        "round": index,
                        "code": result.code,
                        "note": result.note,
                        "trigger": result.trigger,
                        "before_version": result.before_version,
                        "after_version": result.after_version,
                        "announced": result.announced,
                        "direction": result.direction,
                        "elapsed": result.elapsed,
                    }
                    record.rounds.append(row)
                    record.counts[result.code] = record.counts.get(result.code, 0) + 1
                    logger.log(f"结果 {result.code} {result.note}")
                    self._archive_round(record, adb, ui, link, result.code, logger)
                    if result.direction and "->" in result.direction:
                        previous = f"{result.before_version}->{result.announced or result.after_version}"
                    if result.code == "HUNG_DEVICE":
                        record.status = "hung"
                        record.verdict = "HUNG"
                        logger.log("死机，终止后续场景", "ERROR")
                        self._mark_remaining_skip(record, case_id, index)
                        break
                    if result.code == "STALE_POLICY":
                        record.status = "finished"
                        record.verdict = "STALE_POLICY"
                        logger.log("策略失效，终止任务", "ERROR")
                        break
                    if result.code == "ABORT_ENV":
                        record.status = "finished"
                        record.verdict = "ABORT_ENV"
                        logger.log("环境异常，终止任务", "ERROR")
                        break
                    if result.code == "SKIP":
                        consecutive_skip += 1
                        if consecutive_skip >= settings["max_consecutive_skip"]:
                            record.status = "finished"
                            record.verdict = "SKIP"
                            logger.log("连续 SKIP 达到阈值，终止任务", "ERROR")
                            break
                    else:
                        consecutive_skip = 0
                if record.status in {"hung", "stopped", "finished"} and record.verdict in {"HUNG", "STALE_POLICY", "ABORT_ENV", "SKIP"}:
                    break
            if record.status == "running":
                record.status = "finished"
                record.verdict = "DONE"
            self._write(record)
            logger.log(f"任务结束 status={record.status} verdict={record.verdict}")
        except Exception as exc:
            record.status = "error"
            record.verdict = "ERROR"
            record.message = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            logger.log(record.message, "ERROR")
            logger.log(traceback.format_exc().strip(), "ERROR")
            self._write(record)
        finally:
            with self._lock:
                if self._active == record.task_id and record.status not in {"paused"}:
                    pass

    def _pause_for_policy(self, record: TaskRecord, logger: TaskLogger, note: str) -> None:
        record.status = "paused"
        record.paused = True
        record.message = note + "。请在后台切换升级配置后点继续。"
        logger.log(record.message, "WARN")
        self._resume_flags[record.task_id].clear()
        self._resume_flags[record.task_id].wait()
        record.paused = False

    def _mark_remaining_skip(self, record: TaskRecord, case_id: str, done_index: int) -> None:
        """任务提前终止后，剩下的场景记 SKIP。死机和环境不满足要分开写，别混成一句话。"""
        reason = "前序死机，后续不再执行" if record.verdict == "HUNG" else "前序已终止任务，后续不再执行"
        seen = False
        for item in record.plan:
            if not seen:
                if item["id"] != case_id:
                    continue
                seen = True
                start = done_index + 1
            else:
                start = 1
            for _round in range(start, item["loops"] + 1):
                record.rounds.append({
                    "case_id": item["id"],
                    "round": _round,
                    "code": "SKIP",
                    "note": reason,
                    "trigger": "",
                    "before_version": "",
                    "after_version": "",
                    "announced": "",
                    "elapsed": 0,
                })
                record.counts["SKIP"] = record.counts.get("SKIP", 0) + 1

    def _archive_round(self, record, adb, ui, link, code: str, logger: TaskLogger) -> None:
        evidence = paths.evidence_dir(record.slug) / f"{record.case_id}_r{record.round_index}"
        try:
            if ui is not None and hasattr(ui, "save_dump"):
                ui.save_dump(evidence / "ui.xml")
        except Exception as exc:
            logger.log(f"界面 dump 失败: {exc}", "WARN")
        try:
            fetcher = LogFetcher(adb, ui, link)
            if code in {"FAIL_UPGRADE", "HUNG_DEVICE", "STALE_POLICY", "ABORT_ENV"}:
                info = fetcher.collect(paths.log_dir(record.slug) / evidence.name, code)
                if info.get("missing"):
                    logger.log("日志缺失", "WARN")
        except Exception as exc:
            logger.log(f"日志采集失败: {exc}", "WARN")

    def _write(self, record: TaskRecord) -> None:
        try:
            write_task_report(paths.report_dir(record.slug), record.public() | {
                "rounds": record.rounds,
                "phone_model": record.phone_model,
                "android": record.android,
                "device_model": record.device_model,
                "githash": record.githash,
                "app_version": record.app_version,
                "manual_fields": record.manual_fields,
            })
        except Exception as exc:
            record.message = f"报告写入失败: {exc}"

    def _open_device(self, record, link, settings, dry_run, manual, logger):
        if dry_run:
            adb = FakeAdb(device_name=link.get("device_name") or "L806", device_mac=link.get("device_mac") or "00:11:22:33:44:55")
            if manual.get("disconnect"):
                adb.connected = False
                adb.visible = False
            ui = UiDriver(FakeUiDevice(), link.get("fallback"))
            record.serial = adb.serial
            logger.log("dry-run：使用假设备，不调用真机")
            return adb, ui
        adb = AdbDriver(settings.get("adb_path") or "adb")
        serials = adb.devices()
        if len(serials) != 1:
            raise TaskRejected(f"需要且仅需要一台处于 device 状态的手机，当前 {len(serials)} 台")
        adb.serial = serials[0]
        record.serial = serials[0]
        ui = UiDriver.connect(serials[0], link.get("fallback"))
        return adb, ui

    def _prime_device_card(self, adb, ui, link: dict, logger) -> None:
        """打开 App 设备卡片页，方便识别设备名/MAC。失败不阻断任务。"""
        pkg = link.get("pkg") or ""
        if not pkg or ui is None or not hasattr(ui, "device"):
            return
        try:
            from server.common.device_identity import open_device_card

            page = open_device_card(ui.device, adb, link)
            if page:
                logger.log("已打开 App 设备页，准备识别穿戴设备")
            else:
                logger.log("打开 App 设备页未读到内容，稍后再用蓝牙兜底", "WARN")
        except Exception as exc:
            logger.log(f"打开 App 设备页识别失败（可忽略）: {exc}", "WARN")

    def _capture_identity(self, record, adb, runner, link, manual, ui=None) -> None:
        from server.common.device_identity import apply_identity, looks_like_phone, resolve_identity

        snap = adb.snapshot(link["pkg"])
        record.phone_model = snap.model
        record.android = snap.android
        record.app_version = snap.app_version
        read_text = None
        page = None
        if runner is not None and getattr(runner, "ui", None) is not None:
            read_text = runner.ui.read_text
            try:
                page = runner.ui.page_text()
            except Exception:
                page = None
        elif ui is not None:
            if getattr(ui, "read_text", None) is not None:
                read_text = ui.read_text
            try:
                page = ui.page_text() if hasattr(ui, "page_text") else None
            except Exception:
                page = None
        identity = resolve_identity(link, adb=adb, read_text=read_text, page=page)
        apply_identity(link, identity)
        try:
            env = runner.precheck() if runner is not None else {}
        except Exception:
            env = {}
        record.device_model = (
            env.get("device_model")
            or identity.get("name")
            or link.get("device_name")
            or ""
        )
        record.githash = env.get("githash") or ""
        manual = manual or {}
        filled = []
        for key, attr in (("phone_model", "phone_model"), ("android", "android"), ("device_model", "device_model"), ("githash", "githash"), ("app_version", "app_version")):
            if manual.get(key):
                # 预填的设备型号如果就是手机名，不能盖掉已经识别到的穿戴设备。
                if key == "device_model" and looks_like_phone(str(manual[key]), record.phone_model):
                    resolved = record.device_model or ""
                    if resolved and not looks_like_phone(resolved, record.phone_model):
                        continue
                    if not resolved:
                        continue
                setattr(record, attr, str(manual[key]))
                filled.append(key)
            elif not getattr(record, attr):
                filled.append(key)
        record.manual_fields = [item for item in filled if manual.get(item)]
        if identity.get("mac") or identity.get("name"):
            logger = self._loggers.get(record.task_id)
            if logger:
                logger.log(
                    f"识别设备 {identity.get('name') or '?'} "
                    f"{identity.get('mac') or ''}（来源 {identity.get('source')}）"
                )

def _parse_plan(payload: dict) -> list[dict]:
    raw = list(payload.get("cases") or [])
    if not raw:
        raise TaskRejected("至少选择一个场景")
    known = {item["id"]: item for item in load_cases()}
    try:
        link = load_link(payload.get("link") or "")
    except Exception:
        link = {}
    default_loops = payload.get("loops")
    plan = []
    seen = set()
    for item in raw:
        if isinstance(item, str):
            case_id = item
            loops = default_loops if default_loops not in (None, "") else 1
        elif isinstance(item, dict):
            case_id = str(item.get("id") or item.get("case_id") or "")
            loops = item.get("loops")
            if loops in (None, ""):
                loops = default_loops if default_loops not in (None, "") else 1
        else:
            raise TaskRejected("场景格式不正确")
        if case_id not in known:
            raise TaskRejected("未知场景: " + case_id)
        try:
            count = int(loops)
        except (TypeError, ValueError) as exc:
            raise TaskRejected(f"{case_id} 的次数不是整数") from exc
        if count < 1:
            raise TaskRejected(f"{case_id} 的次数至少为 1")
        if count > 10000:
            raise TaskRejected(f"{case_id} 的次数不能超过 10000")
        if case_id in seen:
            raise TaskRejected(f"{case_id} 重复了，把次数填在这一条上")
        seen.add(case_id)
        case = known[case_id]
        plan.append({
            "id": case_id,
            "name": case["name"],
            "loops": count,
            "direction": direction_label(case_direction(case, link)),
        })
    return plan


def _firmware_of(payload: dict, link: dict) -> list[str]:
    if "firmware_targets" not in payload:
        return parse_firmware_targets(None, link)
    return parse_firmware_targets(payload.get("firmware_targets"), link)


def _strategy_of(case_ids: list[str], known: dict) -> str:
    strategies = {known[item].get("strategy") for item in case_ids}
    strategies.discard(None)
    strategies.discard("")
    if "s1" in strategies and "s2" in strategies:
        raise TaskRejected("S1 与 S2 不能放在同一次任务里")
    if "s1" in strategies:
        return "s1"
    if "s2" in strategies:
        return "s2"
    return "baseline"


class _Clock:
    """dry-run 用虚拟时钟，避免探测间隔真的睡 5 秒。实机走 monotonic。"""

    def __init__(self, dry: bool) -> None:
        self.dry = dry
        self.now = 0.0

    def __call__(self) -> float:
        if self.dry:
            return self.now
        import time
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if self.dry:
            self.now += float(seconds or 0)
            return
        if seconds and seconds > 0:
            import time
            time.sleep(seconds)


def _file_id(link_id: str) -> str:
    return {"glass": "glass", "ring": "ring", "watch": "watch"}.get(link_id, link_id)


def list_report_slugs() -> list[str]:
    paths.ensure_output()
    slugs = []
    for path in sorted(paths.OUTPUT_REPORTS.iterdir(), reverse=True):
        if path.is_dir() and (path / "report.html").is_file():
            slugs.append(path.name)
    return slugs


def report_file(slug: str, name: str) -> Path:
    path = (paths.report_dir(slug) / name).resolve()
    root = paths.OUTPUT_REPORTS.resolve()
    if root not in path.parents:
        raise FileNotFoundError(slug)
    if not path.is_file():
        raise FileNotFoundError(path)
    return path
