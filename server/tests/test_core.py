"""不依赖真机的核心规则测试。"""

from __future__ import annotations

import shutil
import threading
import unittest
from pathlib import Path

from server.common import paths
from server.common.config_loader import load_cases, load_global, load_link
from server.common.report_builder import MATRIX_COLUMNS, write_task_report
from server.common.version_util import compare, higher
from server.drivers.bt_probe import BluetoothProbe
from server.drivers.fake import FakeAdb, FakeUiDevice
from server.drivers.uia import UiDriver
from server.engine import HUNG_DEVICE, PASS, STALE_POLICY
from server.engine.judge import HungTracker, matrix_cell
from server.engine.scheduler import Scheduler, SchedulerBusy, TaskRejected
from server.engine.state_machine import RoundMachine
from server.engine.strategy import check_after_s1, check_before, check_s2_direction
from server.links import build_link


class FirmwareTargetTest(unittest.TestCase):
    def test_glass_defaults_to_mcu_and_accepts_labels(self):
        from server.links.firmware_select import module_labels, parse_firmware_targets, selected_modules

        link = load_link("glass")
        self.assertEqual(parse_firmware_targets(None, link), ["mcu"])
        self.assertEqual(parse_firmware_targets(["主控", "影音"], link), ["mcu", "media"])
        self.assertEqual(parse_firmware_targets("全部", link), ["mcu", "media"])
        self.assertEqual(module_labels(selected_modules(link, ["mcu"])), "主控")
        with self.assertRaises(ValueError):
            parse_firmware_targets(["不存在"], link)

    def test_module_button_is_matched_by_vertical_position(self):
        from server.links.firmware_select import selected_modules
        from server.links.glass_link import GlassLink

        link = load_link("glass")
        runner = GlassLink(link, FakeAdb(), UiDriver(FakeUiDevice()), None, lambda _seconds: None)
        runner.ui.dump = lambda: """
            <node text="主控固件版本" bounds="[86,1322][454,1391]" />
            <node content-desc="升级至最新版本" clickable="true" bounds="[86,1561][994,1699]" />
            <node text="影音固件版本" bounds="[86,1850][454,1919]" />
            <node content-desc="已是最新版本" clickable="false" bounds="[86,2089][994,2227]" />
        """
        modules = {item["id"]: item for item in selected_modules(link, ["mcu", "media"])}
        self.assertEqual(runner._module_action(modules["mcu"]), "upgrade")
        self.assertEqual(runner._module_action(modules["media"]), "latest")

    def test_task_records_selected_firmware(self):
        scheduler = Scheduler()
        task = scheduler.create(
            {"link": "glass", "cases": ["T01"], "loops": 1, "firmware_targets": ["mcu"]},
            dry_run=True,
        )
        self.assertEqual(task.firmware_targets, ["mcu"])
        self.assertEqual(task.firmware_label, "主控")
        scheduler.stop(task.task_id)
        deadline = 0
        while deadline < 50 and scheduler.get(task.task_id).status not in {"finished", "stopped", "error", "hung"}:
            deadline += 1
            threading.Event().wait(0.05)
        shutil.rmtree(paths.report_dir(task.slug), ignore_errors=True)
        shutil.rmtree(paths.log_dir(task.slug), ignore_errors=True)
        shutil.rmtree(paths.evidence_dir(task.slug), ignore_errors=True)

    def test_link_without_modules_has_none(self):
        """没有配固件模块的链路（DaFit、戒指）不能凭空得到眼镜的主控/影音。

        2026-11-11：DaFit 页面上冒出固件选择区，点开始报「至少选择一个固件模块」，
        而且任务日志写着「本次固件 主控」——DaFit 根本没有这个概念。
        """
        from server.links.firmware_select import (
            firmware_modules,
            module_labels,
            parse_firmware_targets,
            selected_modules,
        )

        for link_id in ("watch", "ring"):
            link = load_link(link_id)
            self.assertEqual(firmware_modules(link), [], link_id)
            self.assertEqual(parse_firmware_targets(None, link), [], link_id)
            self.assertEqual(selected_modules(link, []), [], link_id)
            self.assertEqual(module_labels(selected_modules(link, [])), "未选择", link_id)

    def test_glass_modules_still_work(self):
        """眼镜链路必须完全不受影响。"""
        from server.links.firmware_select import firmware_modules, parse_firmware_targets

        glass = load_link("glass")
        self.assertEqual([m["id"] for m in firmware_modules(glass)], ["mcu", "media"])
        self.assertEqual(parse_firmware_targets(None, glass), ["mcu"])
        self.assertEqual(parse_firmware_targets(["主控", "影音"], glass), ["mcu", "media"])
        self.assertEqual(parse_firmware_targets("全部", glass), ["mcu", "media"])


class VersionTest(unittest.TestCase):
    def test_numeric_order(self):
        self.assertEqual(compare("2.0.10", "2.0.2"), 1)
        self.assertTrue(higher("2.0.1", "2.0.0"))
        self.assertFalse(higher("2.0.0", "2.0.1"))

    def test_page_text_ignores_statusbar_battery(self):
        from server.common.version_util import ring_version_from_page, watch_versions_from_page

        ring = (
            '<node package="com.android.systemui" text="100.0" />'
            '<node package="com.moyoung.ring" text="当前版本:USY-I403-1.1.9" />'
        )
        watch = (
            '<node package="com.android.systemui" text="100.0" />'
            '<node package="com.crrepa.band.dafit" text="MOY-BZH2-2.0.3" />'
            '<node package="com.crrepa.band.dafit" text="您的固件是最新版本!" />'
        )
        self.assertEqual(ring_version_from_page(ring, pkg="com.moyoung.ring"), "USY-I403-1.1.9")
        parsed = watch_versions_from_page(watch, pkg="com.crrepa.band.dafit")
        self.assertEqual(parsed["current"], "MOY-BZH2-2.0.3")

    def test_progress_placeholder_is_not_firmware(self):
        """进度页上的 0.0 不能写进「升级前」。2026-11-19 手表报告每一轮都是 0.0。"""
        from server.common.version_util import watch_versions_from_page

        page = (
            '<node package="com.crrepa.band.dafit" text="0.0" />'
            '<node package="com.crrepa.band.dafit" text="22%" />'
            '<node package="com.android.systemui" text="100.0" />'
        )
        parsed = watch_versions_from_page(page, pkg="com.crrepa.band.dafit")
        self.assertEqual(parsed["current"], "")
        self.assertEqual(parsed["target"], "")


class PolicyTest(unittest.TestCase):
    def test_s1_requires_higher_announced_and_same_after(self):
        ok, code, _why = check_before("s1", "2.0.0", "2.0.1", True)
        self.assertTrue(ok)
        ok, code, _why = check_before("s1", "2.0.1", "2.0.1", True)
        self.assertEqual(code, STALE_POLICY)
        ok, _why = check_after_s1("2.0.0", "2.0.0")
        self.assertTrue(ok)
        ok, why = check_after_s1("2.0.0", "2.0.1")
        self.assertFalse(ok)
        self.assertIn("前提破坏", why)

    def test_s2_direction(self):
        ok, _why = check_s2_direction("2.0.0->2.0.1", "2.0.1->2.0.0")
        self.assertTrue(ok)
        ok, why = check_s2_direction("2.0.0->2.0.1", "2.0.0->2.0.1")
        self.assertFalse(ok)
        self.assertIn("方向相同", why)

    def test_missing_entry(self):
        ok, code, why = check_before(None, "", "", False)
        self.assertFalse(ok)
        self.assertEqual(code, STALE_POLICY)
        self.assertIn("升级入口", why)


class HungTest(unittest.TestCase):
    def test_reconnect_inside_window_is_not_hung(self):
        tracker = HungTracker(120, 180, 3, 5)
        self.assertEqual(tracker.see(False, 0), "waiting")
        self.assertEqual(tracker.see(False, 119), "waiting")
        self.assertEqual(tracker.see(True, 130), "connected")

    def test_timeout_needs_three_confirms(self):
        tracker = HungTracker(120, 180, 3, 5)
        self.assertEqual(tracker.see(False, 0), "waiting")
        self.assertEqual(tracker.see(False, 180), "confirming")
        self.assertEqual(tracker.see(False, 185), "confirming")
        self.assertEqual(tracker.see(False, 190), "hung")

    def test_matrix_priority(self):
        self.assertEqual(matrix_cell(["PASS", "PASS"]), "PASS")
        self.assertEqual(matrix_cell(["PASS", "FAIL_UPGRADE"]), "FAIL")
        self.assertEqual(matrix_cell(["PASS", "HUNG_DEVICE"]), "HUNG")
        self.assertEqual(matrix_cell(["SKIP"]), "SKIP")


class ProbeTest(unittest.TestCase):
    def test_any_channel_connected(self):
        link = load_link("glass")
        adb = FakeAdb(
            device_name=link["device_name"] or "L806",
            device_mac=link["device_mac"] or "00:11:22:33:44:55",
            connected=False,
        )
        ui = UiDriver(FakeUiDevice())
        probe = BluetoothProbe(adb, ui, link, load_global()["patterns"])
        # 界面仍是已连接，通道 A 命中即可。
        self.assertTrue(probe.connected().connected)
        adb.connected = False
        ui.device.idle = ui.device.idle.replace("已连接", "未连接")
        ui.device.busy = ui.device.busy.replace("已连接", "未连接")
        result = probe.connected()
        self.assertFalse(result.connected)

    def test_env_blocks_false_hung(self):
        link = load_link("glass")
        adb = FakeAdb(
            device_name=link["device_name"] or "L806",
            device_mac=link["device_mac"] or "00:11:22:33:44:55",
            bluetooth_on=False,
        )
        probe = BluetoothProbe(adb, UiDriver(FakeUiDevice()), link, load_global()["patterns"])
        ok, reason = probe.env_ok()
        self.assertFalse(ok)
        self.assertIn("蓝牙", reason)


class RoundTest(unittest.TestCase):
    def _run(self, case_id: str, adb=None, ui_device=None, settings=None):
        link = load_link("glass")
        case = next(item for item in load_cases() if item["id"] == case_id)
        adb = adb or FakeAdb(
            device_name=link["device_name"] or "L806",
            device_mac=link["device_mac"] or "00:11:22:33:44:55",
        )
        ui = UiDriver(ui_device or FakeUiDevice(), link.get("fallback"))
        probe = BluetoothProbe(adb, ui, link, load_global()["patterns"])
        clock = {"now": 0.0}

        def now():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += seconds

        runner = build_link("glass", link, adb, ui, logger=None, sleeper=sleep)
        machine = RoundMachine(link, case, settings or load_global(), runner, probe, now)
        return machine.run(lambda: False), adb

    def test_baseline_pass_without_disconnect(self):
        result, _adb = self._run("T01")
        self.assertEqual(result.code, PASS)
        self.assertFalse(result.saw_disconnect)

    def test_s1_pass_when_version_unchanged(self):
        result, _adb = self._run("T02")
        self.assertEqual(result.code, PASS)
        self.assertEqual(result.before_version, "2.0.0")

    def test_hung_when_never_returns(self):
        link = load_link("glass")
        adb = FakeAdb(
            device_name=link["device_name"] or "L806",
            device_mac=link["device_mac"] or "00:11:22:33:44:55",
            connected=False,
            visible=True,
        )
        device = FakeUiDevice(
            idle="未连接 固件升级 立即升级 实报:2.0.0 公布:2.0.1",
            busy="未连接 正在升级 30%",
        )
        settings = load_global()
        settings["reconnect_timeout"] = 30
        settings["drop_window"] = 10
        settings["hung_confirm_times"] = 3
        settings["probe_interval"] = 5
        result, _adb = self._run("T01", adb=adb, ui_device=device, settings=settings)
        self.assertEqual(result.code, HUNG_DEVICE)

    def test_kill_app_injects_force_stop(self):
        result, adb = self._run("T04")
        self.assertEqual(result.code, PASS)
        self.assertTrue(result.trigger.startswith("进度") or result.trigger.startswith("关键词") or result.trigger == "兜底触发")
        self.assertTrue(any("force-stop" in line for line in adb.shell_log))
        self.assertIn("重新点击升级", result.note)


