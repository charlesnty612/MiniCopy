"""SQLite-backed task ledger.

Persists every submitted task locally so we can resume polling after a crash
and avoid duplicate submissions. The DB lives in the user data dir
(per platform, via platformdirs), not inside the project.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from .config import user_data_path

log = logging.getLogger(__name__)

DB_FILENAME = "tasks.db"


def db_path() -> Path:
    return user_data_path(DB_FILENAME)


@dataclass
class TaskRecord:
    task_id: str
    mode: str                       # t2v / i2v / r2v
    prompt_excerpt: str             # first 100 chars
    status: str = "submitted"
    submitted_at: int = field(default_factory=lambda: int(time.time() * 1000))
    last_query_at: int = 0
    output_path: Optional[str] = None
    error: Optional[str] = None
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------- v0.1.4 upload sweep

UPLOAD_TTL_MS = 24 * 60 * 60 * 1000  # 24h — reference uploads purged after this


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=10.0)
    c.row_factory = sqlite3.Row
    try:
        # Inline schema bootstrap so update_task() works against a brand-new
        # DB file (init_db() is only called from insert_task / get_task /
        # list_tasks, and would recurse if invoked from here).
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id        TEXT PRIMARY KEY,
                mode           TEXT NOT NULL,
                prompt_excerpt TEXT NOT NULL,
                status         TEXT NOT NULL,
                submitted_at   INTEGER NOT NULL,
                last_query_at  INTEGER NOT NULL DEFAULT 0,
                output_path    TEXT,
                error          TEXT,
                extra          TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id        TEXT PRIMARY KEY,
                mode           TEXT NOT NULL,
                prompt_excerpt TEXT NOT NULL,
                status         TEXT NOT NULL,
                submitted_at   INTEGER NOT NULL,
                last_query_at  INTEGER NOT NULL DEFAULT 0,
                output_path    TEXT,
                error          TEXT,
                extra          TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")


def insert_task(rec: TaskRecord) -> None:
    init_db()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tasks "
            "(task_id, mode, prompt_excerpt, status, submitted_at, last_query_at, output_path, error, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.task_id, rec.mode, rec.prompt_excerpt, rec.status,
                rec.submitted_at, rec.last_query_at, rec.output_path, rec.error,
                json.dumps(rec.extra, ensure_ascii=False),
            ),
        )


def update_task(
    task_id: str,
    *,
    status: Optional[str] = None,
    last_query_at: Optional[int] = None,
    output_path: Optional[str] = None,
    error: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    fields: list[str] = []
    values: list = []
    if status is not None:
        fields.append("status = ?"); values.append(status)
    if last_query_at is not None:
        fields.append("last_query_at = ?"); values.append(last_query_at)
    if output_path is not None:
        fields.append("output_path = ?"); values.append(output_path)
    if error is not None:
        fields.append("error = ?"); values.append(error)
    if extra is not None:
        fields.append("extra = ?"); values.append(json.dumps(extra, ensure_ascii=False))
    if not fields:
        return
    values.append(task_id)
    with _conn() as c:
        c.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?", values)


def get_task(task_id: str) -> Optional[TaskRecord]:
    init_db()
    with _conn() as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    return _row_to_record(row)


def list_tasks(limit: int = 50) -> list[TaskRecord]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks ORDER BY submitted_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def _row_to_record(row: sqlite3.Row) -> TaskRecord:
    extra_raw = row["extra"] or "{}"
    try:
        extra = json.loads(extra_raw)
    except json.JSONDecodeError:
        extra = {}
    return TaskRecord(
        task_id=row["task_id"],
        mode=row["mode"],
        prompt_excerpt=row["prompt_excerpt"],
        status=row["status"],
        submitted_at=row["submitted_at"],
        last_query_at=row["last_query_at"],
        output_path=row["output_path"],
        error=row["error"],
        extra=extra,
    )


# ============================================================ v0.1.4 upload sweep


def cleanup_expired_uploads(
    user_data_dir: Path, now_ms: Optional[int] = None
) -> int:
    """Delete uploaded reference files older than ``UPLOAD_TTL_MS``.

    Walks ``<user_data_dir>/uploads/`` and unlinks every entry whose
    ``mtime`` is older than the cutoff. Skips ``.json`` siblings (used
    by ``media._upload_cache_path``). Missing dir is a no-op. Returns
    the number of files deleted.
    """
    uploads_dir = Path(user_data_dir) / "uploads"
    if not uploads_dir.is_dir():
        return 0
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff_ms = now - UPLOAD_TTL_MS
    deleted = 0
    for entry in uploads_dir.iterdir():
        if entry.suffix.lower() == ".json":
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        mtime_ms = int(stat.st_mtime * 1000)
        if mtime_ms < cutoff_ms:
            try:
                entry.unlink()
                deleted += 1
            except OSError:
                # File might be in use or already gone — skip silently.
                continue
    return deleted