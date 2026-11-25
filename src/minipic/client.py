"""Async httpx client for the MiniMax public API.

Endpoints we use:
  - POST /v1/files/upload                 (upload a local file, get back file_id)
  - POST /v2/video_generation             (create a generation task, get back task_id)
  - GET  /v2/query/video_generation/{task_id}  (poll status, get back content.url on success)

Why both V1 and V2: H3 V2 content[] references uploaded files via the
``mm_file://{file_id}`` URL scheme — no /v1/files/retrieve round trip is
needed. We upload with the V1 file API and reference the returned file_id
directly.

Providers (v0.2.8+): the same V2 protocol is served by more than one vendor.
``cfg.provider`` selects the base URL, the upload path, and the wire model id;
see ``config.PROVIDERS``. Capability differences (Vectron has no H3-Max, no 2K,
no Context-IR, no image-01) are enforced before the request is built so the
caller gets a clear error instead of a vendor-side 4xx.

Authentication: Bearer <api_key>.
Retry policy: 1000 / 1001 / 1002 with bounded backoff. Other codes raise immediately.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import Config, provider_constraints, resolve_model_id
from .errors import (
    ApiErrorPayload,
    ConfigError,
    RETRYABLE_CODES,
    RETRYABLE_HTTP_STATUS,
    ServerError,
    raise_for_code,
)

log = logging.getLogger(__name__)

# Endpoints (relative to base_url). Upload is provider-specific — read it from
# ``cfg.upload_path`` rather than these constants.
EP_UPLOAD = "/v1/files/upload"
EP_CREATE_VIDEO = "/v2/video_generation"
EP_QUERY_VIDEO = "/v2/query/video_generation/{task_id}"
EP_CONTEXT_IR = "/v2/h3_context_ir"
EP_IMAGE_GENERATION = "/v1/image_generation"

# V2 content[] references uploaded files with this scheme: mm_file://{file_id}
MM_FILE_PREFIX = "mm_file://"


class MiniMaxClient:
    """Thin async client. Holds a single httpx.AsyncClient for the lifetime."""

    def __init__(self, cfg: Config) -> None:
        if not cfg.is_valid():
            raise ConfigError("API key not configured")
        self.cfg = cfg
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def base_url(self) -> str:
        return self.cfg.effective_base_url()

    @property
    def provider(self) -> str:
        return self.cfg.provider_key()

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
        """Upload a local file to the provider's file API. Returns the file_id.

        Response shapes differ between providers, so the id is extracted
        tolerantly:
          - official: ``{"file": {"file_id": "..."}}`` or ``{"file_id": "..."}``
          - Vectron:  ``{"id": "...", "filename": ..., "purpose": ...}``
        """
        if not path.is_file():
            raise ConfigError(f"file not found: {path}")

        mime = _guess_mime(path)
        with path.open("rb") as f:
            files = {"file": (path.name, f, mime)}
            data = {"purpose": purpose}
            try:
                resp = await self._request_with_retry(
                    "POST", self.cfg.upload_path, data=data, files=files
                )
            except httpx.HTTPError as e:
                raise ConfigError(f"upload failed: {e}") from e
        body = resp.json()
        file_id = _extract_file_id(body)
        if not file_id:
            raise ConfigError(f"upload response missing file_id: {body}")
        log.debug("uploaded %s as %s", path.name, file_id)
        return file_id

    async def resolve_file_url(self, file_id: str) -> str:
        """Return the V2 content[] URL reference for an uploaded file_id.

        V2 content[] items accept ``mm_file://{file_id}`` to reference a file
        that was uploaded via the V1 file API — no /v1/files/retrieve round
        trip is needed (retrieve returns file metadata, not a CDN URL).
        """
        return f"mm_file://{file_id}"

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
        aigc_watermark: bool | None = None,
        prompt_expansion_mode: str | None = None,
    ) -> str:
        """Submit a generation task. Returns task_id.

        Optional pass-through fields aligned with the V2 API schema:
          - ``aigc_watermark`` (bool): AIGC watermark toggle (both models).
          - ``prompt_expansion_mode`` (str ∈ {disabled, balanced, quality}):
            only MiniMax-H3-Max supports it; sent under ``payload["extra"]``.

        Passing ``None`` (default) means "do not send the field". Per the
        official docs the API rejects empty strings / booleans / undeclared
        fields, so we omit entirely instead of sending nulls.

        ``model`` is a **canonical** name (``MiniMax-H3`` / ``MiniMax-H3-Max``);
        it is translated to the provider's wire id here (Vectron:
        ``MiniMax/MiniMax-H3``). A model the provider does not serve raises
        ConfigError before any request is made.
        """
        # Raises ConfigError when this provider does not serve ``model`` —
        # the canonical-name guard for every caller (web, CLI, future surfaces).
        provider_constraints(self.provider, model)
        wire_model = resolve_model_id(self.provider, model)

        payload: dict[str, Any] = {
            "model": wire_model,
            "content": content,
            "duration": duration,
            "resolution": resolution,
            "ratio": ratio,
        }
        if aigc_watermark is not None:
            payload["aigc_watermark"] = bool(aigc_watermark)
        if prompt_expansion_mode is not None:
            # Validation: only H3-Max supports prompt_expansion_mode, and the
            # value must be one of the documented enum entries. We check here
            # (not in the web/CLI layer) so that every caller — web, CLI, and
            # future surfaces — gets the same guard before hitting the API.
            if model != "MiniMax-H3-Max":
                raise ConfigError(
                    f"prompt_expansion_mode is only supported by MiniMax-H3-Max "
                    f"(got model={model!r})"
                )
            if not self.cfg.provider_spec()["supports_prompt_expansion"]:
                raise ConfigError(
                    f"provider {self.provider!r} does not support prompt_expansion_mode"
                )
            if prompt_expansion_mode not in {"disabled", "balanced", "quality"}:
                raise ConfigError(
                    f"invalid prompt_expansion_mode: {prompt_expansion_mode!r} "
                    f"(allowed: disabled, balanced, quality)"
                )
            # Use setdefault so future extra-* fields (e.g. extra.safety_tier,
            # extra.seed) can extend the dict without overwriting peers that
            # may be added by upstream code paths.
            payload.setdefault("extra", {})["prompt_expansion_mode"] = prompt_expansion_mode
        resp = await self._request_with_retry("POST", EP_CREATE_VIDEO, json_body=payload)
        body = resp.json()
        task_id = body.get("task_id")
        if not task_id:
            raise ConfigError(f"submit response missing task_id: {body}")
        log.debug("submitted task %s", task_id)
        return task_id

    # ---------------------------------------------------------------- image generation

    async def generate_image(
        self,
        *,
        prompt: str,
        aspect_ratio: str = "16:9",
        response_format: str = "url",
        n: int = 1,
        prompt_optimizer: bool = True,
    ) -> list[str]:
        """Generate one or more images via MiniMax image-01 (synchronous).

        Returns a list of image URLs (``response_format="url"``) or base64
        strings (``response_format="base64"``). URLs expire after 24 hours.

        Only the official platform serves image-01; providers without it raise
        ConfigError instead of a vendor-side 404.
        """
        if not self.cfg.supports_image_generation:
            raise ConfigError(
                f"provider {self.provider!r} does not provide image generation"
            )
        payload: dict[str, Any] = {
            "model": "image-01",
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "response_format": response_format,
            "n": n,
            "prompt_optimizer": prompt_optimizer,
        }
        resp = await self._request_with_retry(
            "POST", EP_IMAGE_GENERATION, json_body=payload
        )
        body = resp.json()
        data = body.get("data") or {}
        if response_format == "url":
            urls = data.get("image_urls") or []
        else:
            urls = data.get("image_base64") or []
        if not urls:
            raise ConfigError(f"image generation response missing images: {body}")
        log.debug("generated %d image(s)", len(urls))
        return urls

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

        The endpoint is official-platform only; providers without it raise
        ConfigError so callers can fall back to the original prompt.
        """
        if not self.cfg.supports_context_ir:
            raise ConfigError(
                f"provider {self.provider!r} does not provide H3-Context-IR"
            )
        if model != "MiniMax-H3":
            raise ConfigError(
                f"unsupported model: {model!r} (H3-Context-IR only supports MiniMax-H3)"
            )
        payload: dict[str, Any] = {
            "model": resolve_model_id(self.provider, model),
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

    async def fetch_context_ir_prompt(self, task_id: str, *, max_wait_seconds: float = 90.0) -> str:
        """Poll a Context-IR task until terminal, then return content.prompt.

        Context-IR typically completes in under a minute; we cap at 90s so
        that a stuck Context-IR call doesn't block submission of the real
        generation task. Callers wanting longer waits can pass an explicit
        ``max_wait_seconds``. Loops internally because the success payload
        lives at ``task.content.prompt`` (string) instead of
        ``task.content.url``.
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

            if 200 <= resp.status_code < 300:
                return resp

            # Parse error body
            try:
                err_body = resp.json()
            except Exception:  # noqa: BLE001
                err_body = {"message": resp.text[:500]}
            if not isinstance(err_body, dict):
                err_body = {"message": resp.text[:500]}

            err_obj = err_body.get("error")
            if not isinstance(err_obj, dict):
                err_obj = {}
            code = err_body.get("code") or err_obj.get("code") or 0
            msg = (
                err_obj.get("message")
                or err_body.get("message")
                or err_obj.get("msg")
                or resp.text[:200]
            )
            request_id = err_body.get("request_id") or err_body.get("id")

            # Fallback: the official V2 error body has no numeric ``code``
            # field — the error code lives only in the trailing ``(NNNN)`` of
            # the message string (e.g. ``"rate limit, please retry later (1002)"``).
            # Without this, RETRYABLE_CODES (1000/1001/1002) and the per-code
            # exception map never fire — every error silently falls through to
            # the generic MiniPicError. Extract from msg when code is still 0.
            if not code and isinstance(msg, str):
                m = re.search(r"\((\d{3,4})\)\s*$", msg)
                if m:
                    code = int(m.group(1))

            # Providers that report errors by bare HTTP status (Vectron) never
            # populate ``code``; treat their retryable statuses the same way.
            retryable = code in RETRYABLE_CODES or (
                not code and resp.status_code in RETRYABLE_HTTP_STATUS
            )
            if retryable and attempt < max_attempts:
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


def _extract_file_id(body: dict[str, Any]) -> Optional[str]:
    """Pull the uploaded file id out of either provider's upload response.

    Shapes observed:
      - official: ``{"file": {"file_id": "..."}}`` (nested file object)
      - official: ``{"file_id": "..."}`` (flat, older/variant responses)
      - Vectron:  ``{"id": "...", "filename": ..., "purpose": ...}``
    """
    file_obj = body.get("file")
    if isinstance(file_obj, dict) and file_obj.get("file_id"):
        return str(file_obj["file_id"])
    for key in ("file_id", "id"):
        value = body.get(key)
        if value:
            return str(value)
    return None


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
