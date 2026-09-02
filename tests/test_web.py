"""Tests for minipic.web — FastAPI app factory, /api/create, /api/tasks endpoints.

Strategy:
- Build the app with a pre-populated `Config` (no env vars, no files).
- Mock `MiniMaxClient` via monkeypatch so no network I/O happens.
- Use `fastapi.testclient.TestClient` for synchronous requests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from minipic.config import Config
from minipic.errors import (
    BalanceError,
    ConfigError,
    MiniPicError,
    TaskError,
)
from minipic.storage import TaskRecord, get_task
from minipic.web import (
    CreateBody,
    _detect_key_source,
    _record_to_dict,
    _submit_task,
    _validate_mode,
    create_app,
)


# --------------------------------------------------------------------------- shared fixtures
@pytest.fixture
def web_cfg(sample_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A valid Config pointing videos_dir at a tmp path (so downloads don't pollute).

    v0.2.0+: also writes the api_key to the isolated user config file so
    ``GET /api/config`` (which only reads the user config) reports
    ``has_key=True, source="user"``. Without this, the UI would see an
    unconfigured state on every test even though ``sample_config`` carries a
    key in memory.
    """
    cfg = sample_config
    cfg.videos_dir = str(tmp_path / "videos")
    # The conftest's autouse fixture already isolates _user_config_path to a
    # tmp path; reuse it so the web UI sees the same key as the in-memory cfg.
    import minipic.config as config_mod
    user_path = config_mod._user_config_path()
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text(
        json.dumps({"api_key": cfg.api_key, "base_url": cfg.base_url}),
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def web_client(web_cfg: Config) -> TestClient:
    """A TestClient wrapping a fresh app with a valid in-memory config.

    The DB is already redirected to tmp by the conftest's autouse fixture.
    """
    app = create_app(web_cfg)
    return TestClient(app)


class _FakeMiniMaxClient:
    """Async-context-manager-compatible stand-in for MiniMaxClient.

    Returns configurable AsyncMock methods so individual tests can override
    return values / side effects. Exposes `await_fake` for assertions.
    """

    def __init__(self) -> None:
        self.create_video_task = AsyncMock(return_value="tid-default")
        self.query_video_task = AsyncMock(
            return_value={"status": "Success", "content": [{"url": "https://cdn/v.mp4"}]}
        )
        self.upload_and_resolve = AsyncMock(return_value="https://cdn/uploaded")
        # Context-IR defaults: return an empty enhanced prompt so the IR
        # branch in _submit_task is exercised but doesn't actually rewrite
        # (used_context_ir stays False). Tests that want the rewrite path
        # override ``fetch_context_ir_prompt.return_value``.
        self.create_context_ir_task = AsyncMock(return_value="ir-tid-default")
        self.fetch_context_ir_prompt = AsyncMock(return_value="")

        async def _dl(url: str, dest: Path, **kwargs: Any) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake-mp4")
        self.download_video = AsyncMock(side_effect=_dl)

    async def __aenter__(self) -> "_FakeMiniMaxClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest.fixture
def fake_client_factory(monkeypatch: pytest.MonkeyPatch):
    """Factory that patches MiniMaxClient with a fake.

    Usage:
        fake = fake_client_factory()
        fake.create_video_task.return_value = "tid-1"
    """
    def _make() -> _FakeMiniMaxClient:
        fake = _FakeMiniMaxClient()
        # Patch the symbol that web.py imported
        monkeypatch.setattr("minipic.web.MiniMaxClient", lambda cfg: fake)
        return fake

    return _make


# --------------------------------------------------------------------------- create_app
class TestCreateApp:
    def test_returns_fastapi_instance(self, web_cfg: Config) -> None:
        from fastapi import FastAPI
        app = create_app(web_cfg)
        assert isinstance(app, FastAPI)

    def test_stores_cfg_in_state(self, web_cfg: Config) -> None:
        app = create_app(web_cfg)
        assert app.state.cfg is web_cfg

    def test_default_uses_load_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import FastAPI
        fake_cfg = Config(api_key="k", base_url="https://x")
        monkeypatch.setattr("minipic.web.load_config", lambda: fake_cfg)
        app = create_app()
        assert isinstance(app, FastAPI)
        assert app.state.cfg is fake_cfg

    def test_cors_middleware_added(self, web_cfg: Config) -> None:
        from starlette.middleware.cors import CORSMiddleware
        app = create_app(web_cfg)
        middleware_classes = [m.cls for m in app.user_middleware]
        assert CORSMiddleware in middleware_classes


# --------------------------------------------------------------------------- GET /
class TestRootRoute:
    def test_serves_index_html(self, web_client: TestClient) -> None:
        resp = web_client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# --------------------------------------------------------------------------- POST /api/create — auth
class TestApiCreateAuth:
    def test_503_when_no_api_key(self, tmp_path: Path) -> None:
        cfg = Config(api_key=None)
        cfg.videos_dir = str(tmp_path / "videos")
        app = create_app(cfg)
        client = TestClient(app)
        resp = client.post("/api/create", json={
            "mode": "t2v", "prompt": "hi", "ratio": "16:9", "resolution": "768P",
        })
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "API key" in detail or "api_key" in detail.lower()


# --------------------------------------------------------------------------- POST /api/create — mode validation
class TestApiCreateModeValidation:
    def test_t2v_rejects_adaptive_ratio(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "hi", "ratio": "adaptive", "resolution": "768P",
        })
        assert resp.status_code == 400
        assert "adaptive" in resp.json()["detail"].lower()

    def test_t2v_rejects_reference_media(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "hi", "ratio": "16:9", "resolution": "768P",
            "ref_images": [{"path": "/x.jpg", "role": "first_frame"}],
        })
        assert resp.status_code == 400
        assert "reference" in resp.json()["detail"].lower()

    def test_i2v_rejects_non_adaptive_ratio(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "i2v", "prompt": "hi", "ratio": "16:9", "resolution": "768P",
            "ref_images": [{"path": "/x.jpg", "role": "first_frame"}],
        })
        assert resp.status_code == 400

    def test_i2v_requires_exactly_one_image(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "i2v", "prompt": "hi", "resolution": "768P",
            "ref_images": [],
        })
        assert resp.status_code == 400

    def test_i2v_requires_first_frame_role(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "i2v", "prompt": "hi", "resolution": "768P",
            "ref_images": [{"path": "/x.jpg", "role": "reference_image"}],
        })
        assert resp.status_code == 400
        # i2v 角色检查已统一通过 validate_model_modes；H3 不接受 reference_image 作为 i2v 角色。
        assert "reference_image" in resp.json()["detail"]

    def test_i2v_rejects_reference_videos(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "i2v", "prompt": "hi", "resolution": "768P",
            "ref_images": [{"path": "/x.jpg", "role": "first_frame"}],
            "ref_videos": [{"path": "/v.mp4"}],
        })
        assert resp.status_code == 400

    def test_r2v_requires_at_least_one_reference(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "r2v", "prompt": "hi", "resolution": "768P",
        })
        assert resp.status_code == 400
        assert "reference" in resp.json()["detail"].lower()


# --------------------------------------------------------------------------- POST /api/create — happy path
class TestApiCreateHappyPath:
    def test_t2v_submits_task(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        fake = fake_client_factory()
        fake.create_video_task.return_value = "tid-t2v-1"

        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "a cat", "ratio": "16:9", "resolution": "768P",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "tid-t2v-1"
        assert data["status"] == "submitted"

    def test_invalid_mode_pattern_rejected_by_pydantic(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "bogus", "prompt": "x", "ratio": "16:9", "resolution": "768P",
        })
        assert resp.status_code == 422

    def test_invalid_resolution_rejected_by_pydantic(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9", "resolution": "4K",
        })
        assert resp.status_code == 422

    def test_duration_out_of_range_rejected_by_pydantic(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9", "duration": 100,
        })
        assert resp.status_code == 422


