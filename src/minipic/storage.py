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
from dataclasses import asdict, dataclass, field
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


# ----------------------------------------------------------- v0.1.3b prepare record

# Status lifecycle for prepare_records:
#   prepared  ──commit──→  committed   (terminal, never reused)
#   prepared  ──expire──→  expired     (terminal, lookup returns 410)
PREPARE_STATUS_PREPARED = "prepared"
PREPARE_STATUS_COMMITTED = "committed"
PREPARE_STATUS_EXPIRED = "expired"
PREPARE_TTL_MS = 24 * 3600 * 1000   # 24h, then the row is considered expired


@dataclass
class PrepareRecord:
    """One row in prepare_records. Created on POST /api/prepare, mutated on commit.

    The plan is the full preparation result (content[] draft, segment list,
    warnings). The payload is the original request body so the commit endpoint
    can re-derive the call without trusting any client-side state.
    """

    prepare_id: str
    payload: dict                   # raw request body (CreateBody.model_dump())
    plan: dict                      # plan dict with content / segments / warnings
    status: str = PREPARE_STATUS_PREPARED
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    expires_at: int = field(default_factory=lambda: int(time.time() * 1000) + PREPARE_TTL_MS)
    task_id: Optional[str] = None   # set when commit succeeds
    extra: dict = field(default_factory=dict)


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
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS prepare_records (
                prepare_id  TEXT PRIMARY KEY,
                payload     TEXT NOT NULL,
                plan        TEXT NOT NULL,
                status      TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL,
                task_id     TEXT,
                extra       TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_prepare_status ON prepare_records(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_prepare_expires ON prepare_records(expires_at)")
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
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS prepare_records (
                prepare_id  TEXT PRIMARY KEY,
                payload     TEXT NOT NULL,
                plan        TEXT NOT NULL,
                status      TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL,
                task_id     TEXT,
                extra       TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_prepare_status ON prepare_records(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_prepare_expires ON prepare_records(expires_at)")


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


# ========================================================== v0.1.3b prepare_records


def insert_prepare(rec: PrepareRecord) -> None:
    """Persist a new prepare record. Overwrites if prepare_id collides."""
    init_db()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO prepare_records "
            "(prepare_id, payload, plan, status, created_at, expires_at, task_id, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.prepare_id,
                json.dumps(rec.payload, ensure_ascii=False),
                json.dumps(rec.plan, ensure_ascii=False),
                rec.status,
                rec.created_at,
                rec.expires_at,
                rec.task_id,
                json.dumps(rec.extra, ensure_ascii=False),
            ),
        )


def get_prepare(prepare_id: str) -> Optional[PrepareRecord]:
    """Look up a prepare record by id. Returns None when missing."""
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM prepare_records WHERE prepare_id = ?", (prepare_id,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_prepare(row)


def list_prepares(
    *,
    status: Optional[str] = None,
    include_expired: bool = False,
    limit: int = 50,
) -> list[PrepareRecord]:
    """List prepare records, newest first. Optionally filter by status."""
    init_db()
    now = int(time.time() * 1000)
    clauses: list[str] = []
    params: list = []
    if status is not None:
        clauses.append("status = ?"); params.append(status)
    if not include_expired:
        clauses.append("(status != ? AND expires_at > ?)")
        params.append(PREPARE_STATUS_EXPIRED); params.append(now)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM prepare_records {where} ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [_row_to_prepare(r) for r in rows]


def update_prepare_status(
    prepare_id: str,
    status: str,
    *,
    task_id: Optional[str] = None,
) -> None:
    """Update the status (and optionally task_id) of a prepare record."""
    fields = ["status = ?"]
    values: list = [status]
    if task_id is not None:
        fields.append("task_id = ?")
        values.append(task_id)
    values.append(prepare_id)
    with _conn() as c:
        c.execute(
            f"UPDATE prepare_records SET {', '.join(fields)} WHERE prepare_id = ?",
            values,
        )


def delete_prepare(prepare_id: str) -> None:
    """Hard-delete a prepare record. No-op when not present."""
    with _conn() as c:
        c.execute("DELETE FROM prepare_records WHERE prepare_id = ?", (prepare_id,))


def cleanup_expired_prepares(now_ms: Optional[int] = None) -> int:
    """Mark prepared records whose expires_at <= now as expired.

    Returns the number of rows updated. Does NOT delete — the audit trail is
    valuable even after expiry. Lazy cleanup is fine: callers should re-check
    ``expires_at > now`` on lookup and surface a 410 Gone.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    with _conn() as c:
        cur = c.execute(
            "UPDATE prepare_records SET status = ? "
            "WHERE status = ? AND expires_at <= ?",
            (PREPARE_STATUS_EXPIRED, PREPARE_STATUS_PREPARED, now),
        )
    return cur.rowcount


def _row_to_prepare(row: sqlite3.Row) -> PrepareRecord:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        payload = {}
    try:
        plan = json.loads(row["plan"])
    except (TypeError, json.JSONDecodeError):
        plan = {}
    try:
        extra = json.loads(row["extra"]) if row["extra"] else {}
    except (TypeError, json.JSONDecodeError):
        extra = {}
    return PrepareRecord(
        prepare_id=row["prepare_id"],
        payload=payload,
        plan=plan,
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        task_id=row["task_id"],
        extra=extra,
    )
