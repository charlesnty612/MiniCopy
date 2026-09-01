"""Tests for minipic.storage — SQLite task ledger round-trips."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from minipic.storage import (
    DB_FILENAME,
    TaskRecord,
    db_path,
    get_task,
    init_db,
    insert_task,
    list_tasks,
    update_task,
)


# --------------------------------------------------------------------------- TaskRecord dataclass
class TestTaskRecord:
    def test_defaults(self) -> None:
        r = TaskRecord(task_id="t1", mode="r2v", prompt_excerpt="hello")
        assert r.task_id == "t1"
        assert r.mode == "r2v"
        assert r.prompt_excerpt == "hello"
        assert r.status == "submitted"
        assert r.submitted_at > 0
        assert r.last_query_at == 0
        assert r.output_path is None
        assert r.error is None
        assert r.extra == {}

    def test_all_fields(self) -> None:
        now = int(time.time() * 1000)
        r = TaskRecord(
            task_id="t2",
            mode="t2v",
            prompt_excerpt="a" * 100,
            status="Success",
            submitted_at=now,
            last_query_at=now + 1000,
            output_path="/path/to/video.mp4",
            error=None,
            extra={"duration": 10},
        )
        assert r.output_path == "/path/to/video.mp4"
        assert r.extra == {"duration": 10}


# --------------------------------------------------------------------------- init_db
class TestInitDb:
    def test_creates_table(self, storage_db: Path) -> None:
        init_db()
        conn = sqlite3.connect(str(storage_db))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()

    def test_idempotent(self, storage_db: Path) -> None:
        init_db()
        init_db()  # must not raise
        init_db()
        conn = sqlite3.connect(str(storage_db))
        try:
            count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_creates_status_index(self, storage_db: Path) -> None:
        init_db()
        conn = sqlite3.connect(str(storage_db))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            index_names = {r[0] for r in rows}
            assert any("status" in n.lower() for n in index_names)
        finally:
            conn.close()


# --------------------------------------------------------------------------- insert_task
class TestInsertTask:
    def test_insert_and_get_round_trip(self, storage_db: Path) -> None:
        init_db()
        r = TaskRecord(task_id="tid-1", mode="r2v", prompt_excerpt="A prompt.")
        insert_task(r)

        fetched = get_task("tid-1")
        assert fetched is not None
        assert fetched.task_id == "tid-1"
        assert fetched.mode == "r2v"
        assert fetched.prompt_excerpt == "A prompt."
        assert fetched.status == "submitted"

    def test_insert_twice_replaces(self, storage_db: Path) -> None:
        init_db()
        r1 = TaskRecord(task_id="tid-2", mode="t2v", prompt_excerpt="First.")
        r2 = TaskRecord(task_id="tid-2", mode="t2v", prompt_excerpt="Second.", status="Success")
        insert_task(r1)
        insert_task(r2)
        fetched = get_task("tid-2")
        assert fetched is not None
        assert fetched.prompt_excerpt == "Second."
        assert fetched.status == "Success"

    def test_insert_task_no_existing_db(self, storage_db: Path) -> None:
        # init_db is called lazily by insert_task
        r = TaskRecord(task_id="tid-new", mode="i2v", prompt_excerpt="Img prompt.")
        insert_task(r)
        fetched = get_task("tid-new")
        assert fetched is not None
        assert fetched.task_id == "tid-new"

    def test_extra_serialized_as_json(self, storage_db: Path) -> None:
        init_db()
        r = TaskRecord(
            task_id="tid-json",
            mode="r2v",
            prompt_excerpt="x",
            extra={"resolution": "2K", "ratio": "16:9"},
        )
        insert_task(r)
        conn = sqlite3.connect(str(storage_db))
        try:
            raw = conn.execute("SELECT extra FROM tasks WHERE task_id=?", ("tid-json",)).fetchone()[0]
            assert json.loads(raw) == {"resolution": "2K", "ratio": "16:9"}
        finally:
            conn.close()


# --------------------------------------------------------------------------- update_task
class TestUpdateTask:
    def test_update_status(self, storage_db: Path) -> None:
        init_db()
        r = TaskRecord(task_id="tid-upd", mode="t2v", prompt_excerpt="x")
        insert_task(r)
        update_task("tid-upd", status="Success")
        fetched = get_task("tid-upd")
        assert fetched is not None
        assert fetched.status == "Success"

    def test_update_multiple_fields(self, storage_db: Path) -> None:
        init_db()
        r = TaskRecord(task_id="tid-multi", mode="r2v", prompt_excerpt="y")
        insert_task(r)
        now = int(time.time() * 1000)
        update_task("tid-multi", status="Processing", last_query_at=now, output_path="/out.mp4")
        fetched = get_task("tid-multi")
        assert fetched is not None
        assert fetched.status == "Processing"
        assert fetched.last_query_at == now
        assert fetched.output_path == "/out.mp4"

    def test_update_error_field(self, storage_db: Path) -> None:
        init_db()
        r = TaskRecord(task_id="tid-err", mode="t2v", prompt_excerpt="z")
        insert_task(r)
        update_task("tid-err", error="Balance insufficient")
        fetched = get_task("tid-err")
        assert fetched is not None
        assert fetched.error == "Balance insufficient"

    def test_update_no_op_if_nothing_passed(self, storage_db: Path) -> None:
        init_db()
        r = TaskRecord(task_id="tid-noop", mode="i2v", prompt_excerpt="noop")
        insert_task(r)
        update_task("tid-noop")  # no fields
        fetched = get_task("tid-noop")
        assert fetched is not None
        assert fetched.status == "submitted"

    def test_update_nonexistent_is_no_op(self, storage_db: Path) -> None:
        init_db()
        update_task("nonexistent", status="Success")  # must not raise
        assert get_task("nonexistent") is None


# --------------------------------------------------------------------------- list_tasks
class TestListTasks:
    def test_empty_when_no_tasks(self, storage_db: Path) -> None:
        init_db()
        tasks = list_tasks()
        assert tasks == []

    def test_orders_by_submitted_at_desc(self, storage_db: Path) -> None:
        init_db()
        base = int(time.time() * 1000)
        for i in range(5):
            r = TaskRecord(task_id=f"tid-{i}", mode="t2v", prompt_excerpt=f"p{i}", submitted_at=base + i * 1000)
            insert_task(r)
        tasks = list_tasks()
        ids = [t.task_id for t in tasks]
        assert ids == ["tid-4", "tid-3", "tid-2", "tid-1", "tid-0"]

    def test_respects_limit(self, storage_db: Path) -> None:
        init_db()
        base = int(time.time() * 1000)
        for i in range(10):
            r = TaskRecord(task_id=f"tid-{i}", mode="t2v", prompt_excerpt=f"p{i}", submitted_at=base + i)
            insert_task(r)
        tasks = list_tasks(limit=3)
        assert len(tasks) == 3
        assert [t.task_id for t in tasks] == ["tid-9", "tid-8", "tid-7"]


# --------------------------------------------------------------------------- db_path
class TestDbPath:
    def test_db_filename_is_tasks_db(self) -> None:
        assert DB_FILENAME == "tasks.db"


# --------------------------------------------------------------------------- get_task
class TestGetTask:
    def test_returns_none_for_missing(self, storage_db: Path) -> None:
        init_db()
        assert get_task("nonexistent") is None
