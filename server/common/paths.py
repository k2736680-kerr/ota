"""所有路径集中在这里。业务代码不得自行拼接目录。

源码运行时，读写都在项目根目录，和打 exe 之前一样。

打成单文件 exe 之后：
- 页面留在解压目录里，只读。
- 配置、报告、临时文件放在 exe 旁边的 WearableOTA_data。
  配置运行期不回写，但测试员会改 yaml，所以只在文件还不存在时种一份，不覆盖。
- adb 放在 WearableOTA_data/runtime_tools，不放进 runtime/。
  页面上的清空会把 runtime 整目录删掉，adb 放进去下次就启动不了。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "WearableOTA"
_ADB_FILES = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def resource_root() -> Path:
    """只读资源：源码是项目根，exe 是 PyInstaller 解压目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[2]


def data_root() -> Path:
    """可写数据：源码是项目根，exe 是 exe 同目录下的 WearableOTA_data。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent / f"{APP_NAME}_data"
    return Path(__file__).resolve().parents[2]


ROOT = data_root()
_RESOURCE = resource_root()
CONFIG_DIR = ROOT / "config"
CASES_DIR = CONFIG_DIR / "cases"
WEB_DIR = _RESOURCE / "web"
RUNTIME_DIR = ROOT / "runtime"
RUNTIME_SCRIPTS = RUNTIME_DIR / "scripts"
RUNTIME_SNAPSHOTS = RUNTIME_DIR / "snapshots"
RUNTIME_LOGCACHE = RUNTIME_DIR / "logcache"
RUNTIME_UIA_DUMP = RUNTIME_DIR / "uia_dump"
OUTPUT_DIR = ROOT / "output"
OUTPUT_REPORTS = OUTPUT_DIR / "reports"
OUTPUT_LOGS = OUTPUT_DIR / "logs"
OUTPUT_EVIDENCE = OUTPUT_DIR / "evidence"
TOOLS_DIR = ROOT / "runtime_tools"

RUNTIME_CHILDREN = (
    RUNTIME_SCRIPTS,
    RUNTIME_SNAPSHOTS,
    RUNTIME_LOGCACHE,
    RUNTIME_UIA_DUMP,
)

APP_FILES = {
    "glass": CONFIG_DIR / "app_glass.yaml",
    "ring": CONFIG_DIR / "app_ring.yaml",
    "watch": CONFIG_DIR / "app_watch.yaml",
}


def ensure_runtime() -> None:
    """runtime 被整目录删除后，下次启动自动重建。"""
    for path in RUNTIME_CHILDREN:
        path.mkdir(parents=True, exist_ok=True)


def ensure_output() -> None:
    for path in (OUTPUT_REPORTS, OUTPUT_LOGS, OUTPUT_EVIDENCE):
        path.mkdir(parents=True, exist_ok=True)


def _copy_missing(src: Path, dest: Path) -> None:
    """只补还不存在的文件。测试员改过的 yaml 不能被新 exe 覆盖。"""
    if not src.is_dir():
        return
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        target = dest / path.relative_to(src)
        if target.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _copy_if_needed(src: Path, dest: Path) -> None:
    if not src.is_file():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size == src.stat().st_size:
        return
    shutil.copy2(src, dest)


def prepare_runtime() -> None:
    """启动前把目录和自带 adb 准备好。

    源码运行不改 PATH、不改 TEMP，避免开发时抢到另一份 adb 或弄脏系统临时目录。
    exe 才把自带 adb 拷出来并放到 PATH 最前，同时把 TEMP 指到数据目录，
    避开和系统里其他 adb 抢 %TEMP% 里的锁。
    """
    ensure_runtime()
    ensure_output()
    if not is_frozen():
        return
    _copy_missing(_RESOURCE / "config", CONFIG_DIR)
    bundled = _RESOURCE / "bundled_tools"
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    for name in _ADB_FILES:
        _copy_if_needed(bundled / name, TOOLS_DIR / name)
    tools_text = str(TOOLS_DIR)
    parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    if tools_text not in parts:
        os.environ["PATH"] = tools_text + os.pathsep + os.environ.get("PATH", "")
    tmp = ROOT / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(tmp)
    os.environ["TMP"] = str(tmp)
    if not (TOOLS_DIR / "adb.exe").is_file():
        print("未找到自带 adb，将尝试使用系统 PATH 里的 adb")


def task_slug(link: str, strategy: str, stamp: str) -> str:
    return f"{link}_{strategy}_{stamp}"


def report_dir(slug: str) -> Path:
    return OUTPUT_REPORTS / slug


def log_dir(slug: str) -> Path:
    return OUTPUT_LOGS / slug


def evidence_dir(slug: str) -> Path:
    return OUTPUT_EVIDENCE / slug
