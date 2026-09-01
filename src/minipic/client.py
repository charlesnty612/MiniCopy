"""Async httpx client for the MiniMax public API.

Endpoints we use:
  - POST /v1/files/upload                 (upload a local file, get back file_id)
  - GET  /v1/files/retrieve?file_id=...   (resolve a file_id to a CDN URL for V2 content[])
  - POST /v2/video_generation             (create a generation task, get back task_id)
  - GET  /v2/query/video_generation/{task_id}  (poll status, get back content.url on success)

Why both V1 and V2: H3 V2 content[] only accepts public HTTPS URLs, not file_ids.
We use the V1 file API to upload local files, then resolve to a CDN URL via
GET /v1/files/retrieve before submitting the V2 task.

Authentication: Bearer <api_key>.
Retry policy: 1000 / 1001 / 1002 with bounded backoff. Other codes raise immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import Config
from .errors import (
    ApiErrorPayload,
    ConfigError,
    RETRYABLE_CODES,
    ServerError,
    raise_for_code,
)

log = logging.getLogger(__name__)

# Endpoints (relative to base_url)
EP_UPLOAD = "/v1/files/upload"
EP_FILE_RETRIEVE = "/v1/files/retrieve"
EP_CREATE_VIDEO = "/v2/video_generation"
EP_QUERY_VIDEO = "/v2/query/video_generation/{task_id}"
EP_CONTEXT_IR = "/v2/h3_context_ir"


class MiniMaxClient:
    """Thin async client. Holds a single httpx.AsyncClient for the lifetime."""

    def __init__(self, cfg: Config) -> None:
        if not cfg.is_valid():
            raise ConfigError("API key not configured")
        self.cfg = cfg
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def base_url(self) -> str:
        return self.cfg.base_url.rstrip("/")

    async def __aenter__(self) -> "MiniMaxClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            timeout=httpx.Timeout(self.cfg.request_timeout_seconds),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("client used outside `async with` block")
        return self._client

    # ---------------------------------------------------------------- upload

    async def upload_file(self, path: Path, purpose: str = "video_generation_input") -> str:
        """Upload a local file to V1 file management. Returns the file_id."""
        if not path.is_file():
            raise ConfigError(f"file not found: {path}")

        mime = _guess_mime(path)
        with path.open("rb") as f:
            files = {"file": (path.name, f, mime)}
            data = {"purpose": purpose}
            try:
                resp = await self._request_with_retry(
                    "POST", EP_UPLOAD, data=data, files=files
                )
            except httpx.HTTPError as e:
                raise ConfigError(f"upload failed: {e}") from e
        body = resp.json()
        # V1 response shape: {"file": {"file_id": "...", ...}} or {"file_id": "..."}
        file_id = (
            (body.get("file") or {}).get("file_id")
            or body.get("file_id")
        )
        if not file_id:
            raise ConfigError(f"upload response missing file_id: {body}")
        log.debug("uploaded %s as %s", path.name, file_id)
        return file_id

    async def resolve_file_url(self, file_id: str) -> str:
        """Resolve a V1 file_id to a CDN URL usable in V2 content[]."""
        resp = await self._request_with_retry(
            "GET", EP_FILE_RETRIEVE, params={"file_id": file_id}
        )
        body = resp.json()
        # V1 retrieve returns the file metadata; the URL may be in `file.download_url`,
        # `file.url`, or `url` depending on schema version.
        file_obj = body.get("file") or body
        url = (
            file_obj.get("download_url")
            or file_obj.get("url")
            or body.get("url")
        )
        if not url:
            raise ConfigError(
                f"file retrieve response missing URL: {body}\n"
                f"Tip: if V1 retrieve does not return a URL, use --ref-image-url / "
                f"--ref-video-url with a public HTTPS URL instead of a local file."
            )
        return url

    async def upload_and_resolve(self, path: Path) -> str:
        """Upload a local file and return the CDN URL. Convenience wrapper."""
        file_id = await self.upload_file(path)
        return await self.resolve_file_url(file_id)

    # ---------------------------------------------------------------- create

    async def create_video_task(
        self,
        *,
        model: str,
        content: list[dict[str, Any]],
        duration: int,
        resolution: str = "768P",
        ratio: str = "adaptive",
    ) -> str:
        """Submit a generation task. Returns task_id."""
        if model not in ("MiniMax-H3", "MiniMax-H3-Max"):
            raise ConfigError(
                f"unsupported model: {model!r} (supported: MiniMax-H3, MiniMax-H3-Max)"
            )

        payload: dict[str, Any] = {
            "model": model,
            "content": content,
            "duration": duration,
            "resolution": resolution,
            "ratio": ratio,
        }
        resp = await self._request_with_retry("POST", EP_CREATE_VIDEO, json_body=payload)
        body = resp.json()
        task_id = body.get("task_id")
        if not task_id:
            raise ConfigError(f"submit response missing task_id: {body}")
        log.debug("submitted task %s", task_id)
        return task_id

    # ---------------------------------------------------------------- query

    async def query_video_task(self, task_id: str) -> dict[str, Any]:
        """Poll a task. Returns the raw `task` object from the response."""
        path = EP_QUERY_VIDEO.format(task_id=task_id)
        resp = await self._request_with_retry("GET", path)
        body = resp.json()
        # The response may be `{"task": {...}}` or just `{...}` depending on schema
        if isinstance(body, dict) and "task" in body and isinstance(body["task"], dict):
            return body["task"]
        return body

    # ---------------------------------------------------------------- download

    async def download_video(self, url: str, dest: Path, *, chunk_size: int = 65536) -> None:
        """Stream-download the result MP4. Caller is responsible for atomic rename."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.cfg.request_timeout_seconds, read=600.0),
            follow_redirects=True,
        ) as dl:
            async with dl.stream("GET", url) as resp:
                resp.raise_for_status()
                with dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size):
                        if chunk:
                            f.write(chunk)

    # ---------------------------------------------------------------- context IR

    async def create_context_ir_task(
        self,
        *,
        model: str,
        content: list[dict[str, Any]],
        duration: int,
        ratio: str = "adaptive",
    ) -> str:
        """Submit an H3-Context-IR task. Returns task_id.

        H3-Context-IR is MiniMax's own prompt-rewriting service. You give it a
        brief + optional reference media, it returns a structured H3 6-section
        prompt. Use the returned content.prompt as the text element for a
        follow-up H3 generation call.
        """
        if model != "MiniMax-H3":
            raise ConfigError(
                f"unsupported model: {model!r} (H3-Context-IR only supports MiniMax-H3)"
            )
        payload: dict[str, Any] = {
            "model": model,
            "content": content,
            "duration": duration,
            "ratio": ratio,
        }
        resp = await self._request_with_retry("POST", EP_CONTEXT_IR, json_body=payload)
        body = resp.json()
        task_id = body.get("task_id")
        if not task_id:
            raise ConfigError(f"context-ir submit response missing task_id: {body}")
        log.debug("context-ir task %s", task_id)
        return task_id

    async def fetch_context_ir_prompt(self, task_id: str, *, max_wait_seconds: float = 1800.0) -> str:
        """Poll a Context-IR task until terminal, then return content.prompt.

        Context-IR typically completes in under a minute; we cap at 30 min
        for safety. Loops internally because the success payload lives at
        `task.content.prompt` (string) instead of `task.content.url`.
        """
        interval = max(5.0, float(self.cfg.poll_interval_seconds))
        elapsed = 0.0
        while elapsed < max_wait_seconds:
            await asyncio.sleep(interval)
            elapsed += interval
            task = await self.query_video_task(task_id)
            status = task.get("status", "")
            log.debug("context-ir %s: %s", task_id, status)
            if status in ("Success", "Succeeded", "succeeded"):
                content = task.get("content") or {}
                if isinstance(content, dict):
                    prompt = content.get("prompt")
                    if prompt:
                        return prompt
                raise ConfigError(f"task {task_id} succeeded but no content.prompt in response")
            if status in ("Failed", "Cancelled", "Expired",
                          "failed", "cancelled", "expired"):
                raise ConfigError(f"context-ir task {task_id} ended in {status}")
        raise ConfigError(f"context-ir task {task_id} timed out after {max_wait_seconds:.0f}s")

    # ---------------------------------------------------------------- retry core

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        files: Any = None,
        params: Optional[dict[str, Any]] = None,
        max_attempts: int = 4,
    ) -> httpx.Response:
        for attempt in range(1, max_attempts + 1):
            try:
                if files is not None:
                    resp = await self.http.request(
                        method, path, data=data, files=files, params=params
                    )
                else:
                    resp = await self.http.request(
                        method, path, json=json_body, params=params
                    )
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt == max_attempts:
                    raise ConfigError(f"network error after {attempt} attempts: {e}") from e
                await asyncio.sleep(_backoff(attempt))
                continue

            if resp.status_code == 200:
                return resp

            # Parse error body
            try:
                err_body = resp.json()
            except Exception:  # noqa: BLE001
                err_body = {"message": resp.text[:500]}

            code = err_body.get("code") or err_body.get("error", {}).get("code") or 0
            msg = (
                err_body.get("message")
                or err_body.get("error", {}).get("message")
                or resp.text[:200]
            )
            request_id = err_body.get("request_id") or err_body.get("id")

            if code in RETRYABLE_CODES and attempt < max_attempts:
                ra = resp.headers.get("Retry-After")
                if ra:
                    try:
                        wait = float(ra)
                    except ValueError:
                        wait = _backoff(attempt)
                else:
                    wait = _backoff(attempt)
                log.warning("retryable %s (code=%s), waiting %.1fs", method, code, wait)
                await asyncio.sleep(wait)
                continue

            raise_for_code(
                ApiErrorPayload(
                    code=int(code) if code else 0,
                    message=str(msg),
                    request_id=request_id,
                    http_code=resp.status_code,
                )
            )

        raise ServerError("unreachable: retries exhausted")


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter. attempt is 1-indexed."""
    base = 1.5 ** (attempt - 1)
    return min(30.0, base) * (0.75 + 0.5 * (time.time() % 1))


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
    }.get(ext, "application/octet-stream")
