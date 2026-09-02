"""FastAPI web UI for minipic."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
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
    user_data_path,
    validate_model_modes,
    validate_model_params,
)
from .client import MiniMaxClient
from .errors import ConfigError, MiniPicError, TaskError
from .media import (
    _extract_video_url,
    build_content,
    download_task_result,
    ffprobe_duration,
    resolve_reference,
)
from .poller import TERMINAL_STATES, poll_until_done
from .storage import (
    TaskRecord,
    get_task,
    insert_task,
    list_tasks,
    update_task,
)
from minipic import __version__

log = logging.getLogger(__name__)


def _web_dir() -> Path:
    """定位 web/ 静态资源目录。

    源码运行：项目根下的 web/。
    PyInstaller 冻结后：web/ 通过 --add-data 打进 bundle，运行时根为 sys._MEIPASS。
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "web"
    return Path(__file__).parent.parent.parent / "web"


# ---------------------------------------------------------------------------
# v0.1.4+ — upload allow-list + per-kind size caps (对齐 MiniMax V2 API 文档)
# ---------------------------------------------------------------------------
# Per-kind caps come from the official MiniMax video-generation V2 schema:
#   image ≤30MB, video ≤50MB, audio ≤15MB. The previous global MAX_UPLOAD_BYTES
#   of 50MB was too loose for images/audio (over the per-kind cap) and too tight
#   for video (matches). Per-kind caps are applied in api_upload below.
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_AUDIO_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_UPLOAD_BYTES = max(MAX_IMAGE_BYTES, MAX_AUDIO_BYTES, MAX_VIDEO_BYTES)
_ALLOWED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp",
                       "image/heic", "image/heif"}
