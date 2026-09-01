"""FastAPI web UI for minipic."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import (
    API_MODELS,
    MODEL_CONSTRAINTS,
    Config,
    _user_config_path,
    load_config,
    mask_key,
    require_api_key,
    save_config,
    validate_model_params,
)
from .client import MiniMaxClient
from .errors import ConfigError, MediaError, MiniPicError, TaskError
from .media import (
    REF_VIDEO_MAX_DURATION,
    extract_reference_clips,
    ffprobe_duration,
    find_shot_boundaries,
    plan_segments,
    prepare_reference_video,
    resolve_reference,
    SegmentSpec,
)
from .poller import poll_until_done, STATUS_SUCCEEDED
from .storage import (
    PREPARE_STATUS_COMMITTED,
    PREPARE_STATUS_PREPARED,
    PREPARE_TTL_MS,
    PrepareRecord,
    TaskRecord,
    cleanup_expired_prepares,
    delete_prepare,
    get_prepare,
    get_task,
    insert_prepare,
    insert_task,
    list_prepares,
    list_tasks,
    update_prepare_status,
    update_task,
)
from . import media as _media_module

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(cfg: Optional[Config] = None) -> FastAPI:
    app = FastAPI(title="minipic", description="MiniMax H3 video generation UI")
    app.state.cfg = cfg or load_config()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost",
            "http://127.0.0.1",
        ],
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1):\d+$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------ GET /
    @app.get("/")
    def root() -> FileResponse:
        web_dir = Path(__file__).parent.parent.parent / "web"
        return FileResponse(web_dir / "index.html", media_type="text/html")

    # ---------------------------------------------------------------- POST /api/create
    @app.post("/api/create", deprecated=True)
    async def api_create(body: CreateBody) -> dict[str, Any]:
        cfg = app.state.cfg  # type: Config

        # Validate API key early
        try:
            require_api_key(cfg)
        except ConfigError as e:
            raise HTTPException(status_code=503, detail=str(e))

        # Validate mode constraints
        _validate_mode(body)

        # Defer insert_task until AFTER we have the real task_id, so we don't
        # leave a zombie "pending" row in the ledger on any failure path.
        rec: Optional[TaskRecord] = None

        try:
            async with MiniMaxClient(cfg) as client:
                task_id, _content_items = await _submit_task(client, body)

                rec = TaskRecord(
                    task_id=task_id, mode=body.mode,
                    prompt_excerpt=body.prompt[:100], status="submitted",
                )
                insert_task(rec)

                # If wait=True, poll and download
                if body.wait:
                    final_task = await poll_until_done(
                        client,
                        task_id,
                        max_wait_seconds=30 * 60,
                    )
                    await _download_result(client, cfg, task_id, final_task)
                    rec.status = final_task.get("status", "Success")
                    out_path = str(Path(cfg.videos_dir) / f"{task_id}.mp4")
                    update_task(task_id, status=rec.status, output_path=out_path)
                    rec.output_path = out_path

                return {"task_id": task_id, "status": "submitted"}

        except TaskError as e:
            if rec is not None:
                update_task(rec.task_id, status="failed", error=str(e))
            raise HTTPException(status_code=400, detail=f"TaskError: {e}")
        except MiniPicError as e:
            if rec is not None:
                update_task(rec.task_id, status="failed", error=str(e))
            raise HTTPException(status_code=400, detail=f"{e.__class__.__name__}: {e}")
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected error in /api/create")
            raise HTTPException(status_code=500, detail="internal")

    # ---------------------------------------------------------------- GET /api/tasks
    @app.get("/api/tasks")
    def api_list_tasks() -> list[dict[str, Any]]:
        return [_record_to_dict(r) for r in list_tasks(limit=50)]

    # ====================================================== v0.1.3b prepare/commit

    @app.post("/api/prepare")
    async def api_prepare(body: CreateBody) -> dict[str, Any]:
        """Build a plan without submitting to H3. Returns a prepare_id for later commit.

        Long reference videos (>15s) are automatically split into multiple
        H3 generations via the plan_segments algorithm. The full content[]
        draft (with each clip's CDN URL) is returned for the user to review.
        """
        cfg = app.state.cfg
        try:
            require_api_key(cfg)
        except ConfigError as e:
            raise HTTPException(status_code=503, detail=str(e))
        _validate_mode(body)

        try:
            plan = await _build_plan(body, cfg)
        except MediaError as e:
            raise HTTPException(
                status_code=400,
                detail=f"准备失败: {e}\n\n提示: 检查参考视频路径/格式（mp4/mov/≤50MB）",
            )

        prepare_id = "prep-" + uuid.uuid4().hex[:12]
        body_dict = body.model_dump()
        now_ms = int(time.time() * 1000)
        rec = PrepareRecord(
            prepare_id=prepare_id,
            payload=body_dict,
            plan=plan,
            status=PREPARE_STATUS_PREPARED,
            created_at=now_ms,
            expires_at=now_ms + PREPARE_TTL_MS,
        )
        insert_prepare(rec)

        return {
            "prepare_id": prepare_id,
            "estimated_segments": max(len(plan["segments"]), 1),
            "segments": plan["segments"],
            "content_preview": plan["content"],
            "warnings": plan["warnings"],
            "expires_at": rec.expires_at,
            "original_prompt": plan["original_prompt"],
            "enhanced_prompt": plan["enhanced_prompt"],
            "used_context_ir": plan["used_context_ir"],
            "context_ir_fallback": plan["context_ir_fallback"],
            "context_ir_error": plan.get("context_ir_error"),
        }

    @app.get("/api/prepare/{prepare_id}")
    def api_get_prepare(prepare_id: str) -> dict[str, Any]:
        """Return a previously-created prepare plan. 410 Gone if expired."""
        cleanup_expired_prepares()  # lazy: mark stale rows on every lookup
        rec = get_prepare(prepare_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="prepare not found")
        now_ms = int(time.time() * 1000)
        if rec.status == PREPARE_STATUS_COMMITTED:
            raise HTTPException(status_code=410, detail="prepare already committed")
        if rec.expires_at <= now_ms:
            raise HTTPException(status_code=410, detail="prepare expired; please re-prepare")
        return {
            "prepare_id": rec.prepare_id,
            "status": rec.status,
            "expires_at": rec.expires_at,
            "original_prompt": rec.plan.get("original_prompt"),
            "enhanced_prompt": rec.plan.get("enhanced_prompt"),
            "used_context_ir": rec.plan.get("used_context_ir", False),
            "context_ir_fallback": rec.plan.get("context_ir_fallback", False),
            "context_ir_error": rec.plan.get("context_ir_error"),
            "segments": rec.plan.get("segments", []),
            "content_preview": rec.plan.get("content", []),
            "warnings": rec.plan.get("warnings", []),
        }

    @app.post("/api/commit/{prepare_id}")
    async def api_commit(prepare_id: str) -> dict[str, Any]:
        """Submit a prepared plan to H3. Returns task_id (parent for multi-segment).

        Background: H3 calls are submitted, polled, and downloaded by
        ``_run_multi_segment`` running as an asyncio task. The endpoint
        returns 202-style metadata immediately so the client can move on.
        """
        rec = get_prepare(prepare_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="prepare not found")
        now_ms = int(time.time() * 1000)
        if rec.status == PREPARE_STATUS_COMMITTED:
            raise HTTPException(status_code=410, detail="prepare already committed")
        if rec.expires_at <= now_ms:
            raise HTTPException(status_code=410, detail="prepare expired; please re-prepare")

        cfg = app.state.cfg
        content = rec.plan.get("content", [])
        segments_meta = rec.plan.get("segments", [])
        body = rec.payload

        parent_id = "task-" + uuid.uuid4().hex[:12]
        parent_rec = TaskRecord(
            task_id=parent_id,
            mode="multi",
            prompt_excerpt=(body.get("prompt") or "")[:100],
            status="submitted",
            extra={
                "prepare_id": prepare_id,
                "segments": [],   # populated as children complete
                "total_duration_seconds": (
                    sum(s["duration_seconds"] for s in segments_meta)
                    if segments_meta else None
                ),
                "enhanced_prompt": rec.plan.get("enhanced_prompt"),
                "used_context_ir": rec.plan.get("used_context_ir", False),
                "context_ir_fallback": rec.plan.get("context_ir_fallback", False),
            },
        )
        insert_task(parent_rec)
        update_prepare_status(prepare_id, PREPARE_STATUS_COMMITTED, task_id=parent_id)

        # Fire background coroutine; H3 calls and downloads happen there.
        asyncio.create_task(
            _run_multi_segment(parent_id, body, content, segments_meta, cfg)
        )

        return {
            "task_id": parent_id,
            "status": "submitted",
            "segments": segments_meta,
        }

    @app.get("/api/prepares")
    def api_list_prepares() -> list[dict[str, Any]]:
        """List recent (non-expired) prepare records — handy for the UI."""
        cleanup_expired_prepares()
        return [
            {
                "prepare_id": r.prepare_id,
                "status": r.status,
                "created_at": r.created_at,
                "expires_at": r.expires_at,
                "task_id": r.task_id,
            }
            for r in list_prepares(include_expired=False, limit=20)
        ]

    # ---------------------------------------------------------------- GET /api/tasks/{task_id}
    @app.get("/api/tasks/{task_id}")
    async def api_get_task(task_id: str) -> dict[str, Any]:
        cfg = app.state.cfg
        rec = get_task(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")

        result: dict[str, Any] = _record_to_dict(rec)

        # If task is pending/queued/processing, try to refresh from API
        if rec.status not in ("submitted", "completed", "failed", "success", "Success", "Failed"):
            try:
                require_api_key(cfg)
                async with MiniMaxClient(cfg) as client:
                    api_task = await client.query_video_task(task_id)
                    status = api_task.get("status", "")
                    update_task(task_id, status=status, last_query_at=int(time.time() * 1000))
                    result["api_status"] = status
                    # Attach content URL if available
                    content = api_task.get("content")
                    if content and isinstance(content, list) and len(content) > 0:
                        url = content[0].get("url") if isinstance(content[0], dict) else None
                        if url:
                            result["content_url"] = url
            except Exception:  # noqa: BLE001
                pass  # Graceful degradation

        return result

    # -------------------------------------------------------- POST /api/tasks/{task_id}/download
    @app.post("/api/tasks/{task_id}/download")
    async def api_download_task(task_id: str) -> Response:
        cfg = app.state.cfg
        rec = get_task(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")

        video_path = Path(cfg.videos_dir) / f"{task_id}.mp4"
        if not video_path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Video file not found at {video_path}. "
                       "It may not have been downloaded yet.",
            )

        return FileResponse(
            video_path,
            media_type="video/mp4",
            filename=f"{task_id}.mp4",
            headers={"Content-Disposition": f'attachment; filename="{task_id}.mp4"'},
        )

    # -------------------------------------------------------- GET /api/config
    @app.get("/api/config")
    def api_get_config() -> dict[str, Any]:
        """Return masked API key + source, plus model metadata for the UI.

        Never returns the raw key.
        """
        cfg = app.state.cfg
        return {
            "has_key": cfg.is_valid(),
            "masked": mask_key(cfg.api_key),
            "source": _detect_key_source(cfg),
            "user_config_path": str(_user_config_path()),
            "models": [
                {
                    "name": name,
                    "resolutions": MODEL_CONSTRAINTS[name]["resolutions"],
                    "duration_min": MODEL_CONSTRAINTS[name]["duration_min"],
                    "duration_max": MODEL_CONSTRAINTS[name]["duration_max"],
                }
                for name in API_MODELS
            ],
            "model_default": "MiniMax-H3",
        }

    # -------------------------------------------------------- POST /api/config
    @app.post("/api/config")
    def api_set_config(payload: Annotated[ConfigBody, Body()]) -> dict[str, Any]:
        """Persist a new API key to the user config dir and reload cfg.

        Replaces ``app.state.cfg.api_key`` directly (no env override) so the
        next /api/create can use it. If the env var ``MINIMAX_API_KEY`` is
        also set, that value will shadow this one on the next full reload —
        the UI surfaces that fact via the ``source`` field of /api/config.
        """
        cfg = app.state.cfg
        new_key = payload.api_key.strip()
        if not new_key:
            raise HTTPException(status_code=400, detail="api_key must not be empty")
        cfg.api_key = new_key
        try:
            saved_to = save_config(cfg, scope="user")
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=f"failed to write config: {e}",
            )
        return {
            "ok": True,
            "masked": mask_key(cfg.api_key),
            "saved_to": str(saved_to),
            "source": _detect_key_source(cfg),
        }

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class CreateBody(BaseModel):
    mode: Annotated[str, Field(pattern=r"^(t2v|i2v|r2v)$")]
    model: Annotated[str, Field(pattern=r"^(MiniMax-H3|MiniMax-H3-Max)$")] = "MiniMax-H3"
    prompt: str
    duration: int = Field(ge=4, le=15, default=10)
    ratio: Optional[str] = None  # null means "not supplied"
    resolution: Annotated[str, Field(pattern=r"^(480P|768P|2K)$")] = "768P"
    ref_images: list[dict[str, Any]] = Field(default_factory=list)
    ref_videos: list[dict[str, Any]] = Field(default_factory=list)
    ref_audios: list[dict[str, Any]] = Field(default_factory=list)
    wait: bool = False
    use_context_ir: bool = True  # v0.1.3b: default ON, graceful fallback on failure


class ConfigBody(BaseModel):
    api_key: Annotated[str, Field(min_length=1, max_length=512)]


def _validate_mode(body: CreateBody) -> None:
    mode = body.mode
    ratio = body.ratio

    if mode == "t2v":
        if not ratio or ratio == "adaptive":
            raise HTTPException(
                status_code=400,
                detail="t2v mode requires a non-adaptive ratio",
            )
        if body.ref_images or body.ref_videos or body.ref_audios:
            raise HTTPException(
                status_code=400,
                detail="t2v mode does not accept reference media",
            )

    elif mode == "i2v":
        if ratio and ratio != "adaptive":
            raise HTTPException(
                status_code=400,
                detail="i2v mode forces ratio=adaptive; do not supply a ratio",
            )
        if len(body.ref_images) != 1:
            raise HTTPException(
                status_code=400,
                detail="i2v mode requires exactly one reference image",
            )
        first_ref = body.ref_images[0]
        if first_ref.get("role") != "first_frame":
            raise HTTPException(
                status_code=400,
                detail='i2v reference image must have role="first_frame"',
            )
        if body.ref_videos:
            raise HTTPException(
                status_code=400,
                detail="i2v mode does not accept reference videos",
            )

    elif mode == "r2v":
        has_ref = bool(body.ref_images or body.ref_videos or body.ref_audios)
        if not has_ref:
            raise HTTPException(
                status_code=400,
                detail="r2v mode requires at least one reference (image, video, or audio)",
            )

    # Model-specific parameter validation (resolution, duration). Raises
    # HTTPException(400) with a user-friendly message.
    try:
        validate_model_params(
            body.model, resolution=body.resolution, duration=body.duration
        )
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _submit_task(client: MiniMaxClient, body: CreateBody) -> tuple[str, list[dict[str, Any]]]:
    """Build content[] and submit. Returns (task_id, content_items)."""
    content: list[dict[str, Any]] = []

    # Text item
    content.append({"type": "text", "text": body.prompt})

    # Reference images
    for ref in body.ref_images:
        path = ref.get("path") or ""
        role = ref.get("role") or "reference_image"
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = await resolve_reference(client, path)
        content.append({"type": "image_url", "url": url, "role": role})

    # Reference videos — may need splitting
    for ref in body.ref_videos:
        path = ref.get("path") or ""
        clips_spec: Optional[str] = ref.get("clips")
        clips = await prepare_reference_video(Path(path), clips_spec=clips_spec)
        for clip in clips:
            if path.startswith("http://") or path.startswith("https://"):
                clip_url = path
            else:
                clip_url = await resolve_reference(client, clip.path)
            content.append({"type": "video_url", "url": clip_url, "duration": clip.duration_seconds})

    # Reference audios
    for ref in body.ref_audios:
        path = ref.get("path") or ""
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = await resolve_reference(client, path)
        content.append({"type": "audio_url", "url": url})

    # Determine ratio
    if body.mode == "i2v":
        ratio = "adaptive"
    else:
        ratio = body.ratio or "adaptive"

    task_id = await client.create_video_task(
        model=body.model,
        content=content,
        duration=body.duration,
        resolution=body.resolution,
        ratio=ratio,
    )
    return task_id, content


async def _download_result(
    client: MiniMaxClient,
    cfg: Config,
    task_id: str,
    final_task: dict[str, Any],
) -> None:
    """Download the result MP4 to cfg.videos_dir / {task_id}.mp4."""
    content = final_task.get("content") or []
    if not content:
        return
    first = content[0]
    url: Optional[str] = None
    if isinstance(first, dict):
        url = first.get("url")
    if not url:
        return

    videos_dir = Path(cfg.videos_dir)
    videos_dir.mkdir(parents=True, exist_ok=True)
    dest = videos_dir / f"{task_id}.mp4"
    tmp = videos_dir / f"{task_id}.mp4.tmp"

    await client.download_video(url, tmp)
    tmp.replace(dest)


# ====================================================== v0.1.3b prepare / commit helpers

async def _maybe_enhance_prompt(
    body: CreateBody,
    content: list[dict[str, Any]],
    client: MiniMaxClient,
    warnings: list[str],
) -> tuple[str, bool, bool, Optional[str]]:
    """Rewrite the user's prompt through H3-Context-IR. Falls back gracefully.

    Returns
    -------
    (final_prompt, used_context_ir, fallback_occurred, error_msg)
        * final_prompt         — what we'll actually send to H3 (Context-IR output or original)
        * used_context_ir      — True iff Context-IR succeeded and we used its rewrite
        * fallback_occurred    — True iff we tried Context-IR and fell back to the original
        * error_msg            — short failure reason when fallback_occurred, else None
    """
    if not getattr(body, "use_context_ir", True):
        return body.prompt, False, False, None
    try:
        # Re-use the existing content[] for the IR request. The first element
        # is always the text prompt; later elements are reference media.
        ir_content = [{"type": "text", "text": body.prompt}] + list(content[1:])
        ir_task_id = await client.create_context_ir_task(
            model=body.model if body.model == "MiniMax-H3" else "MiniMax-H3",
            content=ir_content,
            duration=body.duration,
            ratio=body.ratio or "adaptive",
        )
        enhanced = await client.fetch_context_ir_prompt(ir_task_id, max_wait_seconds=180.0)
        log.info("Context-IR: rewrote prompt for %s", body.mode)
        return enhanced, True, False, None
    except (MiniPicError, asyncio.TimeoutError, OSError) as e:
        msg = (
            f"Context-IR 改写失败 ({e.__class__.__name__}: {e})，"
            f"已 fallback 用原 prompt。\n"
            f"提示: Context-IR 通常需要 pay-as-you-go 类型的 API key，"
            f"TokenPlan 不支持 Context-IR。"
        )
        log.warning("Context-IR fallback: %s", msg)
        warnings.append(msg)
        return body.prompt, False, True, str(e)


async def _build_plan(body: CreateBody, cfg: Config) -> dict[str, Any]:
    """Build a complete H3 plan without submitting. Returns the plan dict.

    Multi-segment handling
    ----------------------
    Reference videos longer than ``REF_VIDEO_MAX_DURATION`` (15s) are split
    into N segments via ``plan_segments`` and physically cut into per-segment
    mp4 files via ``extract_reference_clips``. Each clip is uploaded to the
    CDN; the resulting ``content[]`` contains N ``video_url`` entries (one
    per segment). ``segments`` is the per-segment metadata for the UI.

    Context-IR
    ----------
    The user's natural-language prompt is sent through H3-Context-IR first
    (default ON). If Context-IR fails (TokenPlan 2013, network, etc.) the
    original prompt is used and a warning is added to the plan.
    """
    warnings: list[str] = []
    segments_meta: list[dict[str, Any]] = []
    content: list[dict[str, Any]] = []

    async with MiniMaxClient(cfg) as client:
        # 1) Resolve references first so we know what the content array looks like
        # Text placeholder — replaced by enhanced (or original) prompt below
        content.append({"type": "text", "text": body.prompt})

        for ref in body.ref_images:
            path = ref.get("path") or ""
            role = ref.get("role") or "reference_image"
            if path.startswith("http://") or path.startswith("https://"):
                url = path
            else:
                url = await resolve_reference(client, path)
            content.append({"type": "image_url", "url": url, "role": role})

        for ref in body.ref_videos:
            path = ref.get("path") or ""
            if not path:
                warnings.append("skipping empty ref_videos entry")
                continue
            if path.startswith("http://") or path.startswith("https://"):
                # Remote URL — cannot split, just append
                content.append({"type": "video_url", "url": path})
                continue
            local_path = Path(path)
            if not local_path.is_file():
                raise MediaError(f"reference video not found: {path}")
            duration = ffprobe_duration(local_path)
            if duration <= REF_VIDEO_MAX_DURATION + 0.05:
                # Short: use the original file as-is
                url = await resolve_reference(client, local_path)
                content.append({"type": "video_url", "url": url, "duration": int(duration)})
            else:
                # Long: split into N segments and upload each
                boundaries = find_shot_boundaries(local_path)
                segments = plan_segments(duration, boundaries)
                clip_paths = await extract_reference_clips(local_path, segments)
                for i, clip_path in enumerate(clip_paths):
                    url = await resolve_reference(client, clip_path)
                    content.append({
                        "type": "video_url",
                        "url": url,
                        "duration": segments[i].duration_seconds,
                    })
                    segments_meta.append({
                        "index": i,
                        "start_seconds": segments[i].start_seconds,
                        "duration_seconds": segments[i].duration_seconds,
                        "ref_clip_path": str(clip_path),
                    })
                total = sum(s.duration_seconds for s in segments)
                if abs(total - duration) > 1.0:
                    warnings.append(
                        f"总输出 {total}s 比参考 {duration:.1f}s "
                        f"{'长' if total > duration else '短'} {abs(total - duration):.2f}s"
                    )

        for ref in body.ref_audios:
            path = ref.get("path") or ""
            if not path:
                continue
            if path.startswith("http://") or path.startswith("https://"):
                url = path
            else:
                url = await resolve_reference(client, path)
            content.append({"type": "audio_url", "url": url})

        # 2) Context-IR — rewrite the prompt (or fall back). Must run AFTER
        #    reference resolution so the IR request can include the same media.
        final_prompt, used_ir, fallback, ir_error = await _maybe_enhance_prompt(
            body, content, client, warnings
        )
        content[0]["text"] = final_prompt

    return {
        "content": content,
        "segments": segments_meta,
        "warnings": warnings,
        "original_prompt": body.prompt,
        "enhanced_prompt": final_prompt,
        "used_context_ir": used_ir,
        "context_ir_fallback": fallback,
        "context_ir_error": ir_error,
    }


async def _run_multi_segment(
    parent_id: str,
    body: dict[str, Any],
    content: list[dict[str, Any]],
    segments_meta: list[dict[str, Any]],
    cfg: Config,
) -> None:
    """Background: submit N H3 children, poll, download, concat into final video.

    Each child is recorded in ``tasks`` with mode=r2v and
    ``extra.parent_task_id = parent_id``. The parent task is updated in-place
    with the list of child results under ``extra.segments``.

    Status mapping for the parent
    -----------------------------
    * All children succeed          → ``succeeded``
    * Some succeed, some fail       → ``partial`` (still concat the good ones)
    * All children fail             → ``failed``
    * Any uncaught exception        → ``failed`` with error=traceback excerpt
    """
    parent_extra = {
        "prepare_id": parent_id,  # placeholder; replaced below from TaskRecord
        "segments": [],
        "total_duration_seconds": None,
    }
    # Pull any extra fields the API endpoint already stored so we don't lose them
    existing = get_task(parent_id)
    if existing is not None and existing.extra:
        parent_extra.update(existing.extra)
    try:
        async with MiniMaxClient(cfg) as client:
            child_outputs: list[Optional[Path]] = []
            child_results: list[dict[str, Any]] = []
            child_iter: list[Optional[dict[str, Any]]] = (
                segments_meta if segments_meta else [None]
            )
            videos_dir = Path(cfg.videos_dir)
            videos_dir.mkdir(parents=True, exist_ok=True)

            for i, seg in enumerate(child_iter):
                child_id = "task-" + uuid.uuid4().hex[:12]
                seg_index = seg["index"] if seg is not None else 0
                seg_duration = seg["duration_seconds"] if seg is not None else body["duration"]
                seg_output = videos_dir / f"{parent_id}_seg{i:02d}.mp4"

                # Insert the child row up-front so the UI can show progress
                child_rec = TaskRecord(
                    task_id=child_id,
                    mode=body.get("mode", "r2v"),
                    prompt_excerpt=(body.get("prompt") or "")[:100],
                    status="submitted",
                    extra={
                        "parent_task_id": parent_id,
                        "segment_index": seg_index,
                    },
                )
                insert_task(child_rec)

                try:
                    h3_task_id = await client.create_video_task(
                        model=body.get("model", "MiniMax-H3"),
                        content=content,
                        duration=int(seg_duration),
                        resolution=body.get("resolution", "768P"),
                        ratio=(
                            "adaptive" if body.get("mode") == "i2v"
                            else (body.get("ratio") or "adaptive")
                        ),
                    )
                    update_task(
                        child_id,
                        status="submitted",
                        output_path=str(seg_output),
                    )
                    log.info("multi[%s] child %d (%ds) submitted as %s",
                             parent_id, seg_index, seg_duration, h3_task_id)
                    final = await poll_until_done(
                        client, h3_task_id, max_wait_seconds=30 * 60,
                    )
                    api_status = final.get("status", "Success")
                    # Download the result
                    final_content = final.get("content") or []
                    video_url: Optional[str] = None
                    for entry in final_content:
                        if isinstance(entry, dict):
                            video_url = (
                                entry.get("video_url", {}).get("url")
                                or entry.get("url")
                            )
                            if video_url:
                                break
                    if api_status == "Success" and video_url:
                        await client.download_video(video_url, seg_output)
                        update_task(child_id, status="Success",
                                    output_path=str(seg_output))
                        child_outputs.append(seg_output)
                    else:
                        update_task(child_id, status="Failed", error=f"H3 status: {api_status}")
                        child_outputs.append(None)
                    child_results.append({
                        "task_id": child_id,
                        "index": seg_index,
                        "duration_seconds": seg_duration,
                        "status": api_status,
                        "h3_task_id": h3_task_id,
                        "output_path": str(seg_output) if seg_output.is_file() else None,
                    })
                except (TaskError, MiniPicError) as e:
                    log.warning("multi[%s] child %d failed: %s", parent_id, seg_index, e)
                    update_task(child_id, status="Failed", error=str(e))
                    child_outputs.append(None)
                    child_results.append({
                        "task_id": child_id,
                        "index": seg_index,
                        "duration_seconds": seg_duration,
                        "status": "Failed",
                        "error": str(e),
                    })
                # Update the parent after each child so the UI can show progress
                parent_extra["segments"] = child_results
                parent_extra["total_duration_seconds"] = (
                    sum(c["duration_seconds"] for c in child_results)
                )
                update_task(
                    parent_id,
                    status="processing",
                    last_query_at=int(time.time() * 1000),
                )
                # Write the parent extra into storage via update_task (no helper for extra)
                # Use a direct sqlite UPDATE through the storage module
                _update_task_extra(parent_id, parent_extra)

            # Concat the successful children
            good = [p for p in child_outputs if p is not None]
            final_path = videos_dir / f"{parent_id}.mp4"
            if not good:
                update_task(parent_id, status="failed", error="all segments failed")
                _update_task_extra(parent_id, parent_extra)
                return
            if len(good) == 1:
                # Single success — promote to the final file name
                good[0].replace(final_path)
            else:
                _media_module.concat_videos(good, final_path)
                # Clean up intermediate _segNN files (success path only)
                for p in good:
                    if p.exists() and p != final_path:
                        p.unlink(missing_ok=True)

            success_count = sum(1 for c in child_results if c.get("status") == "Success")
            if success_count == len(child_results):
                status = "succeeded"
            elif success_count > 0:
                status = "partial"
            else:
                status = "failed"
            update_task(
                parent_id, status=status,
                output_path=str(final_path),
                last_query_at=int(time.time() * 1000),
            )
            _update_task_extra(parent_id, parent_extra)
    except Exception as e:  # noqa: BLE001
        log.exception("multi-segment parent %s failed unexpectedly", parent_id)
        update_task(parent_id, status="failed", error=f"unexpected: {e}")
        _update_task_extra(parent_id, parent_extra)


def _update_task_extra(task_id: str, extra: dict[str, Any]) -> None:
    """Direct UPDATE on tasks.extra for fields the public update_task doesn't expose."""
    import sqlite3
    from .storage import db_path
    with sqlite3.connect(str(db_path()), timeout=10.0) as c:
        c.execute(
            "UPDATE tasks SET extra = ? WHERE task_id = ?",
            (json.dumps(extra, ensure_ascii=False), task_id),
        )
        c.commit()


def _record_to_dict(rec: TaskRecord) -> dict[str, Any]:
    """Serialize a TaskRecord for the API, expanding multi-segment children."""
    out: dict[str, Any] = {
        "task_id": rec.task_id,
        "mode": rec.mode,
        "prompt_excerpt": rec.prompt_excerpt,
        "status": rec.status,
        "submitted_at": rec.submitted_at,
        "output_path": rec.output_path,
        "error": rec.error,
        "extra": rec.extra,
    }
    # Expand child metadata for multi-segment parents
    if rec.mode == "multi" and rec.extra.get("segments"):
        children: list[dict[str, Any]] = []
        for seg in rec.extra["segments"]:
            child = get_task(seg["task_id"])
            if child is not None:
                children.append(_record_to_dict(child))
        out["children"] = children
    return out


def _detect_key_source(cfg: Config) -> str:
    """Best-effort: report which config layer the live key came from.

    Priority matches ``load_config()``: env > local config.json > user config
    dir. Returns "none" when no key is configured anywhere.
    """
    if os.environ.get("MINIMAX_API_KEY", "").strip():
        return "env"
    if cfg.api_key:
        # Both local and user dir could host the key; we report whichever the
        # file system currently has. The order mirrors load_config (local
        # wins over user), so check local first.
        local_path = Path.cwd() / "config.json"
        if local_path.is_file():
            try:
                data = json.loads(local_path.read_text(encoding="utf-8"))
                if (data.get("api_key") or "").strip():
                    return "local"
            except (OSError, ValueError):
                pass
        user_path = _user_config_path()
        if user_path.is_file():
            try:
                data = json.loads(user_path.read_text(encoding="utf-8"))
                if (data.get("api_key") or "").strip():
                    return "user"
            except (OSError, ValueError):
                pass
    return "none"


# ---------------------------------------------------------------------------
# Module-level app (lazy; reads config on first request)
# ---------------------------------------------------------------------------

app: FastAPI = create_app()


# ---------------------------------------------------------------------------
# Direct-run entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("minipic.web:app", host="127.0.0.1", port=8765, reload=False)
