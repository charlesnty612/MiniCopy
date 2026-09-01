"""FastAPI web UI for minipic."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import Config, load_config, require_api_key
from .client import MiniMaxClient
from .errors import ConfigError, MiniPicError, TaskError
from .media import prepare_reference_video, resolve_reference
from .poller import poll_until_done, STATUS_SUCCEEDED
from .storage import TaskRecord, get_task, insert_task, list_tasks, update_task

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
    @app.post("/api/create")
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

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class CreateBody(BaseModel):
    mode: Annotated[str, Field(pattern=r"^(t2v|i2v|r2v)$")]
    prompt: str
    duration: int = Field(ge=4, le=15, default=10)
    ratio: Optional[str] = None  # null means "not supplied"
    resolution: Annotated[str, Field(pattern=r"^(768P|2K)$")] = "768P"
    ref_images: list[dict[str, Any]] = Field(default_factory=list)
    ref_videos: list[dict[str, Any]] = Field(default_factory=list)
    ref_audios: list[dict[str, Any]] = Field(default_factory=list)
    wait: bool = False


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
        model="MiniMax-H3",
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


def _record_to_dict(rec: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": rec.task_id,
        "mode": rec.mode,
        "prompt_excerpt": rec.prompt_excerpt,
        "status": rec.status,
        "submitted_at": rec.submitted_at,
        "output_path": rec.output_path,
        "error": rec.error,
        "extra": rec.extra,
    }


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
