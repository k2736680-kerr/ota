"""一键启动。

源码运行（python start.py）仍打开浏览器，方便改页面。
打成 exe 后双击弹出 tkinter 窗口，和 GlassesTest 一样，不另开浏览器。
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import traceback
import webbrowser

import uvicorn

from server.app import HOST, PORT, create_app
from server.common import paths


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) != 0


def _say(text: str) -> None:
    """窗口程序没有控制台时 stdout 是 None，print 会直接把启动打崩。"""
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(text + "\n")
        stream.flush()
    except Exception:
        pass


def _alert(text: str) -> None:
    """exe 没有黑窗口。失败用系统对话框，别用 input()，否则人看不见。"""
    if not paths.is_frozen() or "--serve-only" in sys.argv:
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, str(text), "穿戴 OTA", 0x10)
    except Exception:
        pass


def _serve() -> None:
    parser = argparse.ArgumentParser(description="穿戴 OTA 本地自动化控制台")
    parser.add_argument("--dry-run", action="store_true", help="使用假设备，不调用 adb")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true", help="源码运行时不打开浏览器")
    parser.add_argument("--serve-only", action="store_true", help="只起服务，不打开窗口或浏览器")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("只允许监听本机")
    paths.prepare_runtime()
    if paths.is_frozen() and not args.serve_only:
        from desktop.app import main as desktop_main

        desktop_main(dry_run=args.dry_run)
        return
    if not port_free(args.host, args.port):
        raise SystemExit(f"{args.host}:{args.port} 已被占用")
    app = create_app(dry_run=args.dry_run)
    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser and not args.serve_only:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    if paths.is_frozen():
        _say(f"数据目录 {paths.ROOT}")
    _say(f"控制台 {url}  dry-run={args.dry_run}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def main() -> None:
    try:
        _serve()
    except KeyboardInterrupt:
        return
    except SystemExit as exc:
        if isinstance(exc.code, str) and exc.code.strip():
            _say(exc.code)
            _alert(exc.code)
        elif paths.is_frozen() and exc.code not in (0, None):
            _alert("启动失败")
        if paths.is_frozen() and exc.code not in (0, None):
            raise SystemExit(exc.code if isinstance(exc.code, int) else 1)
        raise
    except Exception:
        traceback.print_exc()
        _alert(traceback.format_exc())
        if paths.is_frozen():
            raise SystemExit(1)
        raise


if __name__ == "__main__":
    # 冻结后如果有库走 spawn，没有这句会把 exe 反复拉起来。
    import multiprocessing

    multiprocessing.freeze_support()
    main()