# --------------------------------------------------------------------------- POST /api/create — error paths
class TestApiCreateErrors:
    def test_task_error_returns_400(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        fake = fake_client_factory()
        fake.create_video_task.side_effect = TaskError("task ended in Failed: oops")
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9",
        })
        assert resp.status_code == 400
        assert "TaskError" in resp.json()["detail"]

    def test_minipic_error_returns_400(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        fake = fake_client_factory()
        fake.create_video_task.side_effect = BalanceError("[1008] no money")
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9",
        })
        assert resp.status_code == 400
        assert "BalanceError" in resp.json()["detail"]

    def test_unexpected_error_returns_500(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        fake = fake_client_factory()
        fake.create_video_task.side_effect = RuntimeError("boom")
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9",
        })
        assert resp.status_code == 500


# --------------------------------------------------------------------------- POST /api/create — wait=True flow
class TestApiCreateWaitFlow:
    def test_wait_true_polls_and_downloads(
        self, web_client: TestClient, web_cfg: Config, fake_client_factory
    ) -> None:
        fake = fake_client_factory()
        fake.create_video_task.return_value = "tid-wait-1"
        fake.query_video_task.return_value = {
            "status": "Success",
            "content": [{"url": "https://cdn/v.mp4"}],
        }

        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9", "wait": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "tid-wait-1"
        # Result file should be on disk
        result = Path(web_cfg.videos_dir) / "tid-wait-1.mp4"
        assert result.is_file()
        assert result.read_bytes() == b"fake-mp4"


