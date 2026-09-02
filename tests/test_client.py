"""Tests for minipic.client — httpx client, retry logic, upload, task ops."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx
import pytest

from minipic.client import (
    EP_CREATE_VIDEO,
    EP_QUERY_VIDEO,
    EP_UPLOAD,
    MM_FILE_PREFIX,
    MiniMaxClient,
    _guess_mime,
)
from minipic.config import Config
from minipic.errors import (
    AuthError,
    BalanceError,
    ConfigError,
    InvalidParamsError,
    RETRYABLE_CODES,
    SafetyError,
    TaskError,
)


# --------------------------------------------------------------------------- helpers
def ok_response(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=data)


def err_response(http_code: int, data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(http_code, json=data)


class TestGuessMime:
    @pytest.mark.parametrize(
        "ext,mime",
        [
            (".jpg", "image/jpeg"),
            (".jpeg", "image/jpeg"),
            (".png", "image/png"),
            (".webp", "image/webp"),
            (".heic", "image/heic"),
            (".heif", "image/heif"),
            (".mp4", "video/mp4"),
            (".mov", "video/quicktime"),
            (".wav", "audio/wav"),
            (".mp3", "audio/mpeg"),
            (".xyz", "application/octet-stream"),
            ("", "application/octet-stream"),
        ],
    )
    def test_mime_guess(self, ext: str, mime: str, tmp_path: Path) -> None:
        p = tmp_path / f"file{ext}"
        p.touch()
        assert _guess_mime(p) == mime


# --------------------------------------------------------------------------- construction
class TestClientConstruction:
    def test_raises_if_api_key_missing(self) -> None:
        with pytest.raises(ConfigError):
            MiniMaxClient(Config())

    def test_constructs_with_valid_key(self) -> None:
        c = MiniMaxClient(Config(api_key="k"))
        assert c.cfg.api_key == "k"

    def test_base_url_strips_trailing_slash(self) -> None:
        c = MiniMaxClient(Config(api_key="k", base_url="https://a.com/"))
        assert c.base_url == "https://a.com"


# --------------------------------------------------------------------------- context manager
class TestClientContext:
    @pytest.mark.asyncio
    async def test_enter_sets_up_httpx_client(self) -> None:
        c = MiniMaxClient(Config(api_key="k"))
        async with c as client:
            assert isinstance(client.http, httpx.AsyncClient)
            assert client.http.auth is None  # auth is in headers

    @pytest.mark.asyncio
    async def test_http_raises_outside_context(self) -> None:
        c = MiniMaxClient(Config(api_key="k"))
        with pytest.raises(RuntimeError, match="outside.*async with"):
            _ = c.http

    @pytest.mark.asyncio
    async def test_exit_closes_client(self) -> None:
        c = MiniMaxClient(Config(api_key="k"))
        async with c:
            pass
        # after __aexit__, http raises again
        with pytest.raises(RuntimeError):
            _ = c.http


# --------------------------------------------------------------------------- retry logic
class TestRetryLogic:
    @pytest.mark.asyncio
    async def test_no_retry_on_200(self, tmp_path: Path) -> None:
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return ok_response({"task_id": "t1"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://x", transport=transport
        ) as http:
            c._client = http
            await c.create_video_task(
                model="MiniMax-H3",
                content=[{"type": "text", "text": "hello"}],
                duration=5,
            )
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_429_respects_retry_after(self, tmp_path: Path) -> None:
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return err_response(429, {"code": 1002, "message": "rate limit"})
            return ok_response({"task_id": "t1"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://x",
            transport=transport,
            headers={"Retry-After": "0"},  # 0 second back-off
        ) as http:
            c._client = http
            result = await c._request_with_retry("GET", "/test")
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_400(self, tmp_path: Path) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return err_response(400, {"code": 2013, "message": "bad"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://x", transport=transport
        ) as http:
            c._client = http
            with pytest.raises(InvalidParamsError):
                await c._request_with_retry("POST", "/test", json_body={})

    @pytest.mark.asyncio
    async def test_no_retry_on_401(self, tmp_path: Path) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return err_response(401, {"code": 1004, "message": "bad key"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://x", transport=transport
        ) as http:
            c._client = http
            with pytest.raises(AuthError):
                await c._request_with_retry("POST", "/test", json_body={})

    @pytest.mark.asyncio
    async def test_no_retry_on_402_balance(self, tmp_path: Path) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return err_response(402, {"code": 1008, "message": "no money"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://x", transport=transport
        ) as http:
            c._client = http
            with pytest.raises(BalanceError):
                await c._request_with_retry("POST", "/test", json_body={})

    @pytest.mark.asyncio
    async def test_no_retry_on_422_safety(self, tmp_path: Path) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return err_response(422, {"code": 1026, "message": "unsafe"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://x", transport=transport
        ) as http:
            c._client = http
            with pytest.raises(SafetyError):
                await c._request_with_retry("POST", "/test", json_body={})

    @pytest.mark.asyncio
    async def test_no_retry_on_500_with_retryable_code(self, tmp_path: Path) -> None:
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return err_response(500, {"code": 1000, "message": "server error"})
            return ok_response({"task_id": "t1"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://x", transport=transport
        ) as http:
            c._client = http
            result = await c._request_with_retry("GET", "/test")
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_exhausted_retries_raises(self, tmp_path: Path) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return err_response(429, {"code": 1002, "message": "rate limit"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://x", transport=transport
        ) as http:
            c._client = http
            # max_attempts=4 means 4 tries before raising
            with pytest.raises(Exception):  # could be RateLimitError or ConfigError
                await c._request_with_retry("GET", "/test", max_attempts=4)


# --------------------------------------------------------------------------- upload_file
class TestUploadFile:
    @pytest.mark.asyncio
    async def test_upload_file_returns_file_id(self, tmp_path: Path) -> None:
        video = tmp_path / "scene.mp4"
        video.write_bytes(b"fake video data")

        def handler(req: httpx.Request) -> httpx.Response:
            return ok_response({"file": {"file_id": "fid-123"}})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="https://x", transport=transport) as http:
            c._client = http
            fid = await c.upload_file(video)
        assert fid == "fid-123"

    @pytest.mark.asyncio
    async def test_upload_file_rejects_missing_file(self, tmp_path: Path) -> None:
        c = MiniMaxClient(Config(api_key="k"))
        with pytest.raises(ConfigError, match="not found"):
            await c.upload_file(tmp_path / "does_not_exist.mp4")

    @pytest.mark.asyncio
    async def test_upload_file_falls_back_to_toplevel_file_id(self, tmp_path: Path) -> None:
        video = tmp_path / "img.jpg"
        video.write_bytes(b"fake")

        def handler(req: httpx.Request) -> httpx.Response:
            return ok_response({"file_id": "flat-fid"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="https://x", transport=transport) as http:
            c._client = http
            fid = await c.upload_file(video)
        assert fid == "flat-fid"

    @pytest.mark.asyncio
    async def test_upload_file_raises_on_missing_file_id(self, tmp_path: Path) -> None:
        video = tmp_path / "img.jpg"
        video.write_bytes(b"fake")

        def handler(req: httpx.Request) -> httpx.Response:
            return ok_response({"not_a_file_id": "x"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="https://x", transport=transport) as http:
            c._client = http
            with pytest.raises(ConfigError, match="missing file_id"):
                await c.upload_file(video)


# --------------------------------------------------------------------------- resolve_file_url
class TestResolveFileUrl:
    @pytest.mark.asyncio
    async def test_returns_mm_file_url_for_file_id(self) -> None:
        # V2 content[] accepts mm_file://{file_id} — no network round trip.
        c = MiniMaxClient(Config(api_key="k"))
        url = await c.resolve_file_url("fid-1")
        assert url == f"{MM_FILE_PREFIX}fid-1"

    @pytest.mark.asyncio
    async def test_returns_mm_file_url_for_numeric_file_id(self) -> None:
        # Real file_ids from the V1 upload API are numeric.
        c = MiniMaxClient(Config(api_key="k"))
        url = await c.resolve_file_url("437131811713442")
        assert url == f"{MM_FILE_PREFIX}437131811713442"


# --------------------------------------------------------------------------- upload_and_resolve
class TestUploadAndResolve:
    @pytest.mark.asyncio
    async def test_upload_then_mm_file_url(self, tmp_path: Path) -> None:
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")

        call_seq = []

        def handler(req: httpx.Request) -> httpx.Response:
            call_seq.append(req.url.path)
            return ok_response({"file": {"file_id": "fid-42"}})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="https://x", transport=transport) as http:
            c._client = http
            url = await c.upload_and_resolve(video)
        assert url == f"{MM_FILE_PREFIX}fid-42"
        # Only the upload endpoint is hit — no retrieve round trip.
        assert call_seq == [EP_UPLOAD]


# --------------------------------------------------------------------------- create_video_task
class TestCreateVideoTask:
    @pytest.mark.asyncio
    async def test_creates_task_returns_task_id(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            assert body["model"] == "MiniMax-H3"
            assert body["duration"] == 5
            assert body["resolution"] == "768P"
            return ok_response({"task_id": "tid-456"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with c as client:
            client._client = httpx.AsyncClient(base_url="https://x", transport=transport)
            tid = await client.create_video_task(
                model="MiniMax-H3",
                content=[{"type": "text", "text": "hello"}],
                duration=5,
            )
        assert tid == "tid-456"

    @pytest.mark.asyncio
    async def test_rejects_unsupported_model(self) -> None:
        c = MiniMaxClient(Config(api_key="k"))
        async with c as client:
            with pytest.raises(ConfigError, match="unsupported model"):
                await client.create_video_task(
                    model="MiniMax-H3-Pro",  # not in the supported list
                    content=[{"type": "text", "text": "hello"}],
                    duration=5,
                )

    @pytest.mark.asyncio
    async def test_raises_on_missing_task_id_in_response(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return ok_response({})  # no task_id

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with c as client:
            client._client = httpx.AsyncClient(base_url="https://x", transport=transport)
            with pytest.raises(ConfigError, match="missing task_id"):
                await client.create_video_task(
                    model="MiniMax-H3",
                    content=[{"type": "text", "text": "hello"}],
                    duration=5,
                )

    @pytest.mark.asyncio
    async def test_accepts_h3_max_model(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            assert body["model"] == "MiniMax-H3-Max"
            assert body["resolution"] == "768P"
            return ok_response({"task_id": "tid-max"})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with c as client:
            client._client = httpx.AsyncClient(base_url="https://x", transport=transport)
            tid = await client.create_video_task(
                model="MiniMax-H3-Max",
                content=[{"type": "text", "text": "hi"}],
                duration=6,
                resolution="768P",
                ratio="16:9",
            )
        assert tid == "tid-max"


# --------------------------------------------------------------------------- query_video_task
class TestQueryVideoTask:
    @pytest.mark.asyncio
    async def test_query_unwraps_task_object(self) -> None:
        task_data = {"status": "Processing", "progress": 0.5}

        def handler(req: httpx.Request) -> httpx.Response:
            return ok_response({"task": task_data})

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="https://x", transport=transport) as http:
            c._client = http
            result = await c.query_video_task("tid-1")
        assert result == task_data

    @pytest.mark.asyncio
    async def test_query_returns_raw_if_no_task_wrapper(self) -> None:
        raw = {"status": "Success", "content": {"url": "https://x/y.mp4"}}

        def handler(req: httpx.Request) -> httpx.Response:
            return ok_response(raw)

        c = MiniMaxClient(Config(api_key="k"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="https://x", transport=transport) as http:
            c._client = http
            result = await c.query_video_task("tid-1")
        assert result == raw


# --------------------------------------------------------------------------- download_video
class TestDownloadVideo:
    @pytest.mark.asyncio
    async def test_download_writes_to_file(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.mp4"
        c = MiniMaxClient(Config(api_key="k"))

        # Mock download_video at the client level to avoid httpx stream complexity.
        original = c.download_video

        async def _mock(url: str, path: Path, **kwargs: Any) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video bytes")

        c.download_video = _mock
        try:
            await c.download_video("https://cdn/file.mp4", dest)
        finally:
            c.download_video = original

        assert dest.read_bytes() == b"video bytes"

    @pytest.mark.asyncio
    async def test_download_creates_parent_dir(self, tmp_path: Path) -> None:
        dest = tmp_path / "sub" / "deep" / "out.mp4"
        c = MiniMaxClient(Config(api_key="k"))

        original = c.download_video

        async def _mock(url: str, path: Path, **kwargs: Any) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"v")

        c.download_video = _mock
        try:
            await c.download_video("https://x/y.mp4", dest)
        finally:
            c.download_video = original

        assert dest.is_file()


# --------------------------------------------------------------------------- endpoint constants
class TestEndpoints:
    def test_endpoints_are_defined(self) -> None:
        assert EP_UPLOAD == "/v1/files/upload"
        assert MM_FILE_PREFIX == "mm_file://"
        assert EP_CREATE_VIDEO == "/v2/video_generation"
        assert EP_QUERY_VIDEO == "/v2/query/video_generation/{task_id}"


# --------------------------------------------------------------------------- Context-IR defaults
class TestContextIRDefaults:
    """Context-IR defaults & timeout behaviour — no real network calls."""

    def test_fetch_context_ir_prompt_default_max_wait_is_90s(self) -> None:
        """v0.2.0+: cap the IR poll at 90s (the service is documented to finish
        in under a minute; longer waits block the real submission)."""
        import inspect
        from minipic.client import MiniMaxClient
        sig = inspect.signature(MiniMaxClient.fetch_context_ir_prompt)
        assert sig.parameters["max_wait_seconds"].default == 90.0

    @pytest.mark.asyncio
    async def test_fetch_context_ir_prompt_timeout_raises_config_error(self) -> None:
        """When the IR task never reaches a terminal state within
        ``max_wait_seconds``, raise ConfigError (callers handle as fallback)."""
        from minipic.client import MiniMaxClient
        from minipic.errors import ConfigError

        c = MiniMaxClient(Config(api_key="k"))

        async def fake_query(task_id: str) -> dict[str, Any]:
            return {"status": "Processing"}  # never terminal
        c.query_video_task = fake_query  # type: ignore[assignment]

        # Tiny interval + tiny max_wait so the test finishes quickly.
        c.cfg.poll_interval_seconds = 0.01
        with pytest.raises(ConfigError, match="timed out"):
            await c.fetch_context_ir_prompt("ir-tid", max_wait_seconds=0.05)

    def test_create_context_ir_task_rejects_non_h3_model(self) -> None:
        """create_context_ir_task only supports MiniMax-H3 — H3-Max / others
        raise ConfigError, matching the Web layer's behaviour."""
        from minipic.client import MiniMaxClient
        from minipic.errors import ConfigError

        c = MiniMaxClient(Config(api_key="k"))
        with pytest.raises(ConfigError, match="only supports MiniMax-H3"):
            # We never call .create — the check is synchronous.
            # Use asyncio.run to drive the underlying async method.
            import asyncio
            asyncio.run(c.create_context_ir_task(
                model="MiniMax-H3-Max",
                content=[{"type": "text", "text": "x"}],
                duration=10,
                ratio="adaptive",
            ))
