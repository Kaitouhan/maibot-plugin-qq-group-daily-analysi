"""报告和每日投递记录；始终存放于宿主授予的数据目录。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .domain import Report


class Store:
    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "daily_analysis.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reports (
                    stream TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (stream, day));
                CREATE TABLE IF NOT EXISTS deliveries (
                    stream TEXT NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL,
                    PRIMARY KEY (stream, day));
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, report: Report) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO reports VALUES (?, ?, ?)",
                (report.stream_id, report.day, report.model_dump_json()),
            )

    def get(self, stream: str, day: str) -> Report | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM reports WHERE stream=? AND day=?", (stream, day)).fetchone()
        return Report.model_validate_json(row[0]) if row else None

    def history(self, stream: str) -> list[str]:
        with self.connect() as db:
            return [
                row[0]
                for row in db.execute("SELECT day FROM reports WHERE stream=? ORDER BY day DESC LIMIT 14", (stream,))
            ]

    def claim(self, stream: str, day: str) -> bool:
        """先占位再发送；进程重启后不盲目重发结果未知的报告。"""
        with self.connect() as db:
            result = db.execute("INSERT OR IGNORE INTO deliveries VALUES (?, ?, 'running')", (stream, day))
            return result.rowcount == 1

    def finish(self, stream: str, day: str, status: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE deliveries SET status=? WHERE stream=? AND day=?", (status, stream, day))

    def kv_get(self, key: str, default: object = None) -> object:
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_put(self, key: str, value: object) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