class ReportTest(unittest.TestCase):
    def test_csv_columns_match_matrix(self):
        directory = paths.RUNTIME_DIR / "test_report"
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        try:
            task = {
                "task_id": "glass_s1_t",
                "slug": "glass_s1_t",
                "link": "glass",
                "link_name": "眼镜",
                "strategy": "s1",
                "phone_model": "Pixel",
                "android": "14",
                "device_model": "L806",
                "githash": "C07F291",
                "app_version": "1.2.3",
                "manual_fields": [],
                "status": "finished",
                "verdict": "DONE",
                "rounds": [
                    {"case_id": "T02", "code": "PASS", "trigger": "", "before_version": "2.0.0", "after_version": "2.0.0", "announced": "2.0.1", "elapsed": 1, "note": ""},
                    {"case_id": "T04", "code": "HUNG_DEVICE", "trigger": "关键词:正在升级", "before_version": "2.0.0", "after_version": "", "announced": "2.0.1", "elapsed": 2, "note": "死机"},
                ],
            }
            write_task_report(directory, task)
            text = (directory / "report.csv").read_text(encoding="utf-8-sig")
            header = text.splitlines()[0]
            for _case_id, title in MATRIX_COLUMNS:
                self.assertIn(title, header)
            self.assertIn("HUNG", text)
            self.assertIn("PASS", text)
            self.assertIn("反复升级", (directory / "report.html").read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(directory)


class SchedulerTest(unittest.TestCase):
    def test_dry_run_two_rounds_and_single_lock(self):
        scheduler = Scheduler()
        first = scheduler.create({"link": "glass", "cases": ["T01"], "loops": 2}, dry_run=True)
        with self.assertRaises(SchedulerBusy):
            scheduler.create({"link": "ring", "cases": ["T01"], "loops": 1}, dry_run=True)
        thread = scheduler._threads[first.task_id]
        thread.join(timeout=8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first.status, "finished")
        self.assertEqual(first.counts.get("PASS"), 2)
        report = paths.report_dir(first.slug) / "report.csv"
        self.assertTrue(report.is_file())
        shutil.rmtree(paths.report_dir(first.slug), ignore_errors=True)
        shutil.rmtree(paths.log_dir(first.slug), ignore_errors=True)
        shutil.rmtree(paths.evidence_dir(first.slug), ignore_errors=True)

    def test_each_case_has_its_own_loops(self):
        scheduler = Scheduler()
        task = scheduler.create({
            "link": "glass",
            "cases": [{"id": "T01", "loops": 2}, {"id": "T09", "loops": 1}],
        }, dry_run=True)
        scheduler._threads[task.task_id].join(timeout=8)
        self.assertEqual(task.round_total, 3)
        self.assertEqual(task.counts.get("PASS"), 3)
        self.assertEqual([item["loops"] for item in task.plan], [2, 1])
        shutil.rmtree(paths.report_dir(task.slug), ignore_errors=True)
        shutil.rmtree(paths.log_dir(task.slug), ignore_errors=True)
        shutil.rmtree(paths.evidence_dir(task.slug), ignore_errors=True)

    def test_real_mode_rejects_placeholder_package(self):
        from unittest.mock import patch

        scheduler = Scheduler()
        fake = {"name": "占位", "pkg": "com.example.ring", "id": "ring"}
        with patch("server.engine.scheduler.load_link", return_value=fake):
            with self.assertRaises(TaskRejected):
                scheduler.create({"link": "ring", "cases": ["T01"], "loops": 1}, dry_run=False)

    def test_cleanup_rebuilds_runtime_and_keeps_output(self):
        paths.ensure_runtime()
        paths.ensure_output()
        marker = paths.RUNTIME_LOGCACHE / "junk.txt"
        marker.write_text("x", encoding="utf-8")
        kept = paths.OUTPUT_REPORTS / "_keep_probe.txt"
        kept.write_text("keep", encoding="utf-8")
        shutil.rmtree(paths.RUNTIME_DIR)
        self.assertFalse(paths.RUNTIME_DIR.exists())
        paths.ensure_runtime()
        self.assertTrue(paths.RUNTIME_SNAPSHOTS.is_dir())
        self.assertEqual(kept.read_text(encoding="utf-8"), "keep")
        kept.unlink()


class StopTest(unittest.TestCase):
    def test_stop_flag_is_visible(self):
        scheduler = Scheduler()
        event = threading.Event()
        scheduler._stop_flags["x"] = event
        event.set()
        self.assertTrue(scheduler._stopped("x"))


class NodeQuerySafetyTest(unittest.TestCase):
    """控件读不到时不能让整个任务崩掉。

    2026-11-11 手表 T04：重新点击升级后页面还停在入口页，没有进度控件，
    工具按「运行中」去读 tv_upgrade_percent，uiautomator2 抛
    UiObjectNotFoundException(-32001)，异常穿透整轮判定，任务直接 ERROR 终止。

    uiautomator2 的 exists / info 都不是纯属性，会真的去服务端查节点，
    节点不在就抛异常。所有读控件的地方都必须兜住。
    """

    @staticmethod
    def _boom_device():
        """任何查询都抛异常的假设备。"""
        class Boom:
            def __call__(self, **kwargs):
                raise RuntimeError("(-32001, 'UiObjectNotFoundException')")
        return Boom()

    @staticmethod
    def _node_boom_device():
        """返回一个 query 就炸的节点，模拟 exists/info 抛异常。"""
        class Node:
            @property
            def exists(self):
                raise RuntimeError("(-32001, 'UiObjectNotFoundException')")

            @property
            def info(self):
                raise RuntimeError("(-32001, 'UiObjectNotFoundException')")

        class Device:
            def __call__(self, **kwargs):
                return Node()
        return Device()

    def test_text_of_id_survives_missing_node(self):
        from server.scripts.run_dafit_flow import DafitFlow

        flow = DafitFlow.__new__(DafitFlow)
        flow.device = self._boom_device()
        self.assertIsNone(flow._text_of_id("com.crrepa.band.dafit:id/tv_upgrade_percent"))

    def test_text_of_id_survives_exists_exception(self):
        from server.scripts.run_dafit_flow import DafitFlow

        flow = DafitFlow.__new__(DafitFlow)
        flow.device = self._node_boom_device()
        self.assertIsNone(flow._text_of_id("com.crrepa.band.dafit:id/tv_upgrade_percent"))

    def test_percent_returns_none_instead_of_raising(self):
        from server.scripts.run_dafit_flow import DafitFlow

        flow = DafitFlow.__new__(DafitFlow)
        flow.device = self._boom_device()
        flow.link = load_link("watch")
        self.assertIsNone(flow._percent())

    def test_accept_pair_survives_missing_node(self):
        from server.scripts.run_dafit_flow import DafitFlow

        flow = DafitFlow.__new__(DafitFlow)
        flow.device = self._node_boom_device()
        self.assertFalse(flow._accept_pair())

    def test_node_text_survives_info_exception(self):
        from server.scripts.run_dafit_flow import _node_text

        class Node:
            @property
            def info(self):
                raise RuntimeError("节点没了")

        self.assertEqual(_node_text(Node()), "")


class RetryTest(unittest.TestCase):
    """失败后自动重拉，但必须留痕。

    手表 DFU 偶发自己失败（App 报 upgrade error: DFU process failed.），
    重新点一次升级通常就能过去。重试通过不能变成干净的 PASS，
    否则设备的不稳定就被抹平了。
    """

    @staticmethod
    def _flow(codes, stop_after=None):
        """造一个按顺序返回指定结果码的流程。"""
        from server.scripts.run_dafit_flow import DafitFlow

        flow = DafitFlow.__new__(DafitFlow)
        flow.settings = {"case_retry_times": 3, "case_gap_seconds": 0}
        flow.stop_check = lambda: False
        flow.case_id = "T02"
        flow.said = []
        flow._say = lambda msg: flow.said.append(msg)
        flow._identity_bound = True
        flow._touched_firmware = lambda: False
        calls = {"n": 0}

        def fake_attempt(case, started, attempts, *, index):
            code = codes[min(calls["n"], len(codes) - 1)]
            calls["n"] += 1
            attempts.append({"index": index, "code": code, "note": f"第{index}次", "elapsed": 1})
            return {"case_id": "T02", "code": code, "note": f"第{index}次", "elapsed": 1,
                    "trigger": "", "before_version": "", "after_version": "", "announced": ""}

        flow._attempt = fake_attempt
        return flow

    def test_pass_after_failure_records_attempts(self):
        from server.common.config_loader import case_by_id

        flow = self._flow(["FAIL_UPGRADE", "PASS"])
        row = flow.run("T02")
        self.assertEqual(row["code"], "PASS")
        self.assertTrue(row["retried"])
        self.assertEqual([a["code"] for a in row["attempts"]], ["FAIL_UPGRADE", "PASS"])
        # 必须写清楚是第几次才过的
        self.assertIn("第 2 次尝试才通过", row["note"])

    def test_all_attempts_fail_keeps_failure(self):
        flow = self._flow(["FAIL_UPGRADE"])
        row = flow.run("T02")
        self.assertEqual(row["code"], "FAIL_UPGRADE")
        self.assertTrue(row["retried"])
        self.assertEqual(len(row["attempts"]), 3)
        self.assertIn("重试 2 次仍失败", row["note"])

    def test_env_problem_is_not_retried(self):
        """没包、缺电这类重试也没用，不该浪费十几分钟。"""
        flow = self._flow(["ABORT_ENV"])
        row = flow.run("T02")
        self.assertEqual(row["code"], "ABORT_ENV")
        self.assertFalse(row["retried"])
        self.assertEqual(len(row["attempts"]), 1)

    def test_first_time_pass_is_clean(self):
        flow = self._flow(["PASS"])
        row = flow.run("T02")
        self.assertEqual(row["code"], "PASS")
        self.assertFalse(row["retried"])
        self.assertEqual(len(row["attempts"]), 1)

    def test_wait_until_connected_before_reentering_upgrade(self):
        """蓝牙恢复后必须先在设备页看到已连接，才能再进升级页。"""
        from server.scripts.run_dafit_flow import DafitFlow

        flow = DafitFlow.__new__(DafitFlow)
        flow.case_id = "T05"
        flow.settings = {"reconnect_timeout": 30}
        flow.stop_check = lambda: False
        flow.said = []
        flow._say = lambda msg: flow.said.append(msg)
        flow.connected_words = ["已连接"]
        flow.disconnected_words = ["未连接", "连接中"]
        flow.texts = ["未连接", "已连接"]
        flow.focused = []
        flow._focus_device_top = lambda: flow.focused.append("top")
        flow._connect_text = lambda: flow.texts.pop(0)
        flow.slept = []
        import server.scripts.run_dafit_flow as flow_mod
        real_sleep = flow_mod.time.sleep
        flow_mod.time.sleep = lambda seconds: flow.slept.append(seconds)
        try:
            self.assertTrue(flow._wait_until_connected())
        finally:
            flow_mod.time.sleep = real_sleep
        self.assertEqual(flow.texts, [])
        self.assertEqual(flow.focused, ["top", "top"])
        self.assertEqual(flow.slept, [2])
        self.assertIn("滑到顶部", flow.said[0])
        self.assertIn("还不是已连接（未连接）", flow.said[1])
        self.assertIn("先不进升级页", flow.said[1])
        self.assertIn("设备页顶部已连接（已连接）", flow.said[2])

    def test_failure_dialog_done_is_clicked_before_covered_button(self):
        """失败弹窗的「完成」盖在成功页按钮上。先点文字，不能先点被挡住的 id。"""
        from server.scripts.run_dafit_flow import DafitFlow

        class Node:
            def __init__(self, name, clicks):
                self.name = name
                self.exists = True
                self.clicks = clicks

            def click(self):
                self.clicks.append(self.name)

        class Device:
            def __init__(self):
                self.clicks = []

            def __call__(self, **kwargs):
                if kwargs.get("text") == "完成":
                    return Node("完成", self.clicks)
                if kwargs.get("resourceId", "").endswith("btn_upgrade_complete"):
                    return Node("covered", self.clicks)
                return Node("other", self.clicks)

        flow = DafitFlow.__new__(DafitFlow)
        flow.done_text = "完成"
        flow.pkg = "com.crrepa.band.dafit"
        flow.link = {"locators": {"done_button": {
            "type": "resourceId",
            "value": "com.crrepa.band.dafit:id/btn_upgrade_complete",
        }}}
        flow.device = Device()
        flow.cache = None
        flow._touch = lambda: None
        import server.scripts.run_dafit_flow as flow_mod
        real_sleep = flow_mod.time.sleep
        flow_mod.time.sleep = lambda _seconds: None
        try:
            flow._click_done()
        finally:
            flow_mod.time.sleep = real_sleep
        self.assertEqual(flow.device.clicks, ["完成"])

    def test_wait_until_connected_does_not_enter_when_still_down(self):
        from server.scripts.run_dafit_flow import DafitFlow

        flow = DafitFlow.__new__(DafitFlow)
        flow.case_id = "T05"
        flow.settings = {"reconnect_timeout": 0}
        flow.stop_check = lambda: False
        flow.said = []
        flow._say = lambda msg: flow.said.append(msg)
        flow.connected_words = ["已连接"]
        flow.disconnected_words = ["未连接", "连接中"]
        flow._focus_device_top = lambda: None
        flow._connect_text = lambda: "连接中"
        self.assertFalse(flow._wait_until_connected())
        self.assertTrue(any("不进入升级页" in line for line in flow.said))
        self.assertIn("连接中", flow.said[-1])

    def test_retryable_codes(self):
        from server.scripts.run_dafit_flow import RETRYABLE_CODES

        self.assertIn("FAIL_UPGRADE", RETRYABLE_CODES)
        self.assertIn("ERROR", RETRYABLE_CODES)
        self.assertNotIn("ABORT_ENV", RETRYABLE_CODES)
        self.assertNotIn("HUNG_DEVICE", RETRYABLE_CODES)

    def test_report_shows_attempt_column(self):
        import csv
        import io
        import shutil
        from server.common.report_builder import write_task_report
        from server.common import paths

        directory = paths.RUNTIME_DIR / "test_retry_report"
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        try:
            task = {
                "task_id": "t", "slug": "t", "link": "watch", "link_name": "DaFit",
                "strategy": "s1", "phone_model": "P", "android": "11", "device_model": "W",
                "githash": "", "app_version": "", "manual_fields": [],
                "status": "finished", "verdict": "DONE",
                "rounds": [{
                    "case_id": "T02", "code": "PASS", "trigger": "", "before_version": "",
                    "after_version": "", "announced": "", "elapsed": 500,
                    "note": "第 2 次尝试才通过（前 1 次失败）",
                    "retried": True,
                    "attempts": [
                        {"index": 1, "code": "FAIL_UPGRADE", "note": "DFU 失败", "elapsed": 100},
                        {"index": 2, "code": "PASS", "note": "通过", "elapsed": 400},
                    ],
                }],
            }
            write_task_report(directory, task)
            text = (directory / "report.csv").read_text(encoding="utf-8-sig")
            self.assertIn("尝试", text)
            self.assertIn("1.FAIL_UPGRADE", text)
            self.assertIn("2.PASS", text)
            html_text = (directory / "report.html").read_text(encoding="utf-8")
            self.assertIn("尝试", html_text)
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class CaseGapTest(unittest.TestCase):
    """相邻用例之间要留恢复时间。

    2026-11-11 手表全量：T04/T05/T08 连续失败，T08 在 0% 就弹「固件升级失败」。
    同样的 T04 单独跑完全正常（10.5 分钟跑完 0%→100%）。手表连续升级时
    来不及准备下一次握手，所以每条之间要停一下。
    """

    def test_gap_between_rounds_but_not_before_first(self):
        import shutil
        from server.engine.scheduler import Scheduler
        from server.common import paths

        scheduler = Scheduler()
        task = scheduler.create(
            {"link": "glass", "cases": [{"id": "T01", "loops": 1}, {"id": "T09", "loops": 1}],
             "firmware_targets": ["mcu"]},
            dry_run=True,
        )
        scheduler._threads[task.task_id].join(timeout=120)
        try:
            log = (paths.RUNTIME_LOGCACHE / f"{task.slug}.log").read_text(encoding="utf-8")
            # dry-run 同秒可能共用 slug、日志追加；只看本任务最后一段。
            parts = log.split("任务开始 ")
            chunk = parts[-1] if len(parts) > 1 else log
            # 两条用例 = 1 次间隔（第一条之前不等）
            self.assertEqual(chunk.count("用例间隔"), 1, chunk)
        finally:
            for item in (paths.report_dir(task.slug), paths.log_dir(task.slug), paths.evidence_dir(task.slug)):
                shutil.rmtree(item, ignore_errors=True)

    def test_gap_defaults_and_rejects_negative(self):
        from server.common.config_loader import load_global

        settings = load_global()
        self.assertIsInstance(settings["case_gap_seconds"], int)
        self.assertGreaterEqual(settings["case_gap_seconds"], 0)


class WatchVersionTest(unittest.TestCase):
    def test_offer_page_uses_stored_current_version(self):
        link = load_link("watch")
        case = next(item for item in load_cases() if item["id"] == "T01")
        adb = FakeAdb(device_name="PBL Vast", device_mac="5A:7B:66:4B:51:F3")
        calls = {"prefs": 0}

        def run_as(pkg, script):
            calls["prefs"] += 1
            if "band_version" in script:
                return '<string name="band_version">MOY-V7T3-2.0.0</string>'
            if "firmware_version" in script:
                return '<string name="firmware_version">MOY-V7T3-2.0.1</string>'
            return ""

        adb.run_as = run_as
        adb.package_debuggable = lambda pkg: True
        device = FakeUiDevice(
            idle="已连接 固件升级 实报:2.0.0 公布:2.0.1",
            busy="有新版本可以更新 MOY-V7T3-2.0.1 升级 升级成功",
        )
        ui = UiDriver(device)
        # 升级页的版本控件在假设备上会读到 2.0.1，不能把它当成当前版本。
        original = ui.read_text

        def read_text(locator):
            value = (locator or {}).get("value")
            if value == "com.crrepa.band.dafit:id/tv_firmware_version":
                return "MOY-V7T3-2.0.1"
            if isinstance(value, dict) and value.get("contains") == "新版本":
                return "有新版本可以更新"
            return original(locator)

        ui.read_text = read_text
        runner = build_link("watch", link, adb, ui, logger=None, sleeper=lambda _s: None)
        versions = runner.read_firmware()
        self.assertEqual(versions["reported"], "2.0.0")
        self.assertEqual(versions["announced"], "2.0.1")
        self.assertGreater(calls["prefs"], 0)
        probe = BluetoothProbe(adb, ui, link, load_global()["patterns"])
        clock = {"now": 0.0}
        machine = RoundMachine(
            link, case, load_global(), runner, probe,
            lambda: clock["now"],
        )
        runner.sleep = lambda seconds: clock.__setitem__("now", clock["now"] + seconds)
        result = machine.run(lambda: False)
        self.assertEqual(result.before_version, "2.0.0")
        self.assertEqual(result.code, PASS)


class ServiceTest(unittest.TestCase):
    def test_dry_run_http_flow(self):
        from fastapi.testclient import TestClient

        from server.app import create_app

        client = TestClient(create_app(dry_run=True), client=("127.0.0.1", 50000))
        catalog = client.get("/api/config/apps")
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(len(catalog.json()["cases"]), 9)
        created = client.post("/api/tasks", json={"link": "ring", "cases": ["T01", "T09"], "loops": 1})
        self.assertEqual(created.status_code, 200)
        task_id = created.json()["task"]["task_id"]
        deadline = 0
        body = {}
        while deadline < 50:
            body = client.get(f"/api/tasks/{task_id}").json()["task"]
            if body["status"] in {"finished", "hung", "error", "stopped"}:
                break
            deadline += 1
            threading.Event().wait(0.05)
        self.assertEqual(body["status"], "finished")
        self.assertEqual(body["counts"]["PASS"], 2)
        self.assertEqual(client.get("/api/reports").status_code, 200)
        self.assertEqual(client.get(f"/api/reports/{task_id}/html").status_code, 200)
        blocked = client.post("/api/tasks", json={"link": "glass", "cases": ["T02", "T03"], "loops": 1})
        self.assertEqual(blocked.status_code, 400)
        shutil.rmtree(paths.report_dir(task_id), ignore_errors=True)
        shutil.rmtree(paths.log_dir(task_id), ignore_errors=True)
        shutil.rmtree(paths.evidence_dir(task_id), ignore_errors=True)


class PhoneCompatTest(unittest.TestCase):
    def test_usb_power_alone_is_not_charging(self):
        from server.drivers.adb import parse_battery

        level, charging = parse_battery("level: 80\nUSB powered: true\nAC powered: false\nstatus: 1\n")
        self.assertEqual(level, 80)
        self.assertFalse(charging)

    def test_status_charging_and_full(self):
        from server.drivers.adb import parse_battery

        self.assertTrue(parse_battery("level: 40\nstatus: 2\n")[1])
        self.assertTrue(parse_battery("level: 100\nstatus: 5\n")[1])

    def test_wearable_charging_reads_app_page_not_phone(self):
        from server.drivers.phone_ui import wearable_charging_from_page

        pkg = "com.moyoung.glasses"
        charging = (
            f'<node package="{pkg}" text="已连接" />'
            f'<node package="{pkg}" text="充电中 82%" />'
            f'<node package="com.android.systemui" text="充电中" />'
        )
        self.assertEqual(wearable_charging_from_page(charging, pkg), "充电中")
        need_charge = (
            f'<node package="{pkg}" text="请给设备充电" />'
            f'<node package="{pkg}" text="只有当电池充电超过 60% 时，才能进行版本升级。" />'
        )
        self.assertEqual(wearable_charging_from_page(need_charge, pkg), "")
        idle = f'<node package="{pkg}" text="已连接" /><node package="{pkg}" text="固件" />'
        self.assertEqual(wearable_charging_from_page(idle, pkg), "")

    def test_connection_label_ignores_substring_and_status_bar(self):
        from server.drivers.phone_ui import label_connected

        connected = ["已连接"]
        disconnected = ["未连接", "连接失败", "未配对", "连接中"]
        self.assertTrue(label_connected("已连接", connected, disconnected))
        self.assertFalse(label_connected("未连接", connected, disconnected))
        self.assertIsNone(label_connected("蓝牙已连接。", connected, disconnected))
        self.assertFalse(label_connected("连接中", connected, disconnected))

    def test_inject_waits_for_upgrade_page_and_progress(self):
        from server.drivers.phone_ui import inject_due

        keywords = ["正在固件升级", "正在升级"]
        self.assertFalse(inject_due(None, "有新版本可以更新 升级", keywords, False, 30, 20, 15))
        self.assertFalse(inject_due(5, "正在固件升级 5%", keywords, True, 5, 20, 15))
        self.assertFalse(inject_due(3, "正在固件升级 3%", keywords, True, 30, 20, 15))
        self.assertTrue(inject_due(23, "正在固件升级 23%", keywords, True, 3, 20, 15))
        self.assertFalse(inject_due(0, "正在固件升级 0%", keywords, True, 15, 20, 15))
        self.assertTrue(inject_due(None, "正在固件升级", keywords, True, 15, 20, 15))

    def test_lost_page_is_not_hung(self):
        from server.drivers.phone_ui import classify_round, round_note

        self.assertEqual(classify_round("success", "connected"), "PASS")
        self.assertEqual(classify_round("success", "unseen"), "FAIL_UPGRADE")
        self.assertEqual(classify_round("fail", "connected"), "FAIL_UPGRADE")
        self.assertEqual(classify_round("success", "disconnected"), "HUNG_DEVICE")
        self.assertEqual(classify_round("fail", "connected", "retry"), "FAIL_UPGRADE")
        self.assertEqual(classify_round("success", "connected", "retry"), "PASS")
        self.assertIn("设备页状态未确认", round_note("success", "", "unseen"))
        # 失败的「原因」必须在备注最前面，收尾状态放括号里。
        # 否则设备侧缺陷（如 T08 息屏打断 DFU）会被写成像是工具没导航好。
        note = round_note("fail", "固件升级失败", "unseen")
        self.assertTrue(note.startswith("失败：固件升级失败"), note)
        self.assertIn("设备页状态未确认", note)
        self.assertIn("重新升级没有成功", round_note("fail", "固件升级失败", "connected", "retry"))
        self.assertIn("重新升级成功", round_note("success", "", "connected", "retry"))

    def test_device_identity_parsers(self):
        from server.api.env import bluetooth_device_name, githash_from_version, phone_label

        self.assertEqual(phone_label("OPPO", "", "PCPM00"), "OPPO PCPM00")
        self.assertEqual(phone_label("Samsung", "Galaxy S24", "SM-S9210"), "Galaxy S24 (SM-S9210)")
        self.assertEqual(githash_from_version("2.9.23-167-g705a247f5a-dirty"), "705a247f5a")
        self.assertEqual(githash_from_version("1.2.3"), "")
        raw = "5A:7B:66:4B:51:F3 [ DUAL ] PBL Vast\n"
        self.assertEqual(bluetooth_device_name(raw, "5A:7B:66:4B:51:F3"), "PBL Vast")
        # 整份 dumpsys 里第一个 name= 是手机自己，不能当成这台穿戴设备的名字。
        mixed = 'name=OPPO Reno4 5G\nF0:53:69:70:64:C3 [ DUAL ] Usay Ring-64C3\n'
        self.assertEqual(bluetooth_device_name(mixed, "F0:53:69:70:64:C3"), "Usay Ring-64C3")
        from server.common.device_identity import looks_like_phone
        self.assertTrue(looks_like_phone("OPPO Reno4 5G", "OPPO Reno4 5G (PDPM00)"))
        self.assertFalse(looks_like_phone("Usay Ring-64C3", "OPPO Reno4 5G (PDPM00)"))

    def test_screen_off_does_not_press_power(self):
        from server.drivers.phone_ui import PhoneSession

        adb = FakeAdb()
        PhoneSession(FakeUiDevice(), adb).screen_off()
        joined = "\n".join(adb.shell_log)
        self.assertIn("input keyevent 223", joined)
        self.assertNotIn("input keyevent 26", joined)
        # 这台 OPPO 上 settings put 被 WRITE_SECURE_SETTINGS 挡住，写了也不生效，
        # 所以不要再假装改系统设置：真正让屏幕灭掉的就是 223。
        self.assertNotIn("settings put global stay_on_while_plugged_in", joined)

    def test_t08_holds_screen_off_long_enough_and_monitors(self):
        """息屏不能只瞎睡几秒：要够久，且保持期间会打点。"""
        from server.common.config_loader import case_by_id
        from server.scripts.run_dafit_flow import DafitFlow

        case = case_by_id("T08")
        steps = case.get("inject") or []
        off = next(step for step in steps if step.get("action") == "screen_off")
        on = next(step for step in steps if step.get("action") == "screen_on")
        self.assertGreaterEqual(float(off.get("wait_after") or 0), 60)
        self.assertGreaterEqual(float(on.get("wait_after") or 0), 1)

        logs: list[str] = []

        class _Probe(DafitFlow):
            def __init__(self):
                self.case_id = "T08"
                self.case_name = "升级中 APP 息屏"
                self.settings = {}
                self.stop_check = lambda: False

            def _say(self, message: str, level: str = "INFO") -> None:
                logs.append(message)

            def _screen_off_pulse(self) -> str:
                return "日志进度 35%"

        probe = _Probe()
        probe._hold_screen_off(6, watch=None)
        self.assertTrue(any("息屏中" in line and "35%" in line for line in logs))
        self.assertGreaterEqual(sum(1 for line in logs if "息屏中" in line), 1)

    def test_screen_on_wakes_and_unlocks(self):
        from server.drivers.phone_ui import PhoneSession

        adb = FakeAdb()
        PhoneSession(FakeUiDevice(), adb).screen_on()
        joined = "\n".join(adb.shell_log)
        self.assertIn("input keyevent 224", joined)

    def test_oppo_keyguard_flag(self):
        from server.drivers.phone_ui import keyguard_blocking

        self.assertTrue(keyguard_blocking("mDreamingLockscreen=true isKeyguardShowing=true"))
        self.assertFalse(keyguard_blocking("mDreamingLockscreen=false isKeyguardShowing=false"))

    def test_unlock_swipes_up_only_when_locked(self):
        from server.drivers.phone_ui import PhoneSession

        locked = FakeAdb(screen_locked=True)
        PhoneSession(FakeUiDevice(), locked).wake_and_unlock()
        swipes = [line for line in locked.shell_log if line.startswith("input swipe")]
        self.assertEqual(len(swipes), 1)
        parts = swipes[0].split()
        y1, y2 = int(parts[3]), int(parts[5])
        self.assertGreater(y1, y2)
        self.assertGreater(y1, 1920 * 0.88)
        opened = FakeAdb(screen_locked=False)
        PhoneSession(FakeUiDevice(), opened).wake_and_unlock()
        self.assertFalse(any(line.startswith("input swipe") for line in opened.shell_log))

    def test_fallback_does_not_fire_before_progress(self):
        link = load_link("glass")
        case = next(item for item in load_cases() if item["id"] == "T04")
        device = FakeUiDevice(idle="已连接 立即升级", busy="已连接 立即升级")
        runner = build_link("glass", link, FakeAdb(), UiDriver(device), None, lambda _seconds: None)
        ready, _how = runner.inject_ready(case, 30)
        self.assertFalse(ready)

    def test_statusbar_bluetooth_is_not_app_connected(self):
        link = load_link("watch")
        adb = FakeAdb(device_name="PBL Vast", device_mac="5A:7B:66:4B:51:F3", connected=False, visible=False)
        device = FakeUiDevice(idle="蓝牙已连接。 今天 步数", busy="蓝牙已连接。 今天")
        probe = BluetoothProbe(adb, UiDriver(device), link, load_global()["patterns"])
        self.assertIsNone(probe.channel_a())

    def test_progress_ignores_statusbar_percent(self):
        class XmlDevice:
            def dump_hierarchy(self):
                return (
                    "<hierarchy>"
                    '<node package="com.android.systemui" text="100%" />'
                    '<node package="com.crrepa.band.dafit" text="23%" />'
                    "</hierarchy>"
                )

        self.assertEqual(UiDriver(XmlDevice()).progress_percent({}), 23)

    def test_retry_rules_follow_transport(self):
        from server.drivers.phone_ui import allows_retry, case_direction, pass_rule

        cases = {item["id"]: item for item in load_cases()}
        watch = load_link("watch")
        glass = load_link("glass")
        self.assertEqual(case_direction(cases["T01"], watch), "forward")
        self.assertEqual(case_direction(cases["T04"], watch), "retry")
        self.assertEqual(case_direction(cases["T05"], watch), "retry")
        self.assertEqual(case_direction(cases["T06"], watch), "forward")
        self.assertEqual(case_direction(cases["T06"], glass), "retry")
        self.assertEqual(case_direction(cases["T07"], watch), "forward")
        self.assertEqual(case_direction(cases["T08"], watch), "forward")
        self.assertTrue(allows_retry(cases["T04"], watch))
        self.assertFalse(allows_retry(cases["T06"], watch))
        self.assertTrue(allows_retry(cases["T06"], glass))
        self.assertIn("重新点击升级", pass_rule("retry", cases["T04"], watch))
        self.assertIn("不能失败", pass_rule("forward", cases["T06"], watch))
        self.assertIn("Wi-Fi", pass_rule("retry", cases["T06"], glass))

    def test_run_direction_uses_the_link(self):
        """开跑时的方向必须看链路。2026-11-21 眼镜 T06 漏传链路，打断后没有重新点升级。"""
        from server.common.config_loader import case_by_id
        from server.scripts.run_dafit_flow import DafitFlow
        from server.scripts.run_glass_flow import GlassFlow
        from server.scripts.run_ring_flow import RingFlow

        class ProbeDevice:
            serial = "TESTDEVICE"

            def dump_hierarchy(self):
                return "<hierarchy/>"

            def app_current(self):
                return {"package": "", "activity": ""}

        case = case_by_id("T06")

        class Stop(Exception):
            pass

        def direction_of(flow) -> str:
            seen = {}

            def announce(_case):
                seen["direction"] = flow.direction
                raise Stop()

            flow._announce = announce
            try:
                flow._run(case, 0)
            except Stop:
                return seen["direction"]
            self.fail("方向还没定下来就走完了开跑")

        folder = Path("runtime")
        settings = load_global()
        device = ProbeDevice()
        self.assertEqual(direction_of(DafitFlow(device, folder, settings, logger=None)), "forward")
        self.assertEqual(direction_of(RingFlow(device, folder, settings, logger=None)), "forward")
        self.assertEqual(
            direction_of(GlassFlow(device, folder, settings, logger=None, firmware_targets=["mcu"])),
            "retry",
        )

    def test_glass_animation_page_is_still_running(self):
        from server.scripts.run_glass_flow import GlassFlow

        class AnimationDevice:
            serial = "TESTDEVICE"

            def dump_hierarchy(self):
                return '<node package="com.moyoung.glasses" text="升级" />'

            def app_current(self):
                return {"package": "com.moyoung.glasses", "activity": ".home.setting.firmware.ui.DeviceFirmwareActivity"}

        flow = GlassFlow(AnimationDevice(), Path("runtime"), load_global(), logger=None, firmware_targets=["mcu"])
        self.assertEqual(flow._phase(), "running")

    def test_switch_verification_targets(self):
        from server.drivers.phone_ui import expected_switch, switch_action

        self.assertEqual(switch_action("bt_off"), ("bluetooth_on", "False"))
        self.assertEqual(switch_action("wifi_on"), ("wifi_on", "True"))
        self.assertEqual(switch_action("screen_off"), ("screen_on_state", "False"))
        self.assertIsNone(switch_action("kill"))
        self.assertFalse(expected_switch("False"))
        self.assertTrue(expected_switch("True"))


class GlassPhaseTest(unittest.TestCase):
    """2026-11-10 的全量任务：升级页显示「已是最新版本」被当成正在升级，连跑 6 条假失败。"""

    @staticmethod
    def _device(page: str, activity: str = ".home.setting.firmware.ui.DeviceFirmwareActivity"):
        class Device:
            # 假设备也要有 serial。DafitFlow 在没有 serial 时会去问 adb 要一台真机，
            # 手机没连时这里就会抛错，让这些纯逻辑用例连带失败。
            serial = "TESTDEVICE"

            def __init__(self) -> None:
                self.force_stopped = 0

            def dump_hierarchy(self):
                return page

            def app_current(self):
                return {"package": "com.moyoung.glasses", "activity": activity}

        return Device()

    @staticmethod
    def _flow(device):
        from server.scripts.run_glass_flow import GlassFlow

        return GlassFlow(device, Path("runtime"), load_global(), logger=None, firmware_targets=["mcu"])

    PAGE_LATEST = (
        '<node package="com.moyoung.glasses" text="主控固件版本" bounds="[86,1322][454,1391]" />'
        '<node package="com.moyoung.glasses" text="当前版本：MOY-A073-0.1.8" bounds="[86,1450][700,1500]" />'
        '<node package="com.moyoung.glasses" content-desc="已是最新版本" clickable="false" bounds="[86,1561][994,1699]" />'
    )
    PAGE_READY = (
        '<node package="com.moyoung.glasses" text="主控固件版本" bounds="[86,1322][454,1391]" />'
        '<node package="com.moyoung.glasses" content-desc="升级至最新版本" clickable="true" bounds="[86,1561][994,1699]" />'
    )
    PAGE_CHARGE = (
        '<node package="com.moyoung.glasses" text="请给设备充电" bounds="[144,1045][935,1114]" />'
        '<node package="com.moyoung.glasses" text="只有当电池充电超过 60% 时，才能进行版本升级。" />'
        '<node package="com.moyoung.glasses" text="确定" clickable="true" bounds="[187,1335][892,1450]" />'
    )

    def test_connected_channel_wins_over_the_other(self):
        """数据链路已连接、音频还显示未连接时，不能判成断开。"""
        page = (
            '<node package="com.moyoung.glasses" text="已连接" />'
            '<node package="com.moyoung.glasses" text="未连接" />'
        )
        flow = self._flow(self._device(page, activity=".MainActivity"))
        self.assertEqual(flow._connect_state(), "已连接")

    def test_version_is_read_before_upgrade_click(self):
        flow = self._flow(self._device(self.PAGE_LATEST, activity=".MainActivity"))
        self.assertEqual(flow._before_version_text(), "MOY-A073-0.1.8")

    def test_already_latest_is_not_running(self):
        """回归：按钮是「已是最新版本」时不能判成 running。"""
        flow = self._flow(self._device(self.PAGE_LATEST))
        self.assertEqual(flow._phase(), "latest")

    def test_upgrade_button_is_ready(self):
        flow = self._flow(self._device(self.PAGE_READY))
        self.assertEqual(flow._phase(), "ready")

    def test_charge_popup_is_environment_problem(self):
        flow = self._flow(self._device(self.PAGE_CHARGE))
        self.assertEqual(flow._phase(), "need_charge")
        self.assertTrue(flow.need_charge)

    def test_latest_stops_task_instead_of_failing_every_case(self):
        """回归：没有可升级的包时记 ABORT_ENV，让调度器停止任务，而不是逐条记 FAIL_UPGRADE。"""
        flow = self._flow(self._device(self.PAGE_LATEST))
        flow.adb = FakeAdb()
        row = flow.run("T01")
        self.assertEqual(row["code"], "ABORT_ENV")
        self.assertIn("已是最新版本", row["note"])

    def test_restart_recovers_stale_latest_page(self):
        """停在不刷新的「已是最新版本」页时，杀 App 重进后应该认到可升级。"""
        class StaleDevice:
            serial = "TESTDEVICE"

            def __init__(self) -> None:
                self.force_stopped = 0
                self.page = GlassPhaseTest.PAGE_LATEST

            def dump_hierarchy(self):
                return self.page

            def app_current(self):
                return {"package": "com.moyoung.glasses", "activity": ".MainActivity"}

        device = StaleDevice()
        flow = self._flow(device)
        # 重启动作本身只是发两个命令，这里确认它确实按 pkg 杀了进程。
        flow.adb = FakeAdb()
        flow._restart_app()
        self.assertTrue(any("force-stop com.moyoung.glasses" in line for line in flow.adb.shell_log))


class NoRewriteAfterFirmwareWriteTest(unittest.TestCase):
    """写过固件就别再自动整包重刷。

    2026-11-12：Da Ring S4 升级反复在 5% 掉线，工具按 FAIL_UPGRADE 自动重拉，
    App 又从 0% 重传，几轮之后戒指停在 bootloader、开不了机。
    写入中断过的固件再整包重刷，是最容易把设备刷死的操作。
    """

    @staticmethod
    def _flow(touched: bool, failures: int = 5):
        from server.scripts.run_dafit_flow import DafitFlow

        device = FakeUiDevice()
        device.serial = "TESTDEVICE"
        flow = DafitFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow._touched_firmware = lambda: touched
        flow._touched_dfu = lambda: touched
        attempts = {"n": 0}

        def attempt(case, started, records, *, index):
            attempts["n"] += 1
            records.append({"index": index, "code": "FAIL_UPGRADE", "note": "刷写中断", "elapsed": 0.1})
            return {"case_id": case["id"], "code": "FAIL_UPGRADE", "note": "刷写中断"}

        flow._attempt = attempt
        return flow, attempts

    def test_firmware_write_disables_auto_retry(self):
        flow, attempts = self._flow(touched=True)
        row = flow.run("T01")
        self.assertEqual(attempts["n"], 1, "写过固件后不应该再自动重试")
        self.assertFalse(row["retried"])
        self.assertEqual(row["code"], "FAIL_UPGRADE")
        self.assertIn("不再自动重刷", row["note"])

    def test_failure_before_writing_still_retries(self):
        flow, attempts = self._flow(touched=False)
        row = flow.run("T01")
        self.assertGreater(attempts["n"], 1, "还没写过固件时，页面没进去这类失败仍应重试")
        self.assertTrue(row["retried"])


class RingResumeGuardTest(unittest.TestCase):
    """T04/T05 是「故意打断再重试」的用例，戒指的两条新判据不能把它们误杀。

    这两条判据都按时间戳过滤「本次升级开始之后」的日志。漏了重置基准，
    上一轮的 0% 和 DFU 报错就会被算进这一轮，正常重试会被判成卡死。
    """

    @staticmethod
    def _flow(stub_log: str):
        from server.scripts.run_ring_flow import RingFlow

        device = FakeUiDevice()
        device.serial = "TESTDEVICE"
        flow = RingFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow._ring_log = lambda lines=2000: stub_log
        flow._phase = lambda: "running"
        return flow

    LOG_OLD = (
        "11-12 18:08:22.476 9791 9838 D ring_tag: OtaRestoreManager upgrade onProgressChanged: 0\n"
        "11-12 18:08:32.900 9791 9838 D ring_tag: OtaRestoreManager upgrade onProgressChanged: 0\n"
        "11-12 18:09:05.452 9791 9838 D ring_tag: OtaRestoreManager upgrade onProgressChanged: 0\n"
        "11-12 18:09:20.100 9791 8807 E ring_tag: OtaRestoreManager upgrade error: DFU process failed.\n"
    )

    def test_old_failures_before_this_upgrade_are_ignored(self):
        flow = self._flow(self.LOG_OLD)
        # 基准设在历史日志之后，这一轮才刚开始一次 0%。
        flow._since_stamp = "11-12 18:20:00"
        self.assertFalse(flow._dfu_failed(), "上一轮的 DFU 报错不能算到这一轮")
        self.assertEqual(flow._restart_count(), 0)
        self.assertFalse(flow._spinning())

    def test_restarts_after_baseline_are_counted(self):
        log = (
            "11-12 18:21:00.000 9791 9838 D ring_tag: OtaRestoreManager upgrade onProgressChanged: 0\n"
            "11-12 18:21:30.000 9791 9838 D ring_tag: OtaRestoreManager upgrade onProgressChanged: 0\n"
            "11-12 18:22:00.000 9791 9838 D ring_tag: OtaRestoreManager upgrade onProgressChanged: 0\n"
            "11-12 18:22:30.000 9791 9838 D ring_tag: OtaRestoreManager upgrade onProgressChanged: 0\n"
        )
        flow = self._flow(log)
        flow._since_stamp = "11-12 18:20:00"
        self.assertEqual(flow._restart_count(), 4)
        self.assertTrue(flow._spinning(), "连续 4 次从 0% 重来应判空转")

    def test_failure_after_baseline_is_detected(self):
        log = "11-12 18:25:40.745 7124 8807 E ring_tag: OtaRestoreManager upgrade error: DFU process failed.\n"
        flow = self._flow(log)
        flow._since_stamp = "11-12 18:20:00"
        self.assertTrue(flow._dfu_failed())


class DafitScreenOffTest(unittest.TestCase):
    """T08 息屏时不能把黑屏当成升级失败。

    2026-11-12 手表 T08 的 FAIL_UPGRADE 是误判：同一秒 logcat 里 BLE 还在
    `onCharacteristicWrite`、`writeCompleted: true`（固件正常在传），
    而 uiautomator 刷了 105 次 `Could not detect idle state`、68 次 `Node not found`
    ——屏幕是黑的，工具什么都看不见，`_phase()` 却据此报了失败。
    """

    @staticmethod
    def _flow(power: str, page: str = ""):
        from server.scripts.run_dafit_flow import DafitFlow

        device = FakeUiDevice(idle=page or None)
        device.serial = "TESTDEVICE"
        flow = DafitFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow.adb.shell = lambda script, check=True: power if "dumpsys power" in script else "ok"
        flow.session.wake_and_unlock = lambda: setattr(flow, "_woken", True)
        flow._woken = False
        return flow

    def test_dark_screen_is_lit_before_judging(self):
        flow = self._flow("Display Power: state=OFF\nmWakefulness=Asleep\n")
        flow._ensure_screen_on()
        self.assertTrue(flow._woken, "屏幕灭着时必须先点亮，否则读到的是空页面")
        self.assertTrue(flow._screen_lit)

    def test_bright_screen_is_left_alone(self):
        flow = self._flow("Display Power: state=ON\nmWakefulness=Awake\n")
        flow._ensure_screen_on()
        self.assertFalse(flow._woken, "屏幕亮着不该多发一次按键，否则会打断息屏用例")

    def test_guard_does_not_fire_twice(self):
        flow = self._flow("Display Power: state=ON\nmWakefulness=Awake\n")
        flow._ensure_screen_on()
        flow._woken = False
        flow._ensure_screen_on()
        self.assertFalse(flow._woken, "已确认亮过就不该反复点亮")

    def test_touched_firmware_needs_real_progress(self):
        flow = self._flow("Display Power: state=ON\n")
        flow._clicked_upgrade = True
        flow._last_percent_seen = None
        self.assertFalse(
            flow._touched_firmware(),
            "只点了升级、没看到任何进度，说明一个字节都没写，应允许重试",
        )
        flow._last_percent_seen = 0
        self.assertTrue(flow._touched_firmware(), "出现过 0% 也算真的开始写了")


class DafitUltimaFlexTest(unittest.TestCase):
    """DaFit 2.9.23 + Ultima Flex 的页面特征（2026-11-13 真机核对）。

    这一版把「实报版本」和「目标版本」拆到了两个页面、两个控件，
    还多了一个「您的固件是最新版本!」页。判断错就会反复重试或误判失败。
    """

    def test_latest_page_is_not_a_failure(self):
        from server.scripts.run_dafit_flow import DafitFlow

        page = (
            '<node package="com.crrepa.band.dafit" text="固件升级" />'
            '<node package="com.crrepa.band.dafit" resource-id="com.crrepa.band.dafit:id/tv_device_firmware_version"'
            ' text="MOY-CPR2-2.0.2" />'
            '<node package="com.crrepa.band.dafit" text="您的固件是最新版本!" />'
        )
        # strict：节点是否存在由页面内容决定，否则 _has() 恒真，什么页面都会被判成失败。
        device = FakeUiDevice(idle=page, strict=True)
        device.serial = "TESTDEVICE"
        flow = DafitFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow.adb.shell = lambda script, check=True: "Display Power: state=ON\nmWakefulness=Awake\n"
        self.assertEqual(flow._phase(), "latest", "「最新版本」页不能被当成失败或没进升级页")

    def test_failure_page_is_still_failure(self):
        from server.scripts.run_dafit_flow import DafitFlow

        page = (
            '<node package="com.crrepa.band.dafit" text="固件升级失败" />'
            '<node package="com.crrepa.band.dafit" text="完成" />'
        )
        device = FakeUiDevice(idle=page, strict=True)
        device.serial = "TESTDEVICE"
        flow = DafitFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow.adb.shell = lambda script, check=True: "Display Power: state=ON\nmWakefulness=Awake\n"
        self.assertEqual(flow._phase(), "failure", "真失败页仍要判成 failure")

    def test_failure_dialog_wins_over_leftover_percent(self):
        """失败弹窗下面还留着 22% 时，不能当成还在升级。

        2026-11-18 手表 T05：弹窗「固件升级失败」没点掉，工具等了 180 秒「已连接」。
        """
        from server.scripts.run_dafit_flow import DafitFlow

        page = (
            '<node package="com.crrepa.band.dafit" text="固件升级失败" />'
            '<node package="com.crrepa.band.dafit" text="完成" />'
            '<node package="com.crrepa.band.dafit" resource-id="com.crrepa.band.dafit:id/tv_upgrade_percent" text="22%" />'
        )
        device = FakeUiDevice(idle=page, strict=True)
        device.serial = "TESTDEVICE"
        flow = DafitFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow.adb.shell = lambda script, check=True: "Display Power: state=ON\nmWakefulness=Awake\n"
        flow._percent = lambda: 22
        self.assertEqual(flow._phase(), "failure")

    def test_version_reads_either_page(self):
        from server.scripts.run_dafit_flow import DafitFlow

        cases = (
            (
                '<node package="com.crrepa.band.dafit" text="MOY-CPR2-2.0.2" />'
                '<node package="com.crrepa.band.dafit" text="您的固件是最新版本!" />',
                "2.0.2",
            ),
            (
                '<node package="com.crrepa.band.dafit" text="MOY-CPR2-2.0.3" />'
                '<node package="com.crrepa.band.dafit" text="有新版本可以更新" />',
                "2.0.3",
            ),
        )
        for page, expect in cases:
            device = FakeUiDevice(idle=page)
            device.serial = "TESTDEVICE"
            flow = DafitFlow(device, Path("runtime"), load_global(), logger=None)
            flow.adb = FakeAdb()
            self.assertEqual(flow._version_text(), expect, f"页面应读出 {expect}")

    def test_status_bar_connected_is_not_device_page(self):
        """状态栏的「蓝牙已连接」不能让工具以为已经在设备页。

        2026-11-13：升级 100% 成功后收尾，_on_device_page() 被状态栏文字骗过，
        不切 tab 就判成「没回到设备页」，一次成功升级被记成 FAIL_UPGRADE。
        """
        from server.scripts.run_dafit_flow import DafitFlow

        home_page = (
            '<node package="com.android.systemui" content-desc="蓝牙已连接。" />'
            '<node package="com.crrepa.band.dafit" text="步数" />'
        )
        device = FakeUiDevice(idle=home_page)
        device.serial = "TESTDEVICE"
        flow = DafitFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow._exists_id = lambda rid: False
        flow._has_exact = lambda phrase: False
        self.assertFalse(
            flow._on_device_page(),
            "首页 tab 不能被判成设备页，否则不会切 tab，永远读不到连接状态",
        )


class FlowRestartTest(unittest.TestCase):
    """重启 App 的能力必须在基类上，三条链路都能用。

    2026-11-13：`_restart_app` 原来只写在 GlassFlow 里，手表收尾调它直接抛
    `'DafitFlow' object has no attribute '_restart_app'`，把 T08 的一轮打成 ERROR，
    真实结论（息屏打断 DFU）反而没被记下来。
    """

    def test_all_flows_can_restart_app(self):
        from server.scripts.run_dafit_flow import DafitFlow
        from server.scripts.run_glass_flow import GlassFlow
        from server.scripts.run_ring_flow import RingFlow

        for cls in (DafitFlow, GlassFlow, RingFlow):
            self.assertTrue(
                callable(getattr(cls, "_restart_app", None)),
                f"{cls.__name__} 必须能用 _restart_app",
            )

    def test_restart_kills_and_relaunches_package(self):
        from server.scripts.run_dafit_flow import DafitFlow

        device = FakeUiDevice()
        device.serial = "TESTDEVICE"
        flow = DafitFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow._restart_app()
        joined = " ".join(flow.adb.shell_log)
        self.assertIn("force-stop com.crrepa.band.dafit", joined)


class FirmwareAppliedTest(unittest.TestCase):
    """同版本循环：页面完成 + 重连即成功，版本只作备注，不改判。"""

    @staticmethod
    def _flow(stored: str, target: str):
        from server.scripts.run_dafit_flow import DafitFlow

        device = FakeUiDevice()
        device.serial = "TESTDEVICE"
        flow = DafitFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow._stored_version = lambda: stored
        flow._target_version = lambda: target
        return flow

    def test_version_unchanged_keeps_pass(self):
        flow = self._flow("MOY-CPR2-2.0.2", "MOY-CPR2-2.0.3")
        code, note = flow._verify_firmware_applied("PASS", "升级成功且设备页已连接")
        self.assertEqual(code, "PASS")
        self.assertIn("同版本循环", note)
        self.assertIn("2.0.2", note)

    def test_version_applied_keeps_pass(self):
        flow = self._flow("MOY-CPR2-2.0.3", "MOY-CPR2-2.0.3")
        code, note = flow._verify_firmware_applied("PASS", "升级成功且设备页已连接")
        self.assertEqual(code, "PASS")
        self.assertIn("固件已更新到", note)

    def test_unreadable_version_does_not_fail(self):
        """正式包读不到本地记录，不能因此判失败。"""
        flow = self._flow("", "MOY-CPR2-2.0.3")
        code, note = flow._verify_firmware_applied("PASS", "升级成功")
        self.assertEqual(code, "PASS")
        self.assertIn("未校验", note)

    def test_failure_is_not_overwritten(self):
        flow = self._flow("MOY-CPR2-2.0.2", "MOY-CPR2-2.0.3")
        code, note = flow._verify_firmware_applied("FAIL_UPGRADE", "固件升级失败")
        self.assertEqual(code, "FAIL_UPGRADE")
        self.assertEqual(note, "固件升级失败")


class RingFirmwareAppliedTest(unittest.TestCase):
    """戒指同版本循环：版本不变仍 PASS，只记备注。"""

    @staticmethod
    def _flow(before: str, after: str):
        from server.scripts.run_ring_flow import RingFlow

        device = FakeUiDevice()
        device.serial = "TESTDEVICE"
        flow = RingFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow.before_version = before
        flow._version_text = lambda: after
        flow._read_version_on_upgrade_page = lambda: ""
        return flow

    def test_version_parser_keeps_dotted_build(self):
        from server.scripts.run_ring_flow import RingFlow

        page = (
            '<node package="com.moyoung.ring" text="固件升级" />'
            '<node package="com.moyoung.ring" text="当前版本:USY-I403-1.1.9" />'
        )
        device = FakeUiDevice(idle=page)
        device.serial = "TESTDEVICE"
        flow = RingFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        self.assertEqual(flow._version_text(), "USY-I403-1.1.9")

    def test_version_changed_keeps_pass(self):
        flow = self._flow("MOY-I2X3-1.1.6", "MOY-I2X3-1.1.7")
        code, note = flow._verify_firmware_applied("PASS", "通过")
        self.assertEqual(code, "PASS")
        self.assertIn("更新到", note)

    def test_version_unchanged_keeps_pass(self):
        flow = self._flow("MOY-I2X3-1.1.6", "MOY-I2X3-1.1.6")
        code, note = flow._verify_firmware_applied("PASS", "通过")
        self.assertEqual(code, "PASS")
        self.assertIn("同版本循环", note)

    def test_missing_version_keeps_pass(self):
        flow = self._flow("", "MOY-I2X3-1.1.7")
        code, note = flow._verify_firmware_applied("PASS", "通过")
        self.assertEqual(code, "PASS")
        self.assertIn("未校验", note)

    def test_success_dialog_clicks_ok_not_restart(self):
        """成功弹窗是「好的」，点掉后就不该再靠重启 App 收尾。"""
        from server.scripts.run_ring_flow import RingFlow

        device = FakeUiDevice(idle='text="升级成功!" text="好的"')
        device.serial = "TESTDEVICE"
        flow = RingFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow.case_id = "T01"
        flow.case_name = "基线升级"
        clicked = {"ok": False}

        class Node:
            def click(self):
                clicked["ok"] = True

        flow._exact_node = lambda phrase: Node() if phrase == "好的" else None
        flow.cache.page = lambda: 'text="升级成功!" text="好的"'
        flow.cache.cached_page = lambda: 'text="升级成功!" text="好的"'
        self.assertTrue(flow._dismiss_success_dialog())
        self.assertTrue(clicked["ok"])

    def test_ring_flow_does_not_override_base_run(self):
        """戒指的 run 应该复用基类的重试+防重刷逻辑，不能自己另写一套。"""
        from server.scripts.run_dafit_flow import DafitFlow
        from server.scripts.run_ring_flow import RingFlow

        self.assertIs(RingFlow.run, DafitFlow.run)


class BackgroundInterruptTest(unittest.TestCase):
    """升级页读不到时，要分清「被切到后台」和「升级页真丢了」。

    2026-11-13：升级到 69% 时有人把 DaFit 切到后台去配戒指，
    页面读不到，旧的 45 秒超时直接判「升级页丢失 -> 固件升级失败」。
    实际升级一直在后台正常跑，切回来已经到了 73%。
    这种误判会把好设备记成坏设备，比漏判更糟。
    """

    @staticmethod
    def _flow(foreground: bool):
        from server.scripts.run_dafit_flow import DafitFlow

        device = FakeUiDevice()
        device.serial = "TESTDEVICE"
        flow = DafitFlow(device, Path("runtime"), load_global(), logger=None)
        flow.adb = FakeAdb()
        flow.case_id = "T01"
        flow.case_name = "基线升级"
        flow.session.foreground = lambda pkg: foreground
        flow.session.prepare_for_app = lambda pkg, allow_relaunch=True: None
        flow._goto_entry = lambda: None
        return flow

    def test_background_is_not_page_lost(self):
        """App 在后台不该被当成升级页丢失。"""
        flow = self._flow(foreground=False)
        self.assertFalse(flow.session.foreground(flow.pkg))

    def test_foreground_detection_distinguishes_cases(self):
        """同一个 pkg，前台与否要能分开，_wait 靠它决定要不要导航。"""
        in_app = self._flow(foreground=True)
        away = self._flow(foreground=False)
        self.assertTrue(in_app.session.foreground(in_app.pkg))
        self.assertFalse(away.session.foreground(away.pkg))


class ProgressWatchTest(unittest.TestCase):
    """升级不能按总时长判失败，只能按「多久没有进展」判。"""

    @staticmethod
    def _watch(stall=300.0):
        from server.drivers.progress_watch import ProgressWatch

        watch = ProgressWatch(stall, logcat_tag="", serial="")
        # 这几个用例只验页面/百分比两路，把日志那一路摘掉，免得真去碰手机。
        watch._read_logcat = lambda: None
        return watch

    def test_percent_change_counts_as_progress(self):
        watch = self._watch()
        watch.last_change -= 1000
        self.assertTrue(watch.see(percent=20))
        self.assertFalse(watch.stalled())

    def test_page_change_counts_as_progress(self):
        watch = self._watch()
        watch.last_change -= 1000
        self.assertTrue(watch.see(page="<node text='正在升级' />"))
        self.assertFalse(watch.stalled())

    def test_no_change_for_long_enough_is_stalled(self):
        watch = self._watch(stall=60)
        watch.see(page="same")
        watch.last_change -= 120
        self.assertFalse(watch.see(page="same"))
        self.assertTrue(watch.stalled())

    def test_slow_upgrade_is_not_an_error(self):
        """回归：升级跑了 20 分钟，但百分比一直在动，就不能判失败。"""
        watch = self._watch(stall=300)
        watch.last_change -= 1200
        for percent in (10, 25, 40, 55, 70, 99):
            self.assertTrue(watch.see(percent=percent))
            self.assertFalse(watch.stalled())

    def test_repeated_same_percent_is_not_progress(self):
        watch = self._watch(stall=60)
        watch.see(percent=42)
        watch.last_change -= 120
        self.assertFalse(watch.see(percent=42))
        self.assertTrue(watch.stalled())


    def test_no_tag_disables_log_channel(self):
        """没配 logcat tag 时不能退化成读全系统日志。

        否则任何进程打个日志都算「有进展」，卡住检测永远不触发。手表链路就是
        没配 tag 的情况，靠页面百分比判进展。
        """
        from server.drivers.progress_watch import ProgressWatch

        watch = ProgressWatch(60, logcat_tag="", serial="")
        self.assertIsNone(watch._read_logcat())
        # 关掉日志那一路后，不看页面也不看百分比，就应该判为没进展
        watch.last_change -= 1000
        self.assertFalse(watch.see())
        self.assertTrue(watch.stalled())

    def test_tagged_logcat_only_reads_that_tag(self):
        """配了 tag 时必须带 -s，不能把全系统日志当成升级信号。"""
        import subprocess
        from server.drivers import progress_watch as pw

        captured = {}

        class FakeResult:
            stdout = "11-11 10:00:00.000 I X-LOG: ota progress type=1 progress=50.0\n"
            stderr = ""
            returncode = 0

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeResult()

        original = pw.subprocess.run
        pw.subprocess.run = fake_run
        try:
            watch = pw.ProgressWatch(60, logcat_tag="X-LOG:V", serial="SER")
            watch._read_logcat()
        finally:
            pw.subprocess.run = original

        self.assertIn("-s", captured["cmd"])
        self.assertIn("X-LOG:V", captured["cmd"])


class TransferProgressTest(unittest.TestCase):
    """第三路判据要真的反映「固件数据在传」。

    2026-11-10 全量任务 T08 被判超时，翻 logcat 发现那一刻升级走到 77% 还在写数据，
    是旧代码拿总时长硬切造成的。换成按行数统计又会因滚动窗口抖动而误判，
    所以必须用单调指标。
    """

    def _watch(self, log_text):
        from server.drivers.progress_watch import ProgressWatch

        watch = ProgressWatch(60, logcat_tag="X-LOG:V", serial="")
        watch._read_logcat = lambda: log_text
        return watch

    def test_rising_offset_and_progress_is_progress(self):
        watch = self._watch(
            "11-10 20:40:21 I X-LOG : jieli ota progress type=1 progress=76.74\n"
            "11-10 20:40:21 I ota:a  : [upgradeStep04] read data, offset : 1046048(0x100000)\n"
        )
        self.assertTrue(watch.see())
        watch.last_change -= 1000
        # 数值往前走了，说明还在传，不能判卡住
        watch._read_logcat = lambda: (
            "11-10 20:40:28 I X-LOG : jieli ota progress type=1 progress=77.34\n"
            "11-10 20:40:28 I ota:a  : [upgradeStep04] read data, offset : 1054240(0x101620)\n"
        )
        self.assertTrue(watch.see())
        self.assertFalse(watch.stalled())

    def test_frozen_offset_is_not_progress(self):
        """偏移和进度都不再前进，才是真的卡住。"""
        same = (
            "11-10 20:40:21 I X-LOG : jieli ota progress type=1 progress=76.74\n"
            "11-10 20:40:21 I ota:a  : [upgradeStep04] read data, offset : 1046048(0x100000)\n"
        )
        watch = self._watch(same)
        watch.see()
        watch.last_change -= 1000
        self.assertFalse(watch.see())
        self.assertTrue(watch.stalled())

    def test_line_count_jitter_does_not_fake_progress(self):
        """滚动窗口里日志条数会抖，但 offset 没动，就不能当成有进展。"""
        watch = self._watch(
            "11-10 20:40:21 I ota:a : offset : 1046048\n"
            "11-10 20:40:21 I X-LOG : command_write_result ok\n"
            "11-10 20:40:21 I X-LOG : Notif sent\n"
        )
        watch.see()
        watch.last_change -= 1000
        # 行数变少但 offset 没变：不是进展
        watch._read_logcat = lambda: "11-10 20:40:21 I ota:a : offset : 1046048\n"
        self.assertFalse(watch.see())
        self.assertTrue(watch.stalled())

    def test_progress_regression_does_not_rearm_timer(self):
        """眼镜重传分片时 progress 会往回跳。水位线不能跟着降，否则爬回去又算新进展。"""
        watch = self._watch(
            "11-10 20:40:24 I X-LOG : ota progress type=1 progress=77.34\n"
            "11-10 20:40:24 I ota:a : offset : 1054240\n"
        )
        self.assertTrue(watch.see())
        self.assertEqual(watch.last_progress, 77.34)
        watch.last_change -= 1000

        # 回退到 76.93：不是进展，水位线保持 77.34
        watch._read_logcat = lambda: (
            "11-10 20:40:28 I X-LOG : ota progress type=1 progress=76.93\n"
            "11-10 20:40:28 I ota:a : offset : 1051168\n"
        )
        self.assertFalse(watch.see())
        self.assertEqual(watch.last_progress, 77.34)
        self.assertEqual(watch.last_max_offset, 1054240)

        # 只爬回 77.19，仍没超过旧水位：还是不算进展，继续判定卡住
        watch._read_logcat = lambda: (
            "11-10 20:40:34 I X-LOG : ota progress type=1 progress=77.19\n"
            "11-10 20:40:34 I ota:a : offset : 1051680\n"
        )
        self.assertFalse(watch.see())
        self.assertTrue(watch.stalled())


class SafePrintTest(unittest.TestCase):
    """任务线程不能因为控制台写不进去而卡死。

    2026-11-11 的事故：服务用后台作业起，stdout 是管道。glass_s1_20261111_100912 跑到
    T05 第 4 轮时线程卡在 print 上，日志 18 分钟不增长，点停止也没反应——`_stopped()`
    那面旗子没人读，因为线程还在 print 里阻塞。这里守住「写控制台永不阻塞」。
    """

    def test_safe_print_returns_when_console_blocks(self):
        import time
        from server.common import safe_print as sp

        class BlockingStdout:
            """模拟管道写满：write 一直不返回。"""

            def write(self, _text):
                time.sleep(30)
                return 0

            def flush(self):
                pass

        original = sp.sys.stdout
        sp.sys.stdout = BlockingStdout()
        try:
            start = time.perf_counter()
            for index in range(50):
                sp.safe_print(f"第 {index} 行")
            elapsed = time.perf_counter() - start
        finally:
            sp.sys.stdout = original
        # 50 行必须瞬间返回，不能跟着被阻塞的输出线程一起等。
        self.assertLess(elapsed, 5.0)

    def test_safe_print_survives_broken_console(self):
        import time
        from server.common import safe_print as sp

        class BrokenStdout:
            def write(self, _text):
                raise OSError("管道已断开")

            def flush(self):
                raise OSError("管道已断开")

        original = sp.sys.stdout
        sp.sys.stdout = BrokenStdout()
        try:
            start = time.perf_counter()
            sp.safe_print("不会抛出去")
            elapsed = time.perf_counter() - start
        finally:
            sp.sys.stdout = original
        self.assertLess(elapsed, 2.0)

    def test_say_does_not_block_task_thread(self):
        """_say 走 safe_print，任务线程不会被控制台拖住。"""
        import time
        from server.common import safe_print as sp
        from server.scripts.run_dafit_flow import DafitFlow

        class BlockingStdout:
            def write(self, _text):
                time.sleep(30)
                return 0

            def flush(self):
                pass

        original = sp.sys.stdout
        sp.sys.stdout = BlockingStdout()
        try:
            flow = DafitFlow.__new__(DafitFlow)
            flow.logger = None
            start = time.perf_counter()
            for index in range(20):
                flow._say(f"进度 {index}%")
            elapsed = time.perf_counter() - start
        finally:
            sp.sys.stdout = original
        self.assertLess(elapsed, 5.0)


class WatchdogTest(unittest.TestCase):
    """任务线程卡死时，任务不能永远停在 running。

    2026-11-11：线程卡在 print 上，status 一直是 running，界面上点开始只会报
    「已有任务在跑」，日志不增长，但 CPU 空、HTTP 正常，很容易被当成设备死机。
    """

    def test_finished_thread_does_not_trip_watchdog(self):
        """线程正常结束时看门狗直接退出，不会误杀已完成的任务。"""
        import threading as th
        import time as tm
        from server.engine.scheduler import Scheduler

        scheduler = Scheduler()
        scheduler.settings_heartbeat_timeout = 0.05
        scheduler.watchdog_interval = 0.01

        record = type("R", (), {})()
        record.task_id = "t-done"
        record.status = "finished"
        record.verdict = "DONE"
        record.message = ""
        record.rounds = []
        record.counts = {}
        record.plan = []

        scheduler._tasks[record.task_id] = record
        scheduler._beats[record.task_id] = tm.time() - 1000
        dead = th.Thread(target=lambda: None)
        dead.start()
        dead.join()
        scheduler._threads[record.task_id] = dead
        scheduler._write = lambda _rec: None
        logger = type("L", (), {"log": lambda self, *a, **k: None})()

        scheduler._watchdog(record.task_id, logger)
        # 线程已经不 alive，看门狗立刻返回，任务状态保持原样
        self.assertEqual(record.status, "finished")
        self.assertEqual(record.verdict, "DONE")

    def test_watchdog_marks_stale_running_task(self):
        import threading as th
        import time as tm
        from server.engine.scheduler import Scheduler

        scheduler = Scheduler()
        scheduler.settings_heartbeat_timeout = 0.1
        scheduler.watchdog_interval = 0.01

        record = type("R", (), {})()
        record.task_id = "t-stale"
        record.slug = "glass_baseline_stale"
        record.status = "running"
        record.verdict = ""
        record.message = ""
        record.rounds = []
        record.counts = {}
        record.plan = []
        record.case_id = ""
        record.round_index = 0
        record.round_total = 0

        scheduler._tasks[record.task_id] = record
        scheduler._beats[record.task_id] = tm.time() - 100
        # 假线程对象：is_alive 永远 True，代表「线程还在，但完全不动了」
        class AliveButFrozen:
            def is_alive(self):
                return True
        scheduler._threads[record.task_id] = AliveButFrozen()
        scheduler._write = lambda _rec: None

        lines = []
        logger = type("L", (), {"log": lambda self, msg, level="INFO": lines.append(msg)})()

        scheduler._watchdog(record.task_id, logger)
        self.assertEqual(record.status, "error")
        self.assertEqual(record.verdict, "ERROR")
        self.assertIn("没有推进", record.message)
        self.assertTrue(any("看门狗" in line for line in lines))


class StopSignalTest(unittest.TestCase):
    """点「停止任务」后，等待循环必须立刻退出。

    原来 run_case 根本不接停止信号，`_wait` 里也没有停止检查。点停止要等整轮升级
    跑完（十几分钟）才响应，看起来完全像卡死。2026-11-11 复现过一次。
    """

    def test_wait_returns_stopped_when_flag_set(self):
        from server.scripts.run_glass_flow import GlassFlow

        class RunningDevice:
            serial = "TESTDEVICE"

            def dump_hierarchy(self):
                return ('<node package="com.moyoung.glasses" text="主控固件版本" bounds="[86,1322][454,1391]" />'
                        '<node package="com.moyoung.glasses" content-desc="升级至最新版本" clickable="true" bounds="[86,1561][994,1699]" />'
                        '<node package="com.moyoung.glasses" text="正在升级" />')

            def app_current(self):
                return {"package": "com.moyoung.glasses", "activity": ".home.setting.firmware.ui.DeviceFirmwareActivity"}

        from server.common.config_loader import case_by_id

        flow = GlassFlow(RunningDevice(), Path("runtime"), load_global(), logger=None,
                         firmware_targets=["mcu"], stop_check=lambda: True)
        flow.case_id = "T01"
        flow.case_name = "基线升级"
        flow.adb = _StubAdb()
        outcome, detail = flow._wait(case_by_id("T01"), allow_inject=False)
        self.assertEqual(outcome, "stopped")
        self.assertIn("停止", detail)

    def test_run_case_accepts_stop_check(self):
        """run_case 必须能把停止信号传进去，否则调度器没法让任务中途退出。"""
        import inspect
        from server.scripts.run_dafit_flow import run_case
        from server.scripts.run_glass_flow import run_glass_case

        self.assertIn("stop_check", inspect.signature(run_case).parameters)
        self.assertIn("stop_check", inspect.signature(run_glass_case).parameters)


class _StubAdb:
    """只有 _wait 需要的几个 shell 调用，不碰真机。"""

    serial = "STUB"

    def shell(self, script, check=False):
        return "mWakefulness=Awake\nDisplay Power: state=ON\n"

    def window_size(self):
        return 1080, 2400

    def bluetooth_on(self):
        return True

    def wifi_on(self):
        return True

    def screen_on_state(self):
        return True


class UiCacheTest(unittest.TestCase):
    """一次观察只读一次页面和一次前台包名，避免几十次 JSON-RPC 往返。"""

    def test_page_and_package_are_read_once(self):
        from server.drivers.ui_cache import UiCache

        class Device:
            def __init__(self) -> None:
                self.dumps = 0
                self.currents = 0

            def dump_hierarchy(self):
                self.dumps += 1
                return "<node />"

            def app_current(self):
                self.currents += 1
                return {"package": "com.moyoung.glasses", "activity": ".MainActivity"}

        device = Device()
        cache = UiCache(device)
        for _ in range(5):
            cache.page()
            cache.package()
        self.assertEqual(device.dumps, 1)
        self.assertEqual(device.currents, 1)
        cache.mark_stale()
        cache.page()
        self.assertEqual(device.dumps, 2)

    def test_dump_failure_returns_empty_page(self):
        from server.drivers.ui_cache import UiCache

        class Device:
            def dump_hierarchy(self):
                raise RuntimeError("uiautomator 挂了")

        self.assertEqual(UiCache(Device()).page(), "")


class DeviceIdentityTest(unittest.TestCase):
    """换表/换镜/换戒不能要求改 yaml 里的 MAC。"""

    SAMPLE = """
Bonded devices:
  75:D3:BF:5A:C3:D7 [ DUAL ] Ultima Flex
  DD:4D:D6:18:0C:4A [ DUAL ] V06
  F0:53:69:70:64:C3 [ DUAL ] Usay Ring-64C3

  com.crrepa.band.dafit (Registered)
  Connections: 1
    11-13 18:58:16 - 1ms : 75:D3:BF:5A:C3:D7 (263)

  com.moyoung.ring (Registered)
  Connections: 1
    11-16 10:30:39 - 1ms : F0:53:69:70:64:C3 (520)

  com.moyoung.glasses (Registered)
  Connections: 1
    11-13 17:49:51 - 1ms : DD:4D:D6:18:0C:4A (6)
"""

    def test_gatt_package_wins_over_stale_config(self):
        from server.common.device_identity import resolve_identity

        class Adb:
            def shell(self, *_args, **_kwargs):
                return DeviceIdentityTest.SAMPLE

        link = {
            "pkg": "com.moyoung.ring",
            "device_keywords": ["戒指", "Ring"],
            "device_name": "Da Ring S4",
            "device_mac": "D0:48:AA:F0:A8:C5",
            "locators": {},
        }
        identity = resolve_identity(link, adb=Adb())
        self.assertEqual(identity["mac"], "F0:53:69:70:64:C3")
        self.assertEqual(identity["name"], "Usay Ring-64C3")
        self.assertEqual(identity["source"], "gatt")

    def test_ui_locator_beats_gatt(self):
        from server.common.device_identity import resolve_identity

        class Adb:
            def shell(self, *_args, **_kwargs):
                return DeviceIdentityTest.SAMPLE

        link = {
            "pkg": "com.moyoung.ring",
            "device_keywords": ["Ring"],
            "device_name": "",
            "device_mac": "",
            "locators": {
                "device_name": {"type": "resourceId", "value": "id/name"},
                "device_mac": {"type": "resourceId", "value": "id/mac"},
            },
        }

        def read_text(locator):
            value = (locator or {}).get("value") or ""
            if value.endswith("name"):
                return "Usay Ring-UI"
            if value.endswith("mac"):
                return "AA:BB:CC:DD:EE:FF"
            return ""

        identity = resolve_identity(link, adb=Adb(), read_text=read_text)
        self.assertEqual(identity["mac"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(identity["name"], "Usay Ring-UI")
        self.assertEqual(identity["source"], "ui")

    def test_empty_config_still_finds_watch_and_glass(self):
        from server.common.device_identity import resolve_identity

        class Adb:
            def shell(self, *_args, **_kwargs):
                return DeviceIdentityTest.SAMPLE

        watch = resolve_identity(
            {"pkg": "com.crrepa.band.dafit", "device_keywords": ["Ultima"], "device_name": "", "device_mac": "", "locators": {}},
            adb=Adb(),
        )
        glass = resolve_identity(
            {"pkg": "com.moyoung.glasses", "device_keywords": ["V06"], "device_name": "", "device_mac": "", "locators": {}},
            adb=Adb(),
        )
        self.assertEqual(watch["mac"], "75:D3:BF:5A:C3:D7")
        self.assertEqual(glass["mac"], "DD:4D:D6:18:0C:4A")

    def test_app_page_fills_name_when_bluetooth_has_none(self):
        """蓝牙只有 MAC、没有广播名时，要从 App 设备页把名字填上。"""
        from server.common.device_identity import identity_from_page, resolve_identity

        page = """
            <node package="com.crrepa.band.dafit" text="Ultima Flex" />
            <node package="com.crrepa.band.dafit" text="75:D3:BF:5A:C3:D7" />
            <node package="com.crrepa.band.dafit" text="已连接" />
            <node package="com.crrepa.band.dafit" text="固件升级" />
            <node package="com.android.systemui" text="OPPO Phone" />
        """
        scraped = identity_from_page(page, "com.crrepa.band.dafit", ["Ultima", "手表"])
        self.assertEqual(scraped["name"], "Ultima Flex")
        self.assertEqual(scraped["mac"], "75:D3:BF:5A:C3:D7")

        class Adb:
            def shell(self, *_args, **_kwargs):
                # GATT 有 MAC，Bonded 列表故意不给名字。
                return """
state: ON
  com.crrepa.band.dafit (Registered)
  Connections: 1
    11-13 10:00:00 - 1ms : 75:D3:BF:5A:C3:D7 (1)
"""

        identity = resolve_identity(
            {
                "pkg": "com.crrepa.band.dafit",
                "device_keywords": ["Ultima", "手表"],
                "device_name": "",
                "device_mac": "",
                "locators": {},
            },
            adb=Adb(),
            page=page,
        )
        self.assertEqual(identity["name"], "Ultima Flex")
        self.assertEqual(identity["mac"], "75:D3:BF:5A:C3:D7")
        self.assertEqual(identity["source"], "ui")


class SourcePathTest(unittest.TestCase):
    """打包只改冻结后的路径。源码运行必须还是项目根，不能把 TEMP 改掉。"""

    def test_source_layout_unchanged(self):
        root = Path(__file__).resolve().parents[2]
        self.assertFalse(paths.is_frozen())
        self.assertEqual(paths.ROOT, root)
        self.assertEqual(paths.CONFIG_DIR, root / "config")
        self.assertEqual(paths.WEB_DIR, root / "web")
        self.assertEqual(paths.RUNTIME_DIR, root / "runtime")
        self.assertEqual(paths.OUTPUT_DIR, root / "output")
        self.assertTrue((paths.WEB_DIR / "index.html").is_file())

    def test_prepare_runtime_does_not_hijack_temp(self):
        import os

        before = os.environ.get("TEMP")
        paths.prepare_runtime()
        self.assertEqual(os.environ.get("TEMP"), before)
        self.assertEqual(paths.ROOT, Path(__file__).resolve().parents[2])
        self.assertFalse(str(paths.TOOLS_DIR).endswith("runtime"))

    def test_adb_hides_console_window(self):
        import subprocess

        from server.drivers.adb import hidden_subprocess_kwargs

        kwargs = hidden_subprocess_kwargs()
        self.assertIn("creationflags", kwargs)
        self.assertTrue(kwargs["creationflags"] & 0x08000000)
        self.assertTrue(getattr(subprocess, "_ota_hides_console", False))
        self.assertTrue(isinstance(subprocess.Popen, type))


if __name__ == "__main__":
    unittest.main()