_ALLOWED_VIDEO_MIME = {"video/mp4", "video/quicktime"}
_ALLOWED_AUDIO_MIME = {"audio/mpeg", "audio/wav"}
_ALLOWED_MIME = _ALLOWED_IMAGE_MIME | _ALLOWED_VIDEO_MIME | _ALLOWED_AUDIO_MIME
_MIME_TO_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
    "image/heic": "heic", "image/heif": "heif",
    "video/mp4": "mp4", "video/quicktime": "mov",
    "audio/mpeg": "mp3", "audio/wav": "wav",
}
_MIME_TO_KIND = {
    "image/png": "image", "image/jpeg": "image", "image/webp": "image",
    "image/heic": "image", "image/heif": "image",
    "video/mp4": "video", "video/quicktime": "video",
    "audio/mpeg": "audio", "audio/wav": "audio",
}
_KIND_MAX_BYTES = {
    "image": MAX_IMAGE_BYTES,
    "video": MAX_VIDEO_BYTES,
    "audio": MAX_AUDIO_BYTES,
}
_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1MB streaming chunks

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
        web_dir = _web_dir()
        return FileResponse(web_dir / "index.html", media_type="text/html")

    # ------------------------------------------------- static assets (web/assets)
    from fastapi.staticfiles import StaticFiles

    assets_dir = _web_dir() / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

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
                task_id, _content_items, used_context_ir = await _submit_task(client, body)

                extra: dict[str, Any] = {}
                if used_context_ir:
                    extra["used_context_ir"] = True

                rec = TaskRecord(
                    task_id=task_id, mode=body.mode,
                    prompt_excerpt=body.prompt[:100], status="submitted",
                    extra=extra,
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
    async def api_list_tasks() -> list[dict[str, Any]]:
        cfg = app.state.cfg
        out: list[dict[str, Any]] = []
        for rec in list_tasks(limit=50):
            if rec.status not in TERMINAL_STATES:
                # Non-terminal: best-effort refresh from API. On failure keep
                # the local snapshot so the UI still shows *something*.
                try:
                    refreshed = await _refresh_task_from_api(cfg, rec)
                except Exception:  # noqa: BLE001
                    refreshed = _record_to_dict(rec)
                out.append(refreshed)
            else:
                out.append(_record_to_dict(rec))
        return out

    # ---------------------------------------------------------------- GET /api/tasks/{task_id}
    @app.get("/api/tasks/{task_id}")
    async def api_get_task(task_id: str) -> dict[str, Any]:
        cfg = app.state.cfg
        rec = get_task(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")

        # If task is non-terminal, try to refresh from API. Terminal states
        # are read straight from the local DB — no MiniMax round-trip.
        if rec.status not in TERMINAL_STATES:
            try:
                return await _refresh_task_from_api(cfg, rec)
            except Exception:  # noqa: BLE001
                return _record_to_dict(rec)

        return _record_to_dict(rec)

    # -------------------------------------------------------- GET/POST /api/tasks/{task_id}/download
    @app.api_route("/api/tasks/{task_id}/download", methods=["GET", "POST"])
    async def api_download_task(task_id: str) -> Response:
        cfg = app.state.cfg
        rec = get_task(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")

        videos_dir = Path(cfg.videos_dir)
        video_path = videos_dir / f"{task_id}.mp4"
        if not video_path.is_file():
            # On-demand download: task succeeded but the video was never
            # fetched locally (e.g. wait=false submissions). Query the API
            # for the result URL and pull it down. Downloading is free.
            try:
                require_api_key(cfg)
                async with MiniMaxClient(cfg) as client:
                    api_task = await client.query_video_task(task_id)
                    if api_task.get("status") not in ("Success", "Succeeded", "succeeded"):
                        raise HTTPException(
                            status_code=409,
                            detail=f"任务状态 {api_task.get('status')}，视频尚未生成完成，请稍后再试",
                        )
                    ok = await download_task_result(client, api_task, video_path)
                if not ok:
                    raise HTTPException(
                        status_code=404,
                        detail="任务已完成但响应中没有视频 URL，无法下载",
                    )
            except HTTPException:
                raise
            except MiniPicError as e:
                raise HTTPException(status_code=400, detail=f"下载失败: {e}")

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

        v0.2.0+: ``has_key`` / ``masked`` / ``source`` only reflect the
        **user config file** (the single source of truth). On a fresh launch
        with no user config — even if ``MINIMAX_API_KEY`` is set in the
        environment or the CLI's local config has a key — the UI reports
        ``has_key=False, source="none"``. This is intentional: the Web UI is
        the user-facing surface and only knows about keys the user saved
        through it; env / local keys belong to the CLI developer workflow.
        Once the user saves a key via POST /api/config, the next GET here
        reports ``has_key=True, source="user"`` and stays that way across
        process restarts.
        """
        # Read the user config file directly — do NOT trust app.state.cfg
        # because at startup it may already carry an env/local key in memory
        # (which the UI should not surface).
        user_path = _user_config_path()
        user_key: Optional[str] = None
        if user_path.is_file():
            try:
                data = json.loads(user_path.read_text(encoding="utf-8"))
                user_key = (data.get("api_key") or "").strip() or None
            except (OSError, ValueError):
                user_key = None

        return {
            "has_key": bool(user_key),
            "masked": mask_key(user_key),
            "source": "user" if user_key else "none",
            "user_config_path": str(user_path),
            "models": [
                {
                    "name": name,
                    "resolutions": MODEL_CONSTRAINTS[name]["resolutions"],
                    "duration_min": MODEL_CONSTRAINTS[name]["duration_min"],
                    "duration_max": MODEL_CONSTRAINTS[name]["duration_max"],
                    "modes": MODEL_CONSTRAINTS[name]["modes"],
                    "i2v_roles": MODEL_CONSTRAINTS[name]["i2v_roles"],
                }
                for name in API_MODELS
            ],
            "model_default": "MiniMax-H3",
            "version": __version__,
        }

    # -------------------------------------------------------- POST /api/config
    @app.post("/api/config")
    def api_set_config(payload: Annotated[ConfigBody, Body()]) -> dict[str, Any]:
        """Persist a new API key to the user config dir and update cfg in place.

        The user config file is the **single source of truth for the API key**
        (v0.2.0+). This handler writes it there and updates the in-memory
        cfg so the next /api/create uses the new key. The same file is read
        by ``load_config()`` (CLI + subsequent app restarts), so a key saved
        here is immediately visible to the CLI without any restart — because
        user > local > env precedence, the saved key wins even when the env
        var is set (env is only a fallback for files without a key).
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
            "source": "user",
        }

    # -------------------------------------------------------- GET /api/probe/video (v0.1.5)
    @app.get("/api/probe/video")
    def api_probe_video(
        path: Annotated[str, Query(min_length=1, description="Absolute path to a local video file.")],
    ) -> dict[str, Any]:
        """Probe a video file and return its duration in seconds.

        Used by the UI to auto-fill the duration input after the user picks a
        reference video via the file picker. The path is trusted to be a local
        absolute path the user already has on disk; this endpoint does not
        resolve remote URLs.

        Raises 404 if the file is missing or probe fails (e.g. corrupt).
        """
        p = Path(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"file not found: {path}")
        try:
            seconds = ffprobe_duration(p)
        except Exception as e:  # MediaError, JSON decode, etc.
            raise HTTPException(status_code=400, detail=f"probe failed: {e}") from e
        return {"duration": round(seconds, 2), "path": path}

    # -------------------------------------------------------- POST /api/upload (v0.1.4)
    @app.post("/api/upload")
    async def api_upload(
        request: Request, file: UploadFile = File(...)
    ) -> dict[str, Any]:
        """Accept a reference media file (image/video/audio) and persist it to disk.

        Hard guards
        -----------
        * MIME allow-list (7 types) — anything else → 400.
        * 50MB size cap — checked up-front via Content-Length when present,
          and re-checked while streaming. On overflow the partial file is
          unlinked and 400 is returned.
        * Server-side ``uuid.uuid4().hex`` filename — the client-supplied
          ``filename`` is **never** used as a disk name (path-traversal
          protection). Original name is intentionally discarded.

        Returns
        -------
        ``{path, kind, size, content_type, sha256}``. The ``path`` is an
        absolute filesystem path inside the user data dir's ``uploads/``
        folder; the caller (``buildPayload``) only stores it as a string
        and the backend later resolves it via ``resolve_reference``.
        """
        # Pre-flight: Content-Length header. Fast-fail large bodies without
        # allocating disk buffers. Header may be missing (chunked transfer),
        # in which case the streaming guard below still catches overruns.
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                cl_int = int(cl)
            except ValueError:
                cl_int = -1
            if cl_int > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"file too large: Content-Length={cl_int} > "
                        f"{MAX_UPLOAD_BYTES} (50MB cap)"
                    ),
                )

        ct = (file.content_type or "").lower()
        if ct not in _ALLOWED_MIME:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unsupported content_type: {ct or '<missing>'}; "
                    f"allowed: {sorted(_ALLOWED_MIME)}"
                ),
            )
        ext = _MIME_TO_EXT[ct]
        kind = _MIME_TO_KIND[ct]
        kind_cap = _KIND_MAX_BYTES[kind]

        uploads_dir = user_data_path("uploads")
        uploads_dir.mkdir(parents=True, exist_ok=True)
        target_name = f"{uuid.uuid4().hex}.{ext}"
        target = uploads_dir / target_name

        hasher = hashlib.sha256()
        written = 0
        try:
            with target.open("wb") as out:
                while True:
                    chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > kind_cap:
                        # Over per-kind cap — abort. Close first so we can
                        # unlink on Windows without "file in use" errors.
                        out.close()
                        try:
                            target.unlink(missing_ok=True)
                        except OSError:
                            pass
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"{kind} file too large: >{kind_cap} bytes "
                                f"(per-kind cap; image ≤30MB, audio ≤15MB, "
                                f"video ≤50MB)"
                            ),
                        )
                    hasher.update(chunk)
                    out.write(chunk)
        finally:
            await file.close()

        sha = hasher.hexdigest()
        log.info(
            "upload: saved %s (%s, %d bytes, sha256=%s)",
            target_name, ct, written, sha[:12],
        )
        return {
            "path": str(target.resolve()),
            "kind": kind,
            "size": written,
            "content_type": ct,
            "sha256": sha,
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
    use_context_ir: bool = True


class ConfigBody(BaseModel):
    api_key: Annotated[str, Field(min_length=1, max_length=512)]


def _validate_mode(body: CreateBody) -> None:
    """校验 CreateBody 对齐 MiniMax V2 API schema。

    对齐要点（v0.1.6+）：
      - i2v 支持首帧/尾帧两张图组合（role 各一），也支持单图；
      - i2v 与 r2v 互斥（reference_* 角色不得与 first/last_frame 混用）；
      - text ≤7000 字符、ref_images ≤9、first_frame ≤1、last_frame ≤1；
      - 角色是否被模型支持由 validate_model_modes 校验（H3/H3-Max
        已统一收紧为首/尾帧，middle_frame 一律拒绝）。
    """
    mode = body.mode
    ratio = body.ratio

    # ---------------- 全局上限（先做，与模式无关） ----------------
    if len(body.prompt) > 7000:
        raise HTTPException(
            status_code=400,
            detail="提示词超过 7000 字符上限",
        )
    if len(body.ref_images) > 9:
        raise HTTPException(
            status_code=400,
            detail="参考图最多 9 张",
        )
    first_count = sum(
        1 for r in body.ref_images if r.get("role") == "first_frame"
    )
    last_count = sum(
        1 for r in body.ref_images if r.get("role") == "last_frame"
    )
    if first_count > 1:
        raise HTTPException(
            status_code=400,
            detail="first_frame 最多 1 张",
        )
    if last_count > 1:
        raise HTTPException(
            status_code=400,
            detail="last_frame 最多 1 张",
        )

    # ---------------- 全局互斥校验（i2v 与 r2v 角色不可混用） ----------------
    has_first_or_last = first_count + last_count > 0
    has_reference = any(
        r.get("role") in {"reference_image", "reference_video", "reference_audio"}
        for r in body.ref_images
    )
    # ref_videos / ref_audios 在 r2v 分支里也会校验角色；
    # 这里只看 ref_images 一项就足够覆盖"图生视频与多模态参考互斥"。
    if has_first_or_last and has_reference:
        raise HTTPException(
            status_code=400,
            detail="图生视频（first_frame/last_frame）与多模态参考（reference_*）互斥，不可混用",
        )

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
        n_imgs = len(body.ref_images)
        if n_imgs not in (1, 2):
            raise HTTPException(
                status_code=400,
                detail="i2v mode requires 1 or 2 reference images "
                       "(first_frame optional last_frame)",
            )
        # 单图：role ∈ {first_frame, last_frame}（默认 first_frame）；
        # 双图：必须恰好一张 first_frame + 一张 last_frame，且无重复。
        if n_imgs == 1:
            i2v_role = body.ref_images[0].get("role") or "first_frame"
            try:
                validate_model_modes(body.model, mode="i2v", i2v_role=i2v_role)
            except ConfigError as e:
                raise HTTPException(status_code=400, detail=str(e))
        else:  # n_imgs == 2
            roles = sorted(
                (r.get("role") or "") for r in body.ref_images
            )
            if roles != ["first_frame", "last_frame"]:
                raise HTTPException(
                    status_code=400,
                    detail="i2v 双图必须一张 role=first_frame + 一张 role=last_frame",
                )
            # 角色都已合法，逐个校验模型支持（H3 与 H3-Max 均通过）。
            for r in body.ref_images:
                try:
                    validate_model_modes(
                        body.model, mode="i2v", i2v_role=r.get("role")
                    )
                except ConfigError as e:
                    raise HTTPException(status_code=400, detail=str(e))
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
        # 多模态参考能力由模型决定：H3 支持，H3-Max 不支持。
        try:
            validate_model_modes(body.model, mode="r2v")
        except ConfigError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # r2v 的图角色只能是 reference_image；first/last_frame 已在全局互斥里拦掉。
        for r in body.ref_images:
            role = r.get("role") or "reference_image"
            if role != "reference_image":
                raise HTTPException(
                    status_code=400,
                    detail=f"r2v mode only allows role=reference_image for images; got {role!r}",
                )
        # 视频/音频的角色默认即 reference_video / reference_audio，
        # 缺省时放行；显式给了其它角色也放行（兼容性最好，且服务端会再校验）。
        for r in body.ref_videos:
            role = r.get("role")
            if role is not None and role != "reference_video":
                raise HTTPException(
                    status_code=400,
                    detail=f"r2v mode only allows role=reference_video for videos; got {role!r}",
                )
        for r in body.ref_audios:
            role = r.get("role")
            if role is not None and role != "reference_audio":
                raise HTTPException(
                    status_code=400,
                    detail=f"r2v mode only allows role=reference_audio for audios; got {role!r}",
                )

    # Model-specific parameter validation (resolution, duration). Raises
    # HTTPException(400) with a user-friendly message.
    try:
        validate_model_params(
            body.model, resolution=body.resolution, duration=body.duration
        )
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _refresh_task_from_api(cfg: Config, rec: TaskRecord) -> dict[str, Any]:
    """Query the MiniMax API for ``rec.task_id``, persist updates, return the
    serialized dict enriched with ``api_status`` / ``usage`` / ``content_url``.

    Raises whatever the client raises — callers are expected to fall back to
    ``_record_to_dict(rec)`` on failure (graceful degradation: the UI still
    shows *something*).
    """
    result: dict[str, Any] = _record_to_dict(rec)
    require_api_key(cfg)
    async with MiniMaxClient(cfg) as client:
        api_task = await client.query_video_task(rec.task_id)
    status = api_task.get("status", "")
    update_task(
        rec.task_id, status=status, last_query_at=int(time.time() * 1000)
    )
    result["api_status"] = status
    # Persist usage (total_seconds etc.) so the UI can show cost.
    usage = api_task.get("usage")
    if usage:
        extra = dict(rec.extra or {})
        extra["usage"] = usage
        update_task(rec.task_id, extra=extra)
        result["usage"] = usage
    # Attach content URL if available (content may be a dict {"url": ...}
    # or a list — handled by media._extract_video_url).
    url = _extract_video_url(api_task)
    if url:
        result["content_url"] = url
    # Reflect the new status in the serialized record.
    result["status"] = status
    return result


async def _submit_task(client: MiniMaxClient, body: CreateBody) -> tuple[str, list[dict[str, Any]], bool]:
    """Build content[] and submit. Returns ``(task_id, content_items, used_context_ir)``.

    v0.1.5+: reference videos must be ≤15s (H3 per-call cap). prepare_reference_video
    raises MediaError on longer sources — surfaced as a 400 to the caller.

    The content[] shape (text flat, role-on-every-item, duration on video/audio
    clips) is owned by :func:`minipic.media.build_content` — keep this helper
    thin so CLI and Web cannot drift apart again.

    Context-IR (optional, default ON):
      H3 supports ``/v2/h3_context_ir`` which rewrites a brief into the
      6-section H3 prompt structure. We submit the original content (with
      uploaded references — already paid for) to Context-IR, swap the
      ``text`` element with the rewritten prompt, then submit the actual
      video generation task. ``H3-Max`` is unsupported and silently falls
      back to the original prompt (no error). Any failure (TimeoutError,
      ConfigError, network) also falls back silently — Context-IR is a
      nice-to-have, not a gate.
    """
    content = await build_content(
        prompt=body.prompt,
        client=client,
        ref_images=body.ref_images,
        ref_videos=body.ref_videos,
        ref_audios=body.ref_audios,
    )

    used_context_ir = False
    if body.use_context_ir and body.model == "MiniMax-H3":
        # i2v forces ratio=adaptive; the same applies to Context-IR, which
        # is just another H3 video path with no resolution requirement.
        ir_ratio = "adaptive" if body.mode == "i2v" else (body.ratio or "adaptive")
        try:
            ir_task_id = await client.create_context_ir_task(
                model=body.model,
                content=content,
                duration=body.duration,
                ratio=ir_ratio,
            )
            enhanced = await client.fetch_context_ir_prompt(ir_task_id)
            if enhanced and content and content[0].get("type") == "text":
                content[0]["text"] = enhanced
                used_context_ir = True
                log.info("context-ir rewrote prompt for %s", id(content))
        except Exception as e:  # noqa: BLE001 — graceful fallback
            log.warning("context-ir failed (%s), falling back to original prompt",
                        e.__class__.__name__)

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
    return task_id, content, used_context_ir


async def _download_result(
    client: MiniMaxClient,
    cfg: Config,
    task_id: str,
    final_task: dict[str, Any],
) -> None:
    """Download the result MP4 to cfg.videos_dir / {task_id}.mp4.

    Silent no-op when the task response carries no URL — that mirrors the
    legacy behaviour (the file simply isn't downloaded yet). The actual
    extraction + atomic download logic lives in
    :func:`minipic.media.download_task_result`.
    """
    videos_dir = Path(cfg.videos_dir)
    videos_dir.mkdir(parents=True, exist_ok=True)
    dest = videos_dir / f"{task_id}.mp4"
    await download_task_result(client, final_task, dest)


def _record_to_dict(rec: TaskRecord) -> dict[str, Any]:
    """Serialize a TaskRecord for the API."""
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


def _detect_key_source(cfg: Config) -> str:
    """Best-effort: report which config layer the live key actually came from.

    Priority (v0.2.0+) matches ``load_config()``: user config dir wins over
    local cwd config.json, which wins over the env var. Resolution order is
    user → local → env → none, where "env" only appears when no file source
    has a non-empty api_key. Returns "none" when no key is configured
    anywhere.

    Note: the Web UI's ``GET /api/config`` does NOT use this function — it
    reads the user config file directly so the UI surfaces only the keys
    the user has saved through it. This helper remains for callers that
    want a single-string label for the *live* key (used by direct CLI /
    programmatic inspection and by tests).
    """
    # 1. user config dir wins (UI-saved key persists across restarts).
    user_path = _user_config_path()
    if user_path.is_file():
        try:
            data = json.loads(user_path.read_text(encoding="utf-8"))
            if (data.get("api_key") or "").strip():
                return "user"
        except (OSError, ValueError):
            pass
    # 2. local cwd config.json beats env.
    local_path = Path.cwd() / "config.json"
    if local_path.is_file():
        try:
            data = json.loads(local_path.read_text(encoding="utf-8"))
            if (data.get("api_key") or "").strip():
                return "local"
        except (OSError, ValueError):
            pass
    # 3. env var — only acts as a fallback when no file has a key.
    if os.environ.get("MINIMAX_API_KEY", "").strip():
        return "env"
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

    # Fixed port, same as the CLI `web` command (minipic.cli.WEB_PORT).
    uvicorn.run("minipic.web:app", host="127.0.0.1", port=7860, reload=False)