# --------------------------------------------------------------------------- GET /api/tasks
class TestApiListTasks:
    def test_returns_empty_list(self, web_client: TestClient) -> None:
        resp = web_client.get("/api/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_inserted_tasks(self, web_client: TestClient) -> None:
        from minipic.storage import insert_task
        insert_task(TaskRecord(
            task_id="tid-list-1", mode="t2v", prompt_excerpt="p1",
            submitted_at=int(time.time() * 1000),
        ))
        resp = web_client.get("/api/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert any(t["task_id"] == "tid-list-1" for t in data)

    def test_list_refreshes_non_terminal_tasks(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """Bug 1 fix: GET /api/tasks now refreshes non-terminal rows in-place.

        Previously the list endpoint was a pure SQLite read; tasks stuck in
        'submitted' / 'Processing' never moved. After the fix the list
        triggers the same per-task refresh as /api/tasks/{id}, but only for
        non-terminal records (so we don't hammer MiniMax for completed jobs).
        """
        from minipic.storage import insert_task
        now = int(time.time() * 1000)
        insert_task(TaskRecord(
            task_id="tid-list-live", mode="t2v", prompt_excerpt="p",
            status="Processing", submitted_at=now,
        ))
        insert_task(TaskRecord(
            task_id="tid-list-done", mode="t2v", prompt_excerpt="p",
            status="Success", submitted_at=now - 1,
        ))
        fake = fake_client_factory()
        # Different statuses per task — verify the refresh path actually runs.
        async def _fake_query(tid: str) -> dict[str, Any]:
            if tid == "tid-list-live":
                return {"status": "Success", "content": []}
            raise AssertionError(f"unexpected refresh of terminal task {tid}")
        fake.query_video_task.side_effect = _fake_query

        resp = web_client.get("/api/tasks")
        assert resp.status_code == 200
        data = resp.json()
        live = next(t for t in data if t["task_id"] == "tid-list-live")
        done = next(t for t in data if t["task_id"] == "tid-list-done")
        # Live: refreshed to Success; terminal: untouched.
        assert live["status"] == "Success"
        assert live["api_status"] == "Success"
        assert done["status"] == "Success"
        assert "api_status" not in done

    def test_list_swallows_refresh_errors(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """Refresh failure on the list endpoint must NOT 500 — graceful degrade."""
        from minipic.storage import insert_task
        insert_task(TaskRecord(
            task_id="tid-list-boom", mode="t2v", prompt_excerpt="p",
            status="Processing",
        ))
        fake = fake_client_factory()
        fake.query_video_task.side_effect = RuntimeError("net down")
        resp = web_client.get("/api/tasks")
        assert resp.status_code == 200
        # Local snapshot is still returned.
        assert any(t["task_id"] == "tid-list-boom" for t in resp.json())


# --------------------------------------------------------------------------- GET /api/tasks/{task_id}
class TestApiGetTask:
    def test_404_for_missing_task(self, web_client: TestClient) -> None:
        resp = web_client.get("/api/tasks/missing")
        assert resp.status_code == 404

    def test_returns_record_for_known_task(self, web_client: TestClient) -> None:
        from minipic.storage import insert_task
        insert_task(TaskRecord(
            task_id="tid-known", mode="r2v", prompt_excerpt="p", status="submitted",
        ))
        resp = web_client.get("/api/tasks/tid-known")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "tid-known"
        assert data["mode"] == "r2v"

    def test_refreshes_pending_task_from_api(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        from minipic.storage import insert_task
        # Use a non-terminal status (e.g. "Queued") so the impl will refresh.
        insert_task(TaskRecord(
            task_id="tid-refresh", mode="t2v", prompt_excerpt="p", status="Queued",
        ))
        fake = fake_client_factory()
        fake.query_video_task.return_value = {
            "status": "Success",
            "content": [{"url": "https://cdn/v.mp4"}],
        }
        resp = web_client.get("/api/tasks/tid-refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["api_status"] == "Success"
        assert data["content_url"] == "https://cdn/v.mp4"

    def test_terminal_status_does_not_refresh(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        from minipic.storage import insert_task
        insert_task(TaskRecord(
            task_id="tid-done", mode="t2v", prompt_excerpt="p", status="Success",
        ))
        fake = fake_client_factory()
        # The query should NOT be called
        resp = web_client.get("/api/tasks/tid-done")
        assert resp.status_code == 200
        assert fake.query_video_task.await_count == 0

    def test_api_error_during_refresh_is_swallowed(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        from minipic.storage import insert_task
        # Non-terminal status so the refresh branch runs
        insert_task(TaskRecord(
            task_id="tid-swallow", mode="t2v", prompt_excerpt="p", status="Queued",
        ))
        fake = fake_client_factory()
        fake.query_video_task.side_effect = BalanceError("no money")
        resp = web_client.get("/api/tasks/tid-swallow")
        assert resp.status_code == 200

    def test_submitted_status_refreshes_from_api(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """Bug 1 fix: 'submitted' is NOT terminal — must trigger API refresh.

        Previously the impl excluded 'submitted' from the refresh branch by
        mistake (it was in the 'terminal' skip-list). The page therefore
        always showed 'submitted' even when MiniMax had long moved the task
        on to Processing / Success.
        """
        from minipic.storage import insert_task, get_task
        insert_task(TaskRecord(
            task_id="tid-sub-refresh", mode="t2v", prompt_excerpt="p",
            status="submitted",
        ))
        fake = fake_client_factory()
        fake.query_video_task.return_value = {
            "status": "Processing",
            "content": [],
        }
        resp = web_client.get("/api/tasks/tid-sub-refresh")
        assert resp.status_code == 200
        data = resp.json()
        # Refreshed status is reflected in the response and persisted.
        assert data["api_status"] == "Processing"
        assert data["status"] == "Processing"
        rec = get_task("tid-sub-refresh")
        assert rec is not None
        assert rec.status == "Processing"

    def test_all_terminal_states_skip_refresh(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """Success / Failed / Cancelled / Expired → no MiniMax round-trip."""
        from minipic.storage import insert_task
        for terminal in ("Success", "Failed", "Cancelled", "Expired"):
            tid = f"tid-term-{terminal.lower()}"
            insert_task(TaskRecord(
                task_id=tid, mode="t2v", prompt_excerpt="p", status=terminal,
            ))
            fake = fake_client_factory()
            fake.query_video_task.return_value = {"status": "ShouldNotRun"}
            resp = web_client.get(f"/api/tasks/{tid}")
            assert resp.status_code == 200
            # query_video_task must NEVER be called for terminal records.
            assert fake.query_video_task.await_count == 0, terminal


# --------------------------------------------------------------------------- POST /api/tasks/{task_id}/download
class TestApiDownloadTask:
    def test_404_for_missing_task(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/tasks/missing/download")
        assert resp.status_code == 404

    def test_409_when_task_not_completed(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On-demand download: local file missing + API says not finished → 409."""
        from minipic.storage import insert_task
        insert_task(TaskRecord(
            task_id="tid-no-file", mode="t2v", prompt_excerpt="p", status="Processing",
            output_path=None,
        ))

        class _FakeClient:
            def __init__(self, cfg): self.cfg = cfg
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def query_video_task(self, tid):
                return {"status": "Processing"}

        import minipic.web as web_mod
        monkeypatch.setattr(web_mod, "MiniMaxClient", _FakeClient)
        resp = web_client.post("/api/tasks/tid-no-file/download")
        assert resp.status_code == 409

    def test_serves_mp4(
        self, web_client: TestClient, web_cfg: Config
    ) -> None:
        from minipic.storage import insert_task
        vdir = Path(web_cfg.videos_dir)
        vdir.mkdir(parents=True, exist_ok=True)
        target = vdir / "tid-dl.mp4"
        target.write_bytes(b"abc")
        insert_task(TaskRecord(
            task_id="tid-dl", mode="t2v", prompt_excerpt="p", status="Success",
            output_path=str(target),
        ))
        resp = web_client.post("/api/tasks/tid-dl/download")
        assert resp.status_code == 200
        assert resp.headers.get("content-type") == "video/mp4"
        assert resp.content == b"abc"

    def test_on_demand_download_fetches_from_api(
        self, web_client: TestClient, web_cfg: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local file missing + task succeeded → download from MiniMax on demand."""
        from minipic.storage import insert_task
        insert_task(TaskRecord(
            task_id="tid-ondemand", mode="t2v", prompt_excerpt="p", status="succeeded",
            output_path=None,
        ))

        class _FakeClient:
            def __init__(self, cfg): self.cfg = cfg
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def query_video_task(self, tid):
                return {"status": "succeeded", "content": {"url": "https://cdn/v.mp4"}}
            async def download_video(self, url, dest):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"ondemand-mp4")

        import minipic.web as web_mod
        monkeypatch.setattr(web_mod, "MiniMaxClient", _FakeClient)
        resp = web_client.post("/api/tasks/tid-ondemand/download")
        assert resp.status_code == 200
        assert resp.headers.get("content-type") == "video/mp4"
        assert resp.content == b"ondemand-mp4"
        # File should now exist locally for subsequent requests.
        assert (Path(web_cfg.videos_dir) / "tid-ondemand.mp4").is_file()


# --------------------------------------------------------------------------- CreateBody pydantic model
class TestCreateBody:
    def test_defaults(self) -> None:
        b = CreateBody(mode="t2v", prompt="x", ratio="16:9")
        assert b.duration == 10
        assert b.resolution == "768P"
        assert b.ratio == "16:9"
        assert b.model == "MiniMax-H3"
        assert b.ref_images == []
        assert b.ref_videos == []
        assert b.ref_audios == []
        assert b.wait is False

    def test_duration_min_max(self) -> None:
        with pytest.raises(Exception):
            CreateBody(mode="t2v", prompt="x", ratio="16:9", duration=3)
        with pytest.raises(Exception):
            CreateBody(mode="t2v", prompt="x", ratio="16:9", duration=16)

    def test_mode_pattern_enforced(self) -> None:
        with pytest.raises(Exception):
            CreateBody(mode="zzz", prompt="x", ratio="16:9")

    def test_resolution_pattern_enforced(self) -> None:
        with pytest.raises(Exception):
            CreateBody(mode="t2v", prompt="x", ratio="16:9", resolution="4K")

    def test_model_pattern_enforced(self) -> None:
        with pytest.raises(Exception):
            CreateBody(mode="t2v", prompt="x", ratio="16:9", model="MiniMax-X9")
        # Both real models accepted
        CreateBody(mode="t2v", prompt="x", ratio="16:9", model="MiniMax-H3")
        CreateBody(mode="t2v", prompt="x", ratio="16:9", model="MiniMax-H3-Max")


# --------------------------------------------------------------------------- _validate_mode
class TestValidateMode:
    def _body(self, **overrides: Any) -> CreateBody:
        defaults = {"mode": "t2v", "prompt": "p", "ratio": "16:9"}
        defaults.update(overrides)
        return CreateBody(**defaults)

    def test_t2v_adaptive_rejected(self) -> None:
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            _validate_mode(self._body(ratio="adaptive"))
        assert ei.value.status_code == 400

    def test_t2v_with_refs_rejected(self) -> None:
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_mode(self._body(ref_images=[{"path": "/a.jpg"}]))

    def test_i2v_non_adaptive_rejected(self) -> None:
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_mode(self._body(mode="i2v", ratio="16:9"))

    def test_i2v_no_image_rejected(self) -> None:
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_mode(self._body(mode="i2v"))

    def test_i2v_wrong_role_rejected(self) -> None:
        from fastapi import HTTPException
        body = self._body(
            mode="i2v",
            ratio=None,
            ref_images=[{"path": "/a.jpg", "role": "reference_image"}],
        )
        with pytest.raises(HTTPException) as ei:
            _validate_mode(body)
        # H3 不接受 reference_image 作为 i2v 角色（首帧/中间帧/尾帧 之外）
        assert "reference_image" in ei.value.detail

    def test_i2v_first_frame_ok(self) -> None:
        body = self._body(
            mode="i2v",
            ratio=None,  # i2v forces ratio=adaptive, so don't supply one
            ref_images=[{"path": "/a.jpg", "role": "first_frame"}],
        )
        _validate_mode(body)  # must not raise

    def test_r2v_no_refs_rejected(self) -> None:
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_mode(self._body(mode="r2v"))


# --------------------------------------------------------------------------- _submit_task
class TestSubmitTask:
    @pytest.mark.asyncio
    async def test_t2v_passes_prompt_and_ratio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = CreateBody(mode="t2v", prompt="hello world", ratio="16:9", duration=5)
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-1")

        task_id, content, _used_ir = await _submit_task(client, body)
        assert task_id == "tid-1"
        assert content[0] == {"type": "text", "text": body.prompt}
        # The call kwargs should include ratio + duration
        kwargs = client.create_video_task.await_args.kwargs
        assert kwargs["ratio"] == "16:9"
        assert kwargs["duration"] == 5
        assert kwargs["model"] == "MiniMax-H3"

    @pytest.mark.asyncio
    async def test_i2v_forces_adaptive_ratio(self) -> None:
        body = CreateBody(
            mode="i2v", prompt="x",
            ref_images=[{"path": "https://cdn/photo.jpg", "role": "first_frame"}],
        )
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-i2v")

        task_id, content, _used_ir = await _submit_task(client, body)
        assert task_id == "tid-i2v"
        kwargs = client.create_video_task.await_args.kwargs
        assert kwargs["ratio"] == "adaptive"

    @pytest.mark.asyncio
    async def test_image_url_passed_through_unchanged(self) -> None:
        body = CreateBody(
            mode="i2v", prompt="x",
            ref_images=[{"path": "https://example.com/photo.jpg",
                          "role": "first_frame"}],
        )
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-1")

        _, content, _used_ir = await _submit_task(client, body)
        img_items = [c for c in content if c.get("type") == "image_url"]
        assert len(img_items) == 1
        assert img_items[0]["image_url"]["url"] == "https://example.com/photo.jpg"
        assert img_items[0]["role"] == "first_frame"

    @pytest.mark.asyncio
    async def test_local_image_resolved_via_upload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        img = tmp_path / "local.jpg"
        img.write_bytes(b"x")

        body = CreateBody(
            mode="r2v", prompt="x",
            ref_images=[{"path": str(img), "role": "reference_image"}],
        )
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-1")

        # Patch resolve_reference so it doesn't need a real client
        async def fake_resolve(client: Any, source: Any) -> str:
            return "https://cdn/uploaded.jpg"
        monkeypatch.setattr("minipic.media.resolve_reference", fake_resolve)

        _, content, _used_ir = await _submit_task(client, body)
        img_items = [c for c in content if c.get("type") == "image_url"]
        assert len(img_items) == 1
        assert img_items[0]["image_url"]["url"] == "https://cdn/uploaded.jpg"

    @pytest.mark.asyncio
    async def test_audio_url_included(self) -> None:
        body = CreateBody(
            mode="r2v", prompt="x",
            ref_images=[{"path": "https://cdn/a.jpg", "role": "reference_image"}],
            ref_audios=[{"path": "https://cdn/sound.mp3", "role": "reference_audio"}],
        )
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-1")
        _, content, _used_ir = await _submit_task(client, body)
        audio_items = [c for c in content if c.get("type") == "audio_url"]
        assert len(audio_items) == 1
        assert audio_items[0]["audio_url"]["url"] == "https://cdn/sound.mp3"

    @pytest.mark.asyncio
    async def test_reference_video_emits_single_video_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single ref video (≤15s) becomes exactly one video_url content item.

        v0.1.5+: multi-segment generation was removed, so even a long-ish
        reference video at this stage emits only one content entry. Long
        references are rejected earlier by prepare_reference_video.
        """
        from minipic.media import VideoClip

        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        body = CreateBody(
            mode="r2v", prompt="x",
            ref_videos=[{"path": str(video)}],
        )

        # prepare_reference_video returns a single-element list
        async def fake_prep(source: Any) -> list[VideoClip]:
            return [VideoClip(path=source, start_seconds=0.0, duration_seconds=5.0, is_split=False)]
        monkeypatch.setattr("minipic.media.prepare_reference_video", fake_prep)

        async def fake_resolve(client: Any, source: Any) -> str:
            return f"https://cdn/{Path(source).name}"
        monkeypatch.setattr("minipic.media.resolve_reference", fake_resolve)

        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-1")

        _, content, _used_ir = await _submit_task(client, body)
        video_items = [c for c in content if c.get("type") == "video_url"]
        assert len(video_items) == 1
        # The clip carries its duration for H3's payload
        assert "duration" in video_items[0]
        assert video_items[0]["duration"] == 5.0

    @pytest.mark.asyncio
    async def test_long_ref_video_surfaces_media_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A >15s reference video raises MediaError that propagates to the caller."""
        from minipic.errors import MediaError
        from minipic.media import VideoClip

        video = tmp_path / "long.mp4"
        video.write_bytes(b"x")
        body = CreateBody(
            mode="r2v", prompt="x",
            ref_videos=[{"path": str(video)}],
        )

        async def fake_prep_long(source: Any) -> list[VideoClip]:
            raise MediaError(
                "参考视频 17.8s 超过 H3 上限 15s，多段生成已移除，"
                "请先用 ffmpeg 截取到 ≤15s 再重试"
            )
        monkeypatch.setattr("minipic.media.prepare_reference_video", fake_prep_long)

        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-1")

        with pytest.raises(MediaError, match="超过 H3 上限 15s"):
            await _submit_task(client, body)
        # Should not have been called since validation failed before submit
        assert client.create_video_task.await_count == 0

    @pytest.mark.asyncio
    async def test_long_ref_audio_surfaces_media_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A >15s reference audio raises MediaError that propagates to the caller.

        v0.1.6+ audio cap matches V2 docs: ≤15s per clip, no multi-segment.
        """
        from minipic.errors import MediaError
        from minipic.media import AudioClip

        audio = tmp_path / "long.mp3"
        audio.write_bytes(b"x")
        body = CreateBody(
            mode="r2v", prompt="x",
            ref_audios=[{"path": str(audio)}],
        )

        async def fake_prep_audio_long(source: Any) -> list[AudioClip]:
            raise MediaError(
                "音频参考 20.0s 超过 H3 上限 15s，请先用 ffmpeg 截取到 ≤15s 再重试"
            )
        monkeypatch.setattr("minipic.media.prepare_reference_audio", fake_prep_audio_long)

        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-1")

        with pytest.raises(MediaError, match="音频参考"):
            await _submit_task(client, body)
        assert client.create_video_task.await_count == 0

    @pytest.mark.asyncio
    async def test_short_ref_audio_passes_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ≤15s local audio becomes a single audio_url content item with duration."""
        from minipic.media import AudioClip

        audio = tmp_path / "ok.mp3"
        audio.write_bytes(b"x")
        body = CreateBody(
            mode="r2v", prompt="x",
            ref_audios=[{"path": str(audio)}],
        )

        async def fake_prep_audio(source: Any) -> list[AudioClip]:
            return [AudioClip(path=source, start_seconds=0.0, duration_seconds=8.0, is_split=False)]
        monkeypatch.setattr("minipic.media.prepare_reference_audio", fake_prep_audio)

        async def fake_resolve(client: Any, source: Any) -> str:
            return f"https://cdn/{Path(source).name}"
        monkeypatch.setattr("minipic.media.resolve_reference", fake_resolve)

        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-1")

        _, content, _used_ir = await _submit_task(client, body)
        audios = [c for c in content if c.get("type") == "audio_url"]
        assert len(audios) == 1
        assert audios[0]["duration"] == 8.0


# --------------------------------------------------------------------------- _submit_task — Context-IR wiring (Bug 2 fix)
class TestSubmitTaskContextIR:
    """Bug 2: H3-Context-IR was wired up in client.py but never called.

    Web submissions now optionally call create_context_ir_task +
    fetch_context_ir_prompt to rewrite the brief into the structured H3
    prompt before the real generation submit. Failures fall back silently.
    """

    @pytest.mark.asyncio
    async def test_h3_default_uses_context_ir(self) -> None:
        """use_context_ir=True (default) + H3 → IR rewrites content[0].text."""
        body = CreateBody(mode="t2v", prompt="a cat", ratio="16:9")
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-ir-1")
        # Snapshot what IR was called with BEFORE the swap happens.
        ir_content_seen: list[list[dict[str, Any]]] = []

        async def _ir_capture(**kw: Any) -> str:
            # Deep-copy so the later in-place mutation doesn't taint our snapshot.
            ir_content_seen.append([dict(item) for item in kw["content"]])
            return "ir-tid"
        client.create_context_ir_task = AsyncMock(side_effect=_ir_capture)
        client.fetch_context_ir_prompt = AsyncMock(
            return_value="[REWRITTEN 6-SECTION PROMPT]"
        )

        task_id, content, used_ir = await _submit_task(client, body)
        assert task_id == "tid-ir-1"
        assert used_ir is True
        assert content[0]["text"] == "[REWRITTEN 6-SECTION PROMPT]"
        # The IR task was created with the original prompt (snapshot proves it).
        assert len(ir_content_seen) == 1
        assert ir_content_seen[0][0]["text"] == "a cat"
        # And the final submit went through with the rewritten prompt.
        final_kw = client.create_video_task.await_args.kwargs
        assert final_kw["content"][0]["text"] == "[REWRITTEN 6-SECTION PROMPT]"

    @pytest.mark.asyncio
    async def test_use_context_ir_false_skips_ir(self) -> None:
        """use_context_ir=False → no IR calls, original prompt submitted."""
        body = CreateBody(mode="t2v", prompt="hello", ratio="16:9",
                          use_context_ir=False)
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-no-ir")
        client.create_context_ir_task = AsyncMock()
        client.fetch_context_ir_prompt = AsyncMock()

        _, content, used_ir = await _submit_task(client, body)
        assert used_ir is False
        assert content[0]["text"] == "hello"
        client.create_context_ir_task.assert_not_called()
        client.fetch_context_ir_prompt.assert_not_called()

    @pytest.mark.asyncio
    async def test_h3_max_skips_context_ir(self) -> None:
        """H3-Max is unsupported by IR — silently skip, do NOT raise."""
        body = CreateBody(
            mode="t2v", prompt="hi", ratio="16:9",
            model="MiniMax-H3-Max", resolution="768P", duration=5,
        )
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-max-no-ir")
        # Simulate the client's own ConfigError guard for H3-Max.
        async def _ir_create(**_kw: Any) -> str:
            raise ConfigError("unsupported model: 'MiniMax-H3-Max' (H3-Context-IR only supports MiniMax-H3)")
        client.create_context_ir_task = AsyncMock(side_effect=_ir_create)
        client.fetch_context_ir_prompt = AsyncMock()

        task_id, content, used_ir = await _submit_task(client, body)
        assert task_id == "tid-max-no-ir"
        assert used_ir is False
        # Original prompt is preserved, no IR fetches.
        assert content[0]["text"] == "hi"
        client.fetch_context_ir_prompt.assert_not_called()
        # Final submit went through with H3-Max.
        assert client.create_video_task.await_args.kwargs["model"] == "MiniMax-H3-Max"

    @pytest.mark.asyncio
    async def test_context_ir_failure_falls_back_silently(self) -> None:
        """Any IR exception (timeout / network / etc.) → fallback to original prompt."""
        body = CreateBody(mode="t2v", prompt="original", ratio="16:9")
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-fb")
        client.create_context_ir_task = AsyncMock(return_value="ir-tid")
        client.fetch_context_ir_prompt = AsyncMock(
            side_effect=TimeoutError("context-ir timed out")
        )

        # Must NOT raise — graceful degradation.
        task_id, content, used_ir = await _submit_task(client, body)
        assert task_id == "tid-fb"
        assert used_ir is False
        # Original prompt survives.
        assert content[0]["text"] == "original"
        # And the final submit still went through.
        assert client.create_video_task.await_args.kwargs["content"][0]["text"] == "original"

    @pytest.mark.asyncio
    async def test_context_ir_empty_response_keeps_original(self) -> None:
        """If the IR endpoint returns empty/None, leave the prompt alone."""
        body = CreateBody(mode="t2v", prompt="keep me", ratio="16:9")
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-empty")
        client.create_context_ir_task = AsyncMock(return_value="ir-tid")
        client.fetch_context_ir_prompt = AsyncMock(return_value="")

        _, content, used_ir = await _submit_task(client, body)
        assert used_ir is False
        assert content[0]["text"] == "keep me"


# --------------------------------------------------------------------------- /api/create — Context-IR end-to-end
class TestApiCreateContextIRPersisted:
    """The returned (task_id, content, used_context_ir) flag must reach the DB."""

    def test_used_context_ir_persisted_in_extra(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        fake = fake_client_factory()
        fake.create_video_task.return_value = "tid-ir-e2e"
        # IR succeeds with a real rewrite.
        fake.create_context_ir_task.return_value = "ir-tid-e2e"
        fake.fetch_context_ir_prompt.return_value = "[ENHANCED]"

        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "raw", "ratio": "16:9",
            "resolution": "768P",
        })
        assert resp.status_code == 200, resp.text

        # The submitted task's extra["used_context_ir"] is True.
        from minipic.storage import get_task
        rec = get_task("tid-ir-e2e")
        assert rec is not None
        assert rec.extra.get("used_context_ir") is True

    def test_use_context_ir_false_does_not_set_flag(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        fake = fake_client_factory()
        fake.create_video_task.return_value = "tid-no-ir-e2e"

        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "raw", "ratio": "16:9",
            "resolution": "768P", "use_context_ir": False,
        })
        assert resp.status_code == 200, resp.text
        from minipic.storage import get_task
        rec = get_task("tid-no-ir-e2e")
        assert rec is not None
        # No IR used → extra is empty (no used_context_ir key).
        assert "used_context_ir" not in rec.extra


# --------------------------------------------------------------------------- CreateBody pydantic — use_context_ir
class TestCreateBodyContextIR:
    def test_use_context_ir_default_true(self) -> None:
        b = CreateBody(mode="t2v", prompt="x", ratio="16:9")
        assert b.use_context_ir is True

    def test_use_context_ir_explicit_false(self) -> None:
        b = CreateBody(mode="t2v", prompt="x", ratio="16:9",
                       use_context_ir=False)
        assert b.use_context_ir is False


# --------------------------------------------------------------------------- _record_to_dict
class TestRecordToDict:
    def test_round_trip(self) -> None:
        rec = TaskRecord(
            task_id="t", mode="r2v", prompt_excerpt="p", status="submitted",
            submitted_at=123, output_path="/o.mp4", error="err", extra={"k": 1},
        )
        d = _record_to_dict(rec)
        assert d["task_id"] == "t"
        assert d["mode"] == "r2v"
        assert d["prompt_excerpt"] == "p"
        assert d["status"] == "submitted"
        assert d["submitted_at"] == 123
        assert d["output_path"] == "/o.mp4"
        assert d["error"] == "err"
        assert d["extra"] == {"k": 1}

    def test_no_output_or_error(self) -> None:
        rec = TaskRecord(task_id="t", mode="t2v", prompt_excerpt="p")
        d = _record_to_dict(rec)
        assert d["output_path"] is None
        assert d["error"] is None
        assert d["extra"] == {}


# --------------------------------------------------------------------------- GET /api/config + POST /api/config
class TestApiConfigEndpoints:
    def test_get_returns_masked_not_raw(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        resp = web_client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_key"] is True
        # sample_config fixture sets api_key="test-key-123" so mask is "...-123"
        assert data["masked"] == "...-123"
        # raw key must NOT be in the response
        assert "api_key" not in data
        # and the actual key prefix is also absent
        assert "test-key" not in json.dumps(data)

    def test_get_returns_model_constraints(self, web_client: TestClient) -> None:
        resp = web_client.get("/api/config")
        data = resp.json()
        names = [m["name"] for m in data["models"]]
        assert "MiniMax-H3" in names
        assert "MiniMax-H3-Max" in names
        # Spot-check constraints
        h3 = next(m for m in data["models"] if m["name"] == "MiniMax-H3")
        assert "768P" in h3["resolutions"]
        assert "2K" in h3["resolutions"]
        assert h3["duration_min"] == 4
        max_m = next(m for m in data["models"] if m["name"] == "MiniMax-H3-Max")
        assert "2K" not in max_m["resolutions"]
        assert max_m["duration_min"] == 5
        # v0.2.2+: server version is exposed to the UI for footer display
        assert data["version"] == "0.2.2"

    def test_get_handles_missing_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        cfg = Config(api_key=None)
        cfg.videos_dir = str(tmp_path / "videos")
        app = create_app(cfg)
        client = TestClient(app)
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_key"] is False
        assert data["masked"] == ""
        assert data["source"] == "none"

    def test_post_persists_and_updates_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Redirect the user config path so we don't pollute the real one
        target = tmp_path / "user_cfg" / "config.json"
        import minipic.config as config_mod
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: target)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

        cfg = Config(api_key="old-key")
        cfg.videos_dir = str(tmp_path / "videos")
        app = create_app(cfg)
        client = TestClient(app)

        resp = client.post("/api/config", json={"api_key": "new-key-1234"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        # "new-key-1234" is 13 chars; last 4 are "1234"; mask is "...1234"
        assert data["masked"] == "...1234"
        # File should be on disk
        assert target.is_file()
        on_disk = json.loads(target.read_text(encoding="utf-8"))
        assert on_disk["api_key"] == "new-key-1234"
        # The in-memory cfg should also be updated
        assert app.state.cfg.api_key == "new-key-1234"

    def test_post_empty_key_rejected(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/config", json={"api_key": ""})
        assert resp.status_code == 422  # pydantic min_length=1

    def test_post_whitespace_key_rejected(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/config", json={"api_key": "   "})
        assert resp.status_code == 400
        assert "empty" in resp.json()["detail"].lower()

    def test_post_then_create_uses_new_key(
        self, web_client: TestClient, web_cfg: Config,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Replace api_key to a different value via POST, then submit a task
        # and verify create still works (i.e. the new key took effect in memory).
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        # Redirect user config so we don't write to real user dir
        target = tmp_path / "u" / "config.json"
        import minipic.config as config_mod
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: target)

        from minipic import web as web_mod
        class _Noop:
            def __init__(self, c: Config) -> None:
                self.c = c
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def create_video_task(self, **kw): return "tid-after-rotate"
            async def query_video_task(self, tid):
                return {"status": "Processing"}
            async def download_video(self, url, dest):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"x")
        monkeypatch.setattr(web_mod, "MiniMaxClient", _Noop)

        resp = web_client.post("/api/config", json={"api_key": "rotated-1234abcd"})
        assert resp.status_code == 200, resp.text

        # Now submit a task with the (mocked) client
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9", "resolution": "768P",
        })
        assert resp.status_code == 200, resp.text

    def test_post_with_env_set_reports_user_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env set + POST /api/config writes user file → /api/config source is 'user'."""
        monkeypatch.setenv("MINIMAX_API_KEY", "from-env")
        target = tmp_path / "user_cfg_dir" / "config.json"
        import minipic.config as config_mod
        import minipic.web as web_mod
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: target)
        monkeypatch.setattr(web_mod, "_user_config_path", lambda: target)
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)

        cfg = Config(api_key="from-env")
        cfg.videos_dir = str(tmp_path / "videos")
        app = create_app(cfg)
        client = TestClient(app)

        # After build, /api/config should report 'user' once user file has the key
        resp = client.post("/api/config", json={"api_key": "new-user-key-9999"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["source"] == "user"
        # GET /api/config also reports user
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert resp.json()["source"] == "user"

    def test_first_launch_env_set_ui_unconfigured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.2.0+ acceptance: env set + no user config file → /api/config
        reports has_key=False, source='none'. The Web UI is the user-facing
        surface and only knows about keys saved through it; env/local keys
        belong to the CLI developer workflow and must not bleed into the UI.
        """
        monkeypatch.setenv("MINIMAX_API_KEY", "from-env")
        # Conftest already isolates user config to an empty tmp file; no need
        # to write anything. Make sure cwd has no config.json either.
        empty = tmp_path / "empty_cwd"
        empty.mkdir()
        monkeypatch.chdir(empty)

        cfg = Config(api_key="from-env")  # load_config would have picked env
        cfg.videos_dir = str(tmp_path / "videos")
        app = create_app(cfg)
        client = TestClient(app)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_key"] is False
        assert data["masked"] == ""
        assert data["source"] == "none"

    def test_ui_save_then_user_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.2.0+ acceptance: env set + POST /api/config saves → GET reports
        has_key=True, source='user'. Mirrors the user journey: first launch
        with env key sees 'unconfigured', saving through the UI flips it to
        'user', and the env var is no longer the source."""
        monkeypatch.setenv("MINIMAX_API_KEY", "from-env")
        empty = tmp_path / "empty_cwd"
        empty.mkdir()
        monkeypatch.chdir(empty)

        cfg = Config(api_key=None)  # even with no in-memory key
        cfg.videos_dir = str(tmp_path / "videos")
        app = create_app(cfg)
        client = TestClient(app)

        # Pre-save: unconfigured.
        resp = client.get("/api/config")
        assert resp.json()["source"] == "none"

        # Save a key through the UI.
        resp = client.post("/api/config", json={"api_key": "user-saved-9999"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["source"] == "user"

        # Now GET reflects the saved key.
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_key"] is True
        assert data["masked"] == "...9999"
        assert data["source"] == "user"

    def test_ui_save_writes_user_file_cli_reads_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.2.0+ acceptance: POST /api/config → user config file is updated
        → a subsequent ``config.load_config()`` call returns the new key. This
        is the contract that makes 'UI change, CLI sees it' work without a
        restart."""
        empty = tmp_path / "empty_cwd"
        empty.mkdir()
        monkeypatch.chdir(empty)

        cfg = Config(api_key=None)
        cfg.videos_dir = str(tmp_path / "videos")
        app = create_app(cfg)
        client = TestClient(app)

        resp = client.post("/api/config", json={"api_key": "cli-sync-key-42"})
        assert resp.status_code == 200, resp.text

        # CLI side: load_config() must see the key saved by the UI.
        from minipic.config import load_config
        cli_cfg = load_config()
        assert cli_cfg.api_key == "cli-sync-key-42"


# --------------------------------------------------------------------------- _detect_key_source
class TestDetectKeySource:
    def test_env_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINIMAX_API_KEY", "from-env")
        assert _detect_key_source(Config(api_key="from-env")) == "env"

    def test_user_when_user_file_has_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        # Put the user config in a *sub* directory so it doesn't collide
        # with the local config check (which looks at cwd/config.json).
        user_path = tmp_path / "user_dir" / "config.json"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(json.dumps({"api_key": "user-key"}), encoding="utf-8")
        # Patch in BOTH namespaces: minipic.config (load_config) and
        # minipic.web (where _detect_key_source is bound).
        import minipic.config as config_mod
        import minipic.web as web_mod
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: user_path)
        monkeypatch.setattr(web_mod, "_user_config_path", lambda: user_path)
        # chdir to a sibling empty dir so the local check is cleanly empty.
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        assert _detect_key_source(Config(api_key="user-key")) == "user"

    def test_local_when_cwd_config_has_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        local = tmp_path / "config.json"
        local.write_text(json.dumps({"api_key": "local-key"}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert _detect_key_source(Config(api_key="local-key")) == "local"

    def test_local_wins_over_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        local = tmp_path / "config.json"
        local.write_text(json.dumps({"api_key": "local-key"}), encoding="utf-8")
        user_path = tmp_path / "user" / "config.json"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(json.dumps({"api_key": "user-key"}), encoding="utf-8")
        import minipic.config as config_mod
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: user_path)
        monkeypatch.chdir(tmp_path)
        # cfg picks the local value (matches load_config priority)
        assert _detect_key_source(Config(api_key="local-key")) == "local"

    def test_none_when_no_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)
        import minipic.config as config_mod
        monkeypatch.setattr(
            config_mod, "_user_config_path",
            lambda: tmp_path / "nope.json",
        )
        assert _detect_key_source(Config()) == "none"

    def test_user_wins_over_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env set + user file has key → user (UI-saved key overrides env)."""
        monkeypatch.setenv("MINIMAX_API_KEY", "from-env")
        user_path = tmp_path / "user_dir" / "config.json"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(json.dumps({"api_key": "user-key"}), encoding="utf-8")
        import minipic.config as config_mod
        import minipic.web as web_mod
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: user_path)
        monkeypatch.setattr(web_mod, "_user_config_path", lambda: user_path)
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        assert _detect_key_source(Config(api_key="user-key")) == "user"

    def test_local_wins_over_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env set + cwd config.json has key → local (file wins over env)."""
        monkeypatch.setenv("MINIMAX_API_KEY", "from-env")
        local = tmp_path / "config.json"
        local.write_text(json.dumps({"api_key": "local-key"}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert _detect_key_source(Config(api_key="local-key")) == "local"


# --------------------------------------------------------------------------- POST /api/create — model validation
class TestApiCreateModelValidation:
    def test_h3_max_rejects_2k(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9",
            "model": "MiniMax-H3-Max", "resolution": "2K",
        })
        assert resp.status_code == 400
        assert "2K" in resp.json()["detail"]

    def test_h3_max_rejects_duration_4(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9",
            "model": "MiniMax-H3-Max", "duration": 4,
        })
        assert resp.status_code == 400
        assert "duration" in resp.json()["detail"].lower()

    def test_h3_max_accepts_768p_duration_5(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        fake = fake_client_factory()
        fake.create_video_task.return_value = "tid-max-ok"
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9",
            "model": "MiniMax-H3-Max", "resolution": "768P", "duration": 5,
        })
        assert resp.status_code == 200
        # Verify the model was passed through
        kwargs = fake.create_video_task.await_args.kwargs
        assert kwargs["model"] == "MiniMax-H3-Max"
        assert kwargs["resolution"] == "768P"
        assert kwargs["duration"] == 5

    def test_h3_default_model_passed(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        fake = fake_client_factory()
        fake.create_video_task.return_value = "tid-default-model"
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9", "resolution": "2K",
        })
        assert resp.status_code == 200
        kwargs = fake.create_video_task.await_args.kwargs
        assert kwargs["model"] == "MiniMax-H3"

    def test_h3_max_rejects_r2v(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """H3-Max + r2v → 400，明确提示不支持多模态参考。"""
        resp = web_client.post("/api/create", json={
            "mode": "r2v", "prompt": "x", "resolution": "768P",
            "model": "MiniMax-H3-Max",
            "ref_images": [{"path": "/a.jpg", "role": "reference_image"}],
        })
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "MiniMax-H3-Max" in detail
        assert "r2v" in detail
        assert "不支持多模态参考" in detail

    def test_h3_accepts_r2v(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """H3 + r2v 应当通过校验（即使 ref 含 r2v 不允许的 role 也至少过 mode 校验）。"""
        fake = fake_client_factory()
        fake.create_video_task.return_value = "tid-h3-r2v"
        resp = web_client.post("/api/create", json={
            "mode": "r2v", "prompt": "x", "resolution": "768P",
            "model": "MiniMax-H3",
            "ref_images": [{"path": "https://cdn/a.jpg", "role": "reference_image"}],
        })
        assert resp.status_code == 200, resp.text

    def test_h3_max_rejects_middle_frame(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """H3-Max 不支持 i2v 中间帧。"""
        resp = web_client.post("/api/create", json={
            "mode": "i2v", "prompt": "x", "resolution": "768P",
            "model": "MiniMax-H3-Max",
            "ref_images": [{"path": "https://cdn/a.jpg", "role": "middle_frame"}],
        })
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "middle_frame" in detail

    def test_h3_rejects_middle_frame(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """v0.1.6+：官方 V2 API schema 的 role 枚举不含 middle_frame，
        H3 也已收紧为首/尾帧两角色；中间帧应被拒绝。"""
        resp = web_client.post("/api/create", json={
            "mode": "i2v", "prompt": "x", "resolution": "768P",
            "model": "MiniMax-H3",
            "ref_images": [{"path": "https://cdn/a.jpg", "role": "middle_frame"}],
        })
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "middle_frame" in detail

    def test_i2v_first_and_last_frame_combo(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """i2v 首+尾帧组合：两张图，一张 role=first_frame、一张 role=last_frame。"""
        fake = fake_client_factory()
        fake.create_video_task.return_value = "tid-i2v-double"
        resp = web_client.post("/api/create", json={
            "mode": "i2v", "prompt": "x", "resolution": "768P",
            "model": "MiniMax-H3",
            "ref_images": [
                {"path": "https://cdn/first.jpg", "role": "first_frame"},
                {"path": "https://cdn/last.jpg", "role": "last_frame"},
            ],
        })
        assert resp.status_code == 200, resp.text

    def test_i2v_two_same_role_rejected(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """i2v 两张图不能同角色（必须是 first + last）。"""
        resp = web_client.post("/api/create", json={
            "mode": "i2v", "prompt": "x", "resolution": "768P",
            "model": "MiniMax-H3",
            "ref_images": [
                {"path": "https://cdn/a.jpg", "role": "first_frame"},
                {"path": "https://cdn/b.jpg", "role": "first_frame"},
            ],
        })
        assert resp.status_code == 400
        assert "first_frame" in resp.json()["detail"]

    def test_r2v_rejects_first_frame_role(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """r2v 不接受 first_frame 角色（图生视频与多模态参考互斥）。"""
        resp = web_client.post("/api/create", json={
            "mode": "r2v", "prompt": "x", "resolution": "768P",
            "model": "MiniMax-H3",
            "ref_images": [{"path": "https://cdn/a.jpg", "role": "first_frame"}],
        })
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "互斥" in detail or "reference_image" in detail

    def test_prompt_over_7000_chars_rejected(
        self, web_client: TestClient
    ) -> None:
        """text 超过 7000 字符 → 400。"""
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "a" * 7001, "ratio": "16:9",
            "resolution": "768P",
        })
        assert resp.status_code == 400
        assert "7000" in resp.json()["detail"]

    def test_ref_images_over_9_rejected(
        self, web_client: TestClient
    ) -> None:
        """ref_images 超过 9 张 → 400。"""
        refs = [{"path": f"https://cdn/{i}.jpg", "role": "reference_image"}
                for i in range(10)]
        resp = web_client.post("/api/create", json={
            "mode": "r2v", "prompt": "x", "resolution": "768P",
            "model": "MiniMax-H3",
            "ref_images": refs,
        })
        assert resp.status_code == 400
        assert "9" in resp.json()["detail"]

    def test_duplicate_first_frame_rejected(
        self, web_client: TestClient
    ) -> None:
        """多张图共享 first_frame 角色 → 400（每角色至多 1 张）。"""
        resp = web_client.post("/api/create", json={
            "mode": "r2v", "prompt": "x", "resolution": "768P",
            "model": "MiniMax-H3",
            "ref_images": [
                {"path": "https://cdn/a.jpg", "role": "first_frame"},
                {"path": "https://cdn/b.jpg", "role": "first_frame"},
            ],
        })
        assert resp.status_code == 400
        assert "first_frame" in resp.json()["detail"]

    def test_h3_max_accepts_first_and_last_frame(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        """H3-Max 支持 i2v 首帧/尾帧。"""
        for role in ("first_frame", "last_frame"):
            fake = fake_client_factory()
            fake.create_video_task.return_value = f"tid-{role}"
            resp = web_client.post("/api/create", json={
                "mode": "i2v", "prompt": "x", "resolution": "768P",
                "model": "MiniMax-H3-Max",
                "ref_images": [{"path": "https://cdn/a.jpg", "role": role}],
            })
            assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- /api/config exposes mode/role capabilities
class TestApiConfigModeCapabilities:
    def test_models_include_modes_and_i2v_roles(
        self, web_client: TestClient
    ) -> None:
        resp = web_client.get("/api/config")
        data = resp.json()
        for m in data["models"]:
            assert "modes" in m
            assert "i2v_roles" in m
        h3 = next(m for m in data["models"] if m["name"] == "MiniMax-H3")
        h3m = next(m for m in data["models"] if m["name"] == "MiniMax-H3-Max")
        assert h3["modes"] == ["t2v", "i2v", "r2v"]
        assert h3["i2v_roles"] == ["first_frame", "last_frame"]
        assert h3m["modes"] == ["t2v", "i2v"]
        assert h3m["i2v_roles"] == ["first_frame", "last_frame"]


# --------------------------------------------------------------------------- _submit_task — model field passthrough
class TestSubmitTaskModel:
    @pytest.mark.asyncio
    async def test_default_model_h3(self) -> None:
        body = CreateBody(mode="t2v", prompt="x", ratio="16:9")
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-1")
        await _submit_task(client, body)
        assert client.create_video_task.await_args.kwargs["model"] == "MiniMax-H3"

    @pytest.mark.asyncio
    async def test_h3_max_explicit(self) -> None:
        body = CreateBody(
            mode="t2v", prompt="x", ratio="16:9",
            model="MiniMax-H3-Max", resolution="768P", duration=5,
        )
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-max")
        await _submit_task(client, body)
        kwargs = client.create_video_task.await_args.kwargs
        assert kwargs["model"] == "MiniMax-H3-Max"


# ============================================================ /api/create single entry point
class TestApiCreateSingleEntry:
    def test_still_works(
        self, web_client: TestClient, fake_client_factory
    ) -> None:
        fake = fake_client_factory()
        fake.create_video_task.return_value = "tid-legacy"
        resp = web_client.post("/api/create", json={
            "mode": "t2v", "prompt": "x", "ratio": "16:9",
        })
        assert resp.status_code == 200
        assert resp.json()["task_id"] == "tid-legacy"


# ============================================================ v0.1.4 /api/upload
class TestApiUpload:
    PNG_1x1 = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa3z\x1fR~"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    def test_upload_image_success(
        self, web_client: TestClient
    ) -> None:
        resp = web_client.post(
            "/api/upload",
            files={"file": ("photo.png", self.PNG_1x1, "image/png")},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Every required field is present
        assert set(data.keys()) == {"path", "kind", "size", "content_type", "sha256"}
        assert data["kind"] == "image"
        assert data["content_type"] == "image/png"
        assert data["size"] == len(self.PNG_1x1)
        # sha256 is hex, 64 chars
        assert len(data["sha256"]) == 64
        # Path must exist on disk and live under user data dir
        p = Path(data["path"])
        assert p.is_file()
        assert p.suffix == ".png"
        assert p.read_bytes() == self.PNG_1x1
        # Filename is uuid-style (32 hex chars), NOT the client-provided name
        assert p.stem != "photo"
        assert len(p.stem) == 32
        int(p.stem, 16)  # parseable as hex

    def test_upload_path_traversal(
        self, web_client: TestClient
    ) -> None:
        """A traversal-style client filename must NOT bleed into disk."""
        evil_name = "../../etc/passwd.png"
        resp = web_client.post(
            "/api/upload",
            files={"file": (evil_name, self.PNG_1x1, "image/png")},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        p = Path(data["path"])
        # The disk filename must NOT contain "passwd" or any slash
        assert "passwd" not in p.name
        assert "/" not in p.name
        assert "\\" not in p.name
        # The stored path must be inside user_data/uploads/
        from minipic.config import _user_data_dir
        uploads = _user_data_dir() / "uploads"
        assert p.parent.resolve() == uploads.resolve()

    def test_upload_size_limit(
        self, web_client: TestClient
    ) -> None:
        """51MB payload → 400, no partial file left behind.

        With Content-Length present (httpx default), the pre-flight guard
        fires first; the detail message references Content-Length.
        """
        from minipic.config import _user_data_dir
        uploads = _user_data_dir() / "uploads"
        if uploads.is_dir():
            before = {p.name for p in uploads.iterdir()}
        else:
            before = set()
        big = b"x" * (51 * 1024 * 1024)
        resp = web_client.post(
            "/api/upload",
            files={"file": ("big.png", big, "image/png")},
        )
        assert resp.status_code == 400, resp.text
        assert "Content-Length" in resp.json()["detail"]
        if uploads.is_dir():
            after = {p.name for p in uploads.iterdir()}
            assert after == before

    def test_upload_size_limit_streaming(
        self, web_client: TestClient
    ) -> None:
        """Oversize without Content-Length → 400 via streaming guard.

        Simulates a chunked / unknown-length body by sending raw multipart
        bytes with the Content-Length header stripped (httpx normally
        auto-sets it). Verifies the streaming byte-counter guard is the
        fallback when the pre-flight cannot run.
        """
        import httpx

        from minipic.config import _user_data_dir
        uploads = _user_data_dir() / "uploads"
        if uploads.is_dir():
            before = {p.name for p in uploads.iterdir()}
        else:
            before = set()
        # Construct a multipart body that's 51MB total.
        boundary = "----minipicboundaryabcdef0123456789"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="big.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        payload_size = 51 * 1024 * 1024
        padding = b"x" * (
            payload_size - len(head) - len(b"PNG") - len(tail)
        )
        # Force Content-Length to a small number so pre-flight passes,
        # but the actual streamed body is >50MB. Real chunked-encoding
        # clients do exactly this.
        fake_cl = str(len(head) + 100).encode()
        body = head + b"PNG" + padding + tail
        resp = web_client.post(
            "/api/upload",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": fake_cl,
            },
        )
        # Either pre-flight OR streaming guard caught it (both 400).
        assert resp.status_code == 400, resp.text
        if uploads.is_dir():
            after = {p.name for p in uploads.iterdir()}
            assert after == before

    def test_upload_mime_reject(
        self, web_client: TestClient
    ) -> None:
        """An octet-stream (or any non-allow-listed) MIME → 400."""
        resp = web_client.post(
            "/api/upload",
            files={"file": ("malware.exe", b"MZ\x00\x00", "application/octet-stream")},
        )
        assert resp.status_code == 400, resp.text
        assert "unsupported" in resp.json()["detail"].lower()

    def test_upload_audio_accepts_mp3(
        self, web_client: TestClient
    ) -> None:
        """Spot-check: audio/mpeg is on the allow-list."""
        resp = web_client.post(
            "/api/upload",
            files={"file": ("track.mp3", b"ID3\x03\x00" + b"\x00" * 100,
                            "audio/mpeg")},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["kind"] == "audio"
        assert resp.json()["content_type"] == "audio/mpeg"

    def test_upload_image_accepts_heic(
        self, web_client: TestClient
    ) -> None:
        """HEIC 与 HEIF 已加入 image allow-list，对齐 V2 文档。"""
        # HEIC body content is irrelevant for the MIME allow-list check.
        resp = web_client.post(
            "/api/upload",
            files={"file": ("photo.heic", b"\x00" * 32, "image/heic")},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["kind"] == "image"
        assert data["content_type"] == "image/heic"
        assert Path(data["path"]).suffix == ".heic"

    def test_upload_image_accepts_heif(
        self, web_client: TestClient
    ) -> None:
        resp = web_client.post(
            "/api/upload",
            files={"file": ("photo.heif", b"\x00" * 32, "image/heif")},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["kind"] == "image"
        assert data["content_type"] == "image/heif"

    def test_upload_image_over_30mb_rejected(
        self, web_client: TestClient
    ) -> None:
        """图片 >30MB (per-kind cap) → 400。"""
        from minipic.web import MAX_IMAGE_BYTES
        # Use a Content-Length within global cap so pre-flight passes;
        # the streaming guard then enforces the per-kind 30MB cap.
        boundary = "----minipicboundaryabcdef0123456789"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="big.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        # 31MB body, fake CL small so pre-flight passes; streaming guard catches.
        padding = b"x" * (31 * 1024 * 1024)
        body = head + padding + tail
        resp = web_client.post(
            "/api/upload",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(head) + 100),
            },
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert str(MAX_IMAGE_BYTES) in detail
        assert "image" in detail

    def test_upload_audio_over_15mb_rejected(
        self, web_client: TestClient
    ) -> None:
        """音频 >15MB (per-kind cap) → 400。"""
        from minipic.web import MAX_AUDIO_BYTES
        boundary = "----minipicboundaryabcdef0123456789"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="big.mp3"\r\n'
            f"Content-Type: audio/mpeg\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        # 16MB body
        padding = b"x" * (16 * 1024 * 1024)
        body = head + padding + tail
        resp = web_client.post(
            "/api/upload",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(head) + 100),
            },
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert str(MAX_AUDIO_BYTES) in detail
        assert "audio" in detail


# ============================================================ v0.1.5 GET /api/probe/video
class TestApiProbeVideo:
    def test_probe_returns_duration(
        self, web_client: TestClient, tmp_file, monkeypatch
    ) -> None:
        """Mocked ffprobe_duration: pass a path → get {duration, path} back."""
        from minipic.web import MAX_UPLOAD_BYTES  # sanity check module loads
        # Use a path that exists so is_file() passes.
        p = tmp_file("fake.mp4", b"\x00" * 100)
        monkeypatch.setattr("minipic.web.ffprobe_duration", lambda _p: 17.77)
        r = web_client.get("/api/probe/video", params={"path": str(p)})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["duration"] == 17.77
        assert data["path"] == str(p)

    def test_probe_404_for_missing_file(self, web_client: TestClient) -> None:
        """A path that doesn't exist → 404 (no disk read attempted)."""
        r = web_client.get("/api/probe/video", params={"path": "/no/such/file.mp4"})
        assert r.status_code == 404, r.text
        assert "not found" in r.json()["detail"].lower()

    def test_probe_400_on_ffprobe_failure(
        self, web_client: TestClient, tmp_file, monkeypatch
    ) -> None:
        """ffprobe raising MediaError → 400 with detail."""
        from minipic.errors import MediaError
        p = tmp_file("corrupt.mp4", b"\x00" * 100)
        def _boom(_p: object) -> None:
            raise MediaError("ffprobe failed: bogus stream")
        monkeypatch.setattr("minipic.web.ffprobe_duration", _boom)
        r = web_client.get("/api/probe/video", params={"path": str(p)})
        assert r.status_code == 400, r.text
        assert "probe failed" in r.json()["detail"].lower()

    def test_probe_rejects_empty_path(self, web_client: TestClient) -> None:
        """Empty path → 422 (FastAPI Query validation)."""
        r = web_client.get("/api/probe/video", params={"path": ""})
        assert r.status_code == 422, r.text

