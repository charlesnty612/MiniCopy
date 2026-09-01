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
from minipic.storage import TaskRecord
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
def web_cfg(sample_config: Config, tmp_path: Path) -> Config:
    """A valid Config pointing videos_dir at a tmp path (so downloads don't pollute)."""
    cfg = sample_config
    cfg.videos_dir = str(tmp_path / "videos")
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
        assert "first_frame" in resp.json()["detail"]

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


# --------------------------------------------------------------------------- POST /api/tasks/{task_id}/download
class TestApiDownloadTask:
    def test_404_for_missing_task(self, web_client: TestClient) -> None:
        resp = web_client.post("/api/tasks/missing/download")
        assert resp.status_code == 404

    def test_404_when_file_not_on_disk(self, web_client: TestClient) -> None:
        from minipic.storage import insert_task
        insert_task(TaskRecord(
            task_id="tid-no-file", mode="t2v", prompt_excerpt="p", status="Success",
            output_path="/nope.mp4",
        ))
        resp = web_client.post("/api/tasks/tid-no-file/download")
        assert resp.status_code == 404

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
            ref_images=[{"path": "/a.jpg", "role": "reference_image"}],
        )
        with pytest.raises(HTTPException):
            _validate_mode(body)

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

        task_id, content = await _submit_task(client, body)
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

        task_id, content = await _submit_task(client, body)
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

        _, content = await _submit_task(client, body)
        img_items = [c for c in content if c.get("type") == "image_url"]
        assert len(img_items) == 1
        assert img_items[0]["url"] == "https://example.com/photo.jpg"
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
        monkeypatch.setattr("minipic.web.resolve_reference", fake_resolve)

        _, content = await _submit_task(client, body)
        img_items = [c for c in content if c.get("type") == "image_url"]
        assert len(img_items) == 1
        assert img_items[0]["url"] == "https://cdn/uploaded.jpg"

    @pytest.mark.asyncio
    async def test_audio_url_included(self) -> None:
        body = CreateBody(
            mode="r2v", prompt="x",
            ref_images=[{"path": "https://cdn/a.jpg", "role": "reference_image"}],
            ref_audios=[{"path": "https://cdn/sound.mp3", "role": "reference_audio"}],
        )
        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-1")
        _, content = await _submit_task(client, body)
        audio_items = [c for c in content if c.get("type") == "audio_url"]
        assert len(audio_items) == 1
        assert audio_items[0]["url"] == "https://cdn/sound.mp3"

    @pytest.mark.asyncio
    async def test_video_clips_emit_one_item_per_clip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from minipic.media import VideoClip

        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        body = CreateBody(
            mode="r2v", prompt="x",
            ref_videos=[{"path": str(video)}],
        )

        # prepare_reference_video returns 2 clips
        async def fake_prep(source: Any, *, clips_spec: Any = None) -> list[VideoClip]:
            return [
                VideoClip(path=source, start_seconds=0.0, duration_seconds=5.0, is_split=False),
                VideoClip(path=source, start_seconds=5.0, duration_seconds=5.0, is_split=True),
            ]
        monkeypatch.setattr("minipic.web.prepare_reference_video", fake_prep)

        async def fake_resolve(client: Any, source: Any) -> str:
            return f"https://cdn/{Path(source).name}"
        monkeypatch.setattr("minipic.web.resolve_reference", fake_resolve)

        client = MagicMock()
        client.create_video_task = AsyncMock(return_value="tid-1")

        _, content = await _submit_task(client, body)
        video_items = [c for c in content if c.get("type") == "video_url"]
        assert len(video_items) == 2
        # Each should carry the duration
        assert all("duration" in c for c in video_items)


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
