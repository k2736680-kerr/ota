"""穿戴 OTA 的桌面窗口。

GlassesTest 的 exe 能稳住，是因为窗口就是 tkinter，打包进去的是标准库。
这里同样直接调调度和设备接口，不再套一层 WebView。
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from server.api.configs import catalog
from server.api.env import device_view, switch_check
from server.api.tasks import TaskApi
from server.common import paths
from server.engine.scheduler import Scheduler, list_report_slugs, report_file

_DONE = {"finished", "hung", "stopped", "error"}
_FONT = ("Microsoft YaHei UI", 10)
_FONT_B = ("Microsoft YaHei UI", 14, "bold")


class OtaApp(tk.Tk):
    def __init__(self, dry_run: bool = False) -> None:
        super().__init__()
        self.dry_run = dry_run
        self.title("穿戴 OTA")
        self.geometry("1280x840")
        self.minsize(1100, 720)
        self.option_add("*Font", _FONT)
        self.scheduler = Scheduler()
        self.tasks = TaskApi(self.scheduler, dry_run)
        self._links: list[dict] = []
        self._cases: list[dict] = []
        self._case_rows: list[dict] = []
        self._firmware_vars: list[tuple[str, tk.BooleanVar]] = []
        self._task_id = ""
        self._log_offset = 0
        self._polling = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self.refresh_device)
        self._load_reports()

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(8, 4))
        title = "穿戴 OTA" + ("  ·  dry-run" if self.dry_run else "")
        ttk.Label(top, text=title, font=_FONT_B).pack(anchor="w")
        self.path_label = ttk.Label(top, text=f"数据目录  {paths.ROOT}", foreground="#666")
        self.path_label.pack(anchor="w", fill="x")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=10, pady=4)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")

        setup = ttk.LabelFrame(left, text="任务")
        setup.pack(fill="x")
        grid = ttk.Frame(setup)
        grid.pack(fill="x", padx=8, pady=6)
        grid.columnconfigure(1, weight=1)
        ttk.Label(grid, text="App").grid(row=0, column=0, sticky="w")
        self.link_var = tk.StringVar()
        self.link_combo = ttk.Combobox(grid, textvariable=self.link_var, state="readonly")
        self.link_combo.grid(row=0, column=1, columnspan=3, sticky="we", padx=(6, 0))
        self.link_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_link())
        ttk.Label(grid, text="断开观察窗（秒）").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.drop_var = tk.StringVar(value="120")
        ttk.Entry(grid, textvariable=self.drop_var, width=8).grid(row=1, column=1, sticky="w", padx=(6, 16), pady=(6, 0))
        ttk.Label(grid, text="重连超时（秒）").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.timeout_var = tk.StringVar(value="180")
        ttk.Entry(grid, textvariable=self.timeout_var, width=8).grid(row=1, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        ident = ttk.LabelFrame(left, text="设备信息")
        ident.pack(fill="x", pady=(6, 0))
        self.phone_var = tk.StringVar()
        self.android_var = tk.StringVar()
        self.app_var = tk.StringVar()
        self.device_var = tk.StringVar()
        self.hash_var = tk.StringVar()
        fields = (
            ("手机型号", self.phone_var),
            ("系统版本", self.android_var),
            ("APP 版本", self.app_var),
            ("穿戴设备", self.device_var),
            ("githash", self.hash_var),
        )
        box = ttk.Frame(ident)
        box.pack(fill="x", padx=8, pady=6)
        box.columnconfigure(1, weight=1)
        box.columnconfigure(3, weight=1)
        for index, (label, var) in enumerate(fields):
            row_i = index // 2
            col = (index % 2) * 2
            if index == len(fields) - 1:
                row_i, col = 2, 0
            ttk.Label(box, text=label).grid(row=row_i, column=col, sticky="w", pady=2)
            span = 3 if index == len(fields) - 1 else 1
            ttk.Entry(box, textvariable=var).grid(
                row=row_i, column=col + 1, columnspan=span, sticky="we", padx=(4, 12), pady=2
            )

        self.firmware_frame = ttk.LabelFrame(left, text="固件模块")
        self.firmware_inner = ttk.Frame(self.firmware_frame)
        self.firmware_inner.pack(fill="x", padx=8, pady=6)

        case_box = ttk.LabelFrame(left, text="用例")
        case_box.pack(fill="both", expand=True, pady=(6, 0))
        ttk.Label(case_box, text="未勾选的不跑。次数只作用于勾上的那一条。", foreground="#666").pack(
            anchor="w", padx=8, pady=(4, 0)
        )
        self.case_holder = ttk.Frame(case_box)
        self.case_holder.pack(fill="both", expand=True, padx=8, pady=6)

        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=(6, 0))
        primary = ttk.Frame(actions)
        primary.pack(fill="x")
        extra = ttk.Frame(actions)
        extra.pack(fill="x", pady=(4, 0))
        for text, command in (
            ("开始任务", self.start_task),
            ("停止任务", self.stop_task),
            ("继续", self.resume_task),
            ("刷新设备", self.refresh_device),
        ):
            ttk.Button(primary, text=text, command=command).pack(side="left", padx=(0, 4))
        for text, command in (
            ("全选", lambda: self._set_checked(True)),
            ("取消全选", lambda: self._set_checked(False)),
            ("全量运行", self.run_all),
            ("蓝牙/Wi-Fi自检", self.switch_check),
            ("清空临时目录", self.cleanup),
        ):
            ttk.Button(extra, text=text, command=command).pack(side="left", padx=(0, 4))

        run = ttk.LabelFrame(right, text="运行")
        run.pack(fill="both", expand=True)
        self.progress_var = tk.StringVar(value="空闲")
        self.progress_label = ttk.Label(run, textvariable=self.progress_var, wraplength=420)
        self.progress_label.pack(anchor="w", fill="x", padx=8, pady=(6, 2))
        self.steps_var = tk.StringVar(value="")
        ttk.Label(run, textvariable=self.steps_var, foreground="#333").pack(anchor="w", padx=8)
        stats = ttk.Frame(run)
        stats.pack(fill="x", padx=8, pady=4)
        self.pass_var = tk.StringVar(value="成功 0")
        self.fail_var = tk.StringVar(value="失败 0")
        self.hung_var = tk.StringVar(value="死机 0")
        self.skip_var = tk.StringVar(value="跳过 0")
        for var in (self.pass_var, self.fail_var, self.hung_var, self.skip_var):
            ttk.Label(stats, textvariable=var).pack(side="left", padx=(0, 10))
        self.verdict_var = tk.StringVar(value="尚未开始")
        self.verdict = ttk.Label(run, textvariable=self.verdict_var, wraplength=420)
        self.verdict.pack(anchor="w", fill="x", padx=8, pady=(0, 4))
        self.log = scrolledtext.ScrolledText(run, height=16, font=("Consolas", 9), wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        reports = ttk.LabelFrame(right, text="报告")
        reports.pack(fill="both", expand=True, pady=(6, 0))
        self.report_list = tk.Listbox(reports, height=6)
        self.report_list.pack(fill="both", expand=True, padx=8, pady=6)
        row = ttk.Frame(reports)
        row.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(row, text="打开 HTML", command=lambda: self._open_report("report.html")).pack(side="left")
        ttk.Button(row, text="打开 CSV", command=lambda: self._open_report("report.csv")).pack(side="left", padx=4)
        ttk.Button(row, text="打开报告目录", command=self._open_report_dir).pack(side="left", padx=4)

        self.right = right
        self.bind("<Configure>", self._fit_wrap, add="+")
        self._load_catalog()

    def _fit_wrap(self, _event=None) -> None:
        width = max(220, self.right.winfo_width() - 28)
        self.progress_label.configure(wraplength=width)
        self.verdict.configure(wraplength=width)
        self.path_label.configure(wraplength=max(320, self.winfo_width() - 28))

    def _load_catalog(self) -> None:
        data = catalog()
        self._links = data["links"]
        self._cases = data["cases"]
        defaults = data.get("defaults") or {}
        self.drop_var.set(str(defaults.get("drop_window") or 120))
        self.timeout_var.set(str(defaults.get("reconnect_timeout") or 180))
        names = [item["name"] for item in self._links]
        self.link_combo["values"] = names
        if names and not self.link_var.get():
            self.link_var.set(names[0])
        self._render_firmware()
        self._render_cases()

    def _current_link(self) -> dict | None:
        name = self.link_var.get()
        for item in self._links:
            if item["name"] == name:
                return item
        return self._links[0] if self._links else None

    def _on_link(self) -> None:
        self._render_firmware()
        self._render_cases()
        self.refresh_device()

    def _render_firmware(self) -> None:
        for child in self.firmware_inner.winfo_children():
            child.destroy()
        self._firmware_vars = []
        link = self._current_link() or {}
        modules = link.get("firmware_modules") or []
        if not modules:
            self.firmware_frame.pack_forget()
            return
        self.firmware_frame.pack(fill="x", pady=(6, 0), before=self.case_holder.master)
        defaults = {str(item) for item in (link.get("firmware_targets") or [])}
        if not defaults and modules:
            defaults = {str(modules[0]["id"])}
        for module in modules:
            var = tk.BooleanVar(value=str(module["id"]) in defaults)
            self._firmware_vars.append((str(module["id"]), var))
            ttk.Checkbutton(self.firmware_inner, text=module.get("label") or module["id"], variable=var).pack(
                side="left", padx=(0, 8)
            )

    def _render_cases(self) -> None:
        for child in self.case_holder.winfo_children():
            child.destroy()
        self._case_rows = []
        for item in self._cases:
            row = ttk.Frame(self.case_holder)
            row.pack(fill="x", pady=2)
            row.columnconfigure(1, weight=1)
            checked = tk.BooleanVar(value=item.get("default_checked") is not False)
            loops = tk.StringVar(value="1")
            ttk.Checkbutton(row, variable=checked).grid(row=0, column=0, sticky="w")
            ttk.Label(row, text=f"{item['id']}  {item['name']}").grid(row=0, column=1, sticky="w", padx=(4, 8))
            tag = "可重试" if item.get("direction") == "可重试" else "必须成功"
            ttk.Label(row, text=tag, foreground="#666").grid(row=0, column=2, sticky="e", padx=(0, 8))
            ttk.Label(row, text="次数").grid(row=0, column=3, sticky="e")
            ttk.Entry(row, textvariable=loops, width=5).grid(row=0, column=4, sticky="e", padx=(4, 0))
            self._case_rows.append({
                "id": item["id"],
                "strategy": item.get("strategy") or "",
                "checked": checked,
                "loops": loops,
            })

    def _set_checked(self, value: bool, skip_s2: bool = False) -> None:
        for row in self._case_rows:
            if skip_s2 and row["strategy"] == "s2":
                row["checked"].set(False)
            else:
                row["checked"].set(value)

    def _selected_plan(self) -> list[dict]:
        plan = []
        for row in self._case_rows:
            if not row["checked"].get():
                continue
            try:
                loops = int(row["loops"].get() or "1")
            except ValueError:
                loops = 1
            plan.append({"id": row["id"], "loops": max(1, loops)})
        return plan

    def _firmware_targets(self) -> list[str]:
        return [item_id for item_id, var in self._firmware_vars if var.get()]

    def _payload(self) -> dict:
        payload = {
            "link": (self._current_link() or {}).get("id") or "",
            "cases": self._selected_plan(),
            "drop_window": int(self.drop_var.get() or "120"),
            "reconnect_timeout": int(self.timeout_var.get() or "180"),
            "manual": {
                "phone_model": self.phone_var.get().strip(),
                "android": self.android_var.get().strip(),
                "device_model": self.device_var.get().strip(),
                "githash": self.hash_var.get().strip(),
                "app_version": self.app_var.get().strip(),
            },
        }
        if self._firmware_vars:
            payload["firmware_targets"] = self._firmware_targets()
        return payload

    def _say(self, text: str, bad: bool = False) -> None:
        self.verdict_var.set(text)
        self.verdict.configure(foreground="#a33" if bad else "#163")

    def start_task(self) -> None:
        plan = self._selected_plan()
        if not plan:
            self._say("至少勾选一个用例", True)
            return
        if self._firmware_vars and not self._firmware_targets():
            self._say("至少选择一个固件模块", True)
            return
        try:
            payload = self._payload()
        except ValueError:
            self._say("断开观察窗和重连超时要填数字", True)
            return
        result = self.tasks.create(payload)
        if not result.get("ok"):
            self._say(result.get("message") or "启动失败", True)
            return
        self._task_id = result["task"]["task_id"]
        self._log_offset = 0
        self.log.delete("1.0", "end")
        self._say("任务已开始")
        self._polling = True
        self._poll()

    def run_all(self) -> None:
        self._set_checked(True, skip_s2=True)
        self.start_task()

    def stop_task(self) -> None:
        if not self._task_id:
            self._say("当前没有运行中的任务", True)
            return
        result = self.tasks.stop(self._task_id)
        self._say(result.get("message") or "已请求停止任务", not result.get("ok", True))

    def resume_task(self) -> None:
        if not self._task_id:
            self._say("没有可继续的任务", True)
            return
        result = self.tasks.resume(self._task_id)
        if not result.get("ok"):
            self._say(result.get("message") or "无法继续", True)
            return
        self._say("已继续任务")
        self._polling = True
        self._poll()

    def refresh_device(self) -> None:
        link_id = (self._current_link() or {}).get("id") or ""

        def work() -> None:
            data = device_view(self.dry_run, link_id)
            self.after(0, lambda: self._apply_device(data))

        threading.Thread(target=work, name="ota-device", daemon=True).start()

    def _apply_device(self, data: dict) -> None:
        devices = data.get("devices") or []
        if not data.get("ok") or not devices:
            self._say(data.get("message") or "没有读到手机", True)
            return
        item = devices[0]
        self.phone_var.set(item.get("phone_model") or item.get("model") or "")
        self.android_var.set(item.get("android") or "")
        self.app_var.set(item.get("app_version") or "")
        self.device_var.set(item.get("device_model") or "")
        self.hash_var.set(item.get("githash") or "")
        self._say(data.get("message") or f"已读取 {item.get('serial') or ''}")

    def switch_check(self) -> None:
        self._say("正在自检：会关开一次蓝牙和 Wi-Fi，并恢复原状…")

        def work() -> None:
            data = switch_check(self.dry_run)
            self.after(0, lambda: self._apply_switch(data))

        threading.Thread(target=work, name="ota-switch", daemon=True).start()

    def _apply_switch(self, data: dict) -> None:
        lines = [
            f"{item['name']}: {'OK' if item.get('ok') else '不可用'} {item.get('detail') or ''}"
            for item in data.get("checks") or []
        ]
        self._say(" · ".join(lines) or data.get("message") or "自检完成", not data.get("ok"))

    def cleanup(self) -> None:
        paths.ensure_runtime()
        for child in list(paths.RUNTIME_DIR.iterdir()):
            if child.is_dir():
                import shutil
                shutil.rmtree(child)
            else:
                child.unlink()
        paths.ensure_runtime()
        self._say("临时目录已清空并重建")

    def _poll(self) -> None:
        if not self._polling or not self._task_id:
            return
        status = self.tasks.status(self._task_id)
        task = status.get("task") or {}
        if status.get("ok"):
            self._render_task(task)
        logs = self.tasks.log(self._task_id, self._log_offset)
        if logs.get("ok"):
            self._log_offset = logs.get("offset") or self._log_offset
            lines = logs.get("lines") or []
            if lines:
                self.log.insert("end", "\n".join(lines) + "\n")
                self.log.see("end")
        if status.get("ok") and task.get("status") in _DONE:
            self._polling = False
            self._load_reports()
            self._say(f"任务结束：{task.get('verdict') or task.get('status')}", task.get("verdict") == "HUNG")
            return
        self.after(1000, self._poll)

    def _render_task(self, task: dict) -> None:
        counts = task.get("counts") or {}
        self.progress_var.set(
            f"{task.get('link_name') or ''} / {task.get('strategy') or ''} · "
            f"{task.get('case_id') or '-'} {task.get('case_name') or ''} "
            f"第 {task.get('case_round') or 0}/{task.get('case_loops') or 0} 次 · "
            f"总进度 {task.get('round_index') or 0}/{task.get('round_total') or 0} · {task.get('status') or ''}"
        )
        self.steps_var.set(f"步骤 {task.get('state') or '-'}")
        self.pass_var.set(f"成功 {counts.get('PASS') or 0}")
        self.fail_var.set(f"失败 {counts.get('FAIL_UPGRADE') or 0}")
        self.hung_var.set(f"死机 {counts.get('HUNG_DEVICE') or 0}")
        self.skip_var.set(f"跳过 {counts.get('SKIP') or 0}")
        bad = task.get("status") in {"hung", "error"} or task.get("verdict") == "HUNG"
        self._say(task.get("message") or task.get("verdict") or task.get("status") or "", bad)

    def _load_reports(self) -> None:
        self.report_list.delete(0, "end")
        for slug in list_report_slugs():
            self.report_list.insert("end", slug)

    def _selected_slug(self) -> str:
        selection = self.report_list.curselection()
        if not selection:
            return ""
        return self.report_list.get(selection[0])

    def _open_report(self, name: str) -> None:
        slug = self._selected_slug()
        if not slug:
            self._say("先在列表里选一份报告", True)
            return
        try:
            path = report_file(slug, name)
        except FileNotFoundError:
            self._say("报告文件不存在", True)
            return
        os.startfile(path)  # noqa: S606  用户点的就是这份本地报告

    def _open_report_dir(self) -> None:
        os.startfile(paths.OUTPUT_REPORTS)  # noqa: S606

    def _on_close(self) -> None:
        if self._task_id and self._polling:
            if not messagebox.askyesno("穿戴 OTA", "任务还在跑，关掉窗口会停掉它。确定退出？"):
                return
            self.tasks.stop(self._task_id)
        self.destroy()


def main(dry_run: bool = False) -> None:
    OtaApp(dry_run=dry_run).mainloop()
