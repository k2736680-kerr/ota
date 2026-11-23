"""把控制台输出和任务线程解耦。

任务日志要同时写文件和控制台。写文件没问题，写控制台有风险：stdout 一旦是管道
（被后台作业、日志采集器接走），管道缓冲区满了以后 `print` 会一直阻塞。

2026-11-11 就是这么挂的：服务用后台作业起，stdout 是管道。glass_s1_20261111_100912
跑到 T05 第 4 轮时线程停在打印那一步，日志 18 分钟不增长，网页上点停止也没反应——
`_stopped()` 那面旗子压根没人读，因为线程卡在 `print` 里出不来。当时 CPU 几乎为空，
HTTP 接口却正常，很容易被误判成「设备死机」。

所以这里用一个守护线程专门做输出：任务线程只往队列里放一行，放不进去就丢掉。
宁可控制台少几行，也不能让判定逻辑停住不返回。
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
from datetime import datetime
from pathlib import Path


_queue: "queue.Queue[str]" = queue.Queue(maxsize=2000)
_started = False
_lock = threading.Lock()
_HAS_STAMP = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]")
_live_path: Path | None = None


def _live_file() -> Path | None:
    """可选：OTA_LIVE_LOG 指向的行缓冲文件，用来避开管道攒批。"""
    global _live_path
    if _live_path is not None:
        return _live_path
    raw = (os.environ.get("OTA_LIVE_LOG") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    _live_path = path
    return path


def _drain() -> None:
    """后台输出线程。这里允许阻塞，它卡住不影响任何任务线程。"""
    while True:
        line = _queue.get()
        try:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except Exception:
                # 控制台没了（管道断开、窗口关闭）也不该让进程做什么异常动作。
                pass
            live = _live_file()
            if live is not None:
                try:
                    with live.open("a", encoding="utf-8", buffering=1) as handle:
                        handle.write(line + "\n")
                        handle.flush()
                except Exception:
                    pass
        finally:
            _queue.task_done()


def _ensure_started() -> None:
    global _started
    if _started:
        return
    with _lock:
        if _started:
            return
        thread = threading.Thread(target=_drain, name="console-writer", daemon=True)
        thread.start()
        _started = True


def safe_print(message: str) -> None:
    """写一行控制台输出，永不阻塞调用线程。

    每行自动带上 [HH:MM:SS]，方便用时间空档判断卡住/优化点。
    已有时间戳的行不再重复加。
    """
    _ensure_started()
    text = str(message)
    if text and not _HAS_STAMP.match(text.lstrip()):
        text = f"[{datetime.now().strftime('%H:%M:%S')}] {text}"
    try:
        _queue.put_nowait(text)
    except queue.Full:
        # 控制台跟不上就丢弃，任务线程优先。
        pass
    except Exception:
        pass
