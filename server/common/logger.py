"""执行日志双写：runtime/logcache 与当前任务的 output/logs。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


class TaskLogger:
    def __init__(self, cache_file: Path, archive_file: Path | None = None) -> None:
        self.cache_file = cache_file
        self.archive_file = archive_file
        self.lines: list[str] = []
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        if archive_file is not None:
            archive_file.parent.mkdir(parents=True, exist_ok=True)

    def bind_archive(self, archive_file: Path) -> None:
        self.archive_file = archive_file
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        if self.lines:
            archive_file.write_text("".join(self.lines), encoding="utf-8")

    def log(self, message: str, level: str = "INFO") -> str:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {level} {message}\n"
        self.lines.append(line)
        for path in (self.cache_file, self.archive_file):
            if path is None:
                continue
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        return line.rstrip("\n")

    def since(self, offset: int) -> tuple[list[str], int]:
        chunk = self.lines[offset:]
        return [item.rstrip("\n") for item in chunk], len(self.lines)
