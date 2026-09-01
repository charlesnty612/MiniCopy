"""Click-based CLI for minipic."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Annotated, Optional

import click

from . import web as _web_module
from .config import (
    API_MODELS,
    Config,
    ensure_dirs,
    load_config,
    require_api_key,
    save_config,
    validate_model_params,
)
from .client import MiniMaxClient
from .errors import ConfigError, MiniPicError
from .media import resolve_reference
from .poller import (
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    poll_until_done,
)
from .prompt import H3Prompt
from .storage import (
    PREPARE_STATUS_COMMITTED,
    PREPARE_STATUS_PREPARED,
    PREPARE_TTL_MS,
    PrepareRecord,
    TaskRecord,
    delete_prepare,
    get_prepare,
    get_task,
    init_db,
    insert_prepare,
    insert_task,
    list_prepares,
    list_tasks,
    update_prepare_status,
    update_task,
)
from .web import CreateBody, _build_plan, _run_multi_segment, _validate_mode

# ---------------------------------------------------------------- try rich

try:
    from rich.console import Console
    from rich.table import Table

    _console = Console()
except Exception:  # noqa: BLE001
    _console = None  # type: ignore[assignment]


def _print(*args: object, **kwargs: object) -> None:
    if _console is not None:
        _console.print(*args, **kwargs)
    else:
        print(*args, **kwargs)


def _print_json(data: object) -> None:
    if _console is not None:
        # rich.console.print_json expects a Python object, not a pre-serialized
        # string. Passing a JSON string makes rich print the string with quotes
        # around it instead of a pretty tree.
        _console.print_json(data=data)
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- helpers


def _atomic_download(client: MiniMaxClient, url: str, dest: Path) -> None:
    """Download to .part then atomic-rename."""
    tmp = dest.with_suffix(".part")
    # ensure parent exists
    dest.parent.mkdir(parents=True, exist_ok=True)
    # remove stale .part if present
    if tmp.exists():
        tmp.unlink()
    # run sync download in thread pool
    asyncio.run(client.download_video(url, tmp))
    tmp.rename(dest)


def _status_label(status: str) -> str:
    """Map raw status string to human label with emoji."""
    mapping = {
        STATUS_SUCCEEDED: "[green]✓ Success[/green]",
        STATUS_FAILED: "[red]✗ Failed[/red]",
        STATUS_CANCELLED: "[yellow]⊘ Cancelled[/yellow]",
        STATUS_EXPIRED: "[yellow]⊘ Expired[/yellow]",
        STATUS_RUNNING: "[cyan]◐ Processing[/cyan]",
        STATUS_QUEUED: "[cyan]◔ Queued[/cyan]",
    }
    return mapping.get(status, status)


def _print_task_record(rec: TaskRecord) -> None:
    """Print a single TaskRecord in a human-friendly way."""
    ts = rec.submitted_at / 1000
    try:
        import datetime

        dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        dt = str(ts)
    _print(
        f"[bold]{rec.task_id}[/bold]  {_status_label(rec.status)}  [{rec.mode}]  {dt}"
    )
    _print(f"  prompt: {rec.prompt_excerpt[:80]}")
    if rec.output_path:
        _print(f"  output: {rec.output_path}")
    if rec.error:
        _print(f"  [red]error: {rec.error}[/red]")


# ---------------------------------------------------------------- config group


@click.group(name="config")
def config_group() -> None:
    """Read and write configuration."""
    pass


@config_group.command("show")
def config_show() -> None:
    """Print the current effective configuration."""
    cfg = load_config()
    _print_json(cfg.to_dict())


@config_group.command("set")
@click.argument("key")
@click.argument("value")
@click.option(
    "--scope",
    type=click.Choice(["user", "local"], case_sensitive=False),
    default="user",
    help='Write to user config dir (default) or ./config.json ("local").',
)
def config_set(key: str, value: str, scope: str) -> None:
    """Persist a config key/value pair."""
    cfg = load_config()
    # Apply to a dataclass
    if not hasattr(cfg, key):
        _print(f"[red]Unknown config key: {key}[/red]")
        raise SystemExit(1)
    # Type coercion
    field_val = cfg.__dataclass_fields__[key]
    current = getattr(cfg, key)
    if isinstance(current, bool):
        coerced = value.lower() in ("true", "1", "yes")
    elif isinstance(current, int):
        try:
            coerced = int(value)
        except ValueError:
            _print(f"[red]Expected int for {key}, got {value!r}[/red]")
            raise SystemExit(1)
    elif isinstance(current, float):
        try:
            coerced = float(value)
        except ValueError:
            _print(f"[red]Expected float for {key}, got {value!r}[/red]")
            raise SystemExit(1)
    else:
        coerced = value

    setattr(cfg, key, coerced)
    path = save_config(cfg, scope=scope)
    _print(f"Saved {key}={value!r} to {path}")


# ---------------------------------------------------------------- create group


@click.group(name="create")
def create_group() -> None:
    """Create a video generation task."""
    pass


# ---------- t2v


@create_group.command(name="t2v")
@click.option("--prompt", required=True, help="H3 prompt text (used as-is).")
@click.option(
    "--duration", type=int, default=None, help="Video duration in seconds."
)
@click.option(
    "--ratio",
    required=True,
    help="Aspect ratio, e.g. 16:9 or 9:16. Must not be 'adaptive'.",
)
@click.option(
    "--resolution",
    default="768P",
    help="Output resolution. H3 V2 officially supports 768P (default) and 2K.",
)
@click.option(
    "--model",
    "model",
    type=click.Choice(API_MODELS, case_sensitive=False),
    default="MiniMax-H3",
    help="Generation model. Default: MiniMax-H3.",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Block until the task finishes and download the result.",
)
def create_t2v(
    prompt: str,
    duration: Optional[int],
    ratio: str,
    resolution: str,
    model: str,
    wait: bool,
) -> None:
    """Text-to-video: pure text prompt → video."""
    if ratio == "adaptive":
        _print("[red]--ratio may not be 'adaptive' for t2v.[/red]")
        raise SystemExit(1)

    cfg = load_config()
    require_api_key(cfg)

    ensure_dirs()
    init_db()

    duration_val = duration if duration is not None else cfg.default_duration

    try:
        validate_model_params(model, resolution=resolution, duration=duration_val)
    except ConfigError as e:
        _print(f"[red]{e}[/red]")
        raise SystemExit(1)

    content: list[dict] = [{"type": "text", "text": {"text": prompt}}]

    async def _run() -> None:
        async with MiniMaxClient(cfg) as client:
            task_id = await client.create_video_task(
                model=model,
                content=content,
                duration=duration_val,
                resolution=resolution,
                ratio=ratio,
            )
            _print(f"[green]Task submitted: {task_id}[/green]")
            rec = TaskRecord(
                task_id=task_id,
                mode="t2v",
                prompt_excerpt=prompt[:100],
            )
            insert_task(rec)
            update_task(task_id, status="submitted")

            if wait:
                await _wait_and_download(client, task_id, cfg)

    asyncio.run(_run())


# ---------- i2v


@create_group.command(name="i2v")
@click.option("--prompt", required=True, help="H3 prompt text (used as-is).")
@click.option(
    "--ref-image",
    required=True,
    help="Path or HTTPS URL for the first frame.",
)
@click.option(
    "--ref-image-role",
    default="first_frame",
    help="Role of the reference image. Default: first_frame.",
)
@click.option(
    "--duration", type=int, default=None, help="Video duration in seconds."
)
@click.option(
    "--resolution",
    default="768P",
    help="Output resolution. H3 V2 officially supports 768P (default) and 2K.",
)
@click.option(
    "--model",
    "model",
    type=click.Choice(API_MODELS, case_sensitive=False),
    default="MiniMax-H3",
    help="Generation model. Default: MiniMax-H3.",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Block until the task finishes and download the result.",
)
def create_i2v(
    prompt: str,
    ref_image: str,
    ref_image_role: str,
    duration: Optional[int],
    resolution: str,
    model: str,
    wait: bool,
) -> None:
    """Image-to-video: first-frame image + text prompt → video."""
    cfg = load_config()
    require_api_key(cfg)

    ensure_dirs()
    init_db()

    duration_val = duration if duration is not None else cfg.default_duration

    try:
        validate_model_params(model, resolution=resolution, duration=duration_val)
    except ConfigError as e:
        _print(f"[red]{e}[/red]")
        raise SystemExit(1)

    async def _run() -> None:
        async with MiniMaxClient(cfg) as client:
            image_url = await resolve_reference(client, ref_image)
            content: list[dict] = [
                {"type": "text", "text": {"text": prompt}},
                {
                    "type": "image_url",
                    "image_url": {"url": image_url},
                    "role": ref_image_role,
                },
            ]
            task_id = await client.create_video_task(
                model=model,
                content=content,
                duration=duration_val,
                resolution=resolution,
                ratio="adaptive",
            )
            _print(f"[green]Task submitted: {task_id}[/green]")
            rec = TaskRecord(
                task_id=task_id,
                mode="i2v",
                prompt_excerpt=prompt[:100],
            )
            insert_task(rec)
            update_task(task_id, status="submitted")

            if wait:
                await _wait_and_download(client, task_id, cfg)

    asyncio.run(_run())


# ---------- r2v


@create_group.command(name="r2v")
@click.option("--prompt", required=True, help="H3 prompt text (used as-is).")
@click.option(
    "--ref-image",
    multiple=True,
    help='Reference image as "path_or_url[:role]". Role defaults to "reference_image". '
    "May be repeated.",
)
@click.option(
    "--ref-video",
    multiple=True,
    help='Reference video as "path[:role]". Role defaults to "reference_video". '
    "May be repeated.",
)
@click.option(
    "--ref-video-clips",
    default=None,
    help='Clip extraction spec applied to every --ref-video source. '
    'Format: "start:duration,start:duration,..." (seconds, may be float). '
    "If a source video is ≤ 15s this spec is ignored. "
    "Each extracted clip becomes its own content entry.",
)
@click.option(
    "--ref-audio",
    multiple=True,
    help='Reference audio as "path_or_url[:role]". Role defaults to "reference_audio". '
    "May be repeated.",
)
@click.option(
    "--ratio",
    default="adaptive",
    help="Aspect ratio. Default: adaptive.",
)
@click.option(
    "--duration", type=int, default=None, help="Video duration in seconds."
)
@click.option(
    "--resolution",
    default="768P",
    help="Output resolution. H3 V2 officially supports 768P (default) and 2K.",
)
@click.option(
    "--model",
    "model",
    type=click.Choice(API_MODELS, case_sensitive=False),
    default="MiniMax-H3",
    help="Generation model. Default: MiniMax-H3.",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Block until the task finishes and download the result.",
)
def create_r2v(
    prompt: str,
    ref_image: tuple[str, ...],
    ref_video: tuple[str, ...],
    ref_video_clips: Optional[str],
    ref_audio: tuple[str, ...],
    ratio: str,
    duration: Optional[int],
    resolution: str,
    model: str,
    wait: bool,
) -> None:
    """Reference video generation: text + multimodal references → video."""
    cfg = load_config()
    require_api_key(cfg)

    ensure_dirs()
    init_db()

    duration_val = duration if duration is not None else cfg.default_duration

    try:
        validate_model_params(model, resolution=resolution, duration=duration_val)
    except ConfigError as e:
        _print(f"[red]{e}[/red]")
        raise SystemExit(1)

    async def _run() -> None:
        from .media import prepare_reference_video

        async with MiniMaxClient(cfg) as client:
            # Build content list
            content: list[dict] = [{"type": "text", "text": {"text": prompt}}]

            # Reference images
            for raw in ref_image:
                path_str, role = _parse_ref(raw, "reference_image")
                url = await resolve_reference(client, path_str)
                content.append({
                    "type": "image_url",
                    "image_url": {"url": url},
                    "role": role,
                })

            # Reference videos — may produce multiple clips per source
            for raw in ref_video:
                path_str, role = _parse_ref(raw, "reference_video")
                source = Path(path_str)
                clips = await prepare_reference_video(source, clips_spec=ref_video_clips)
                for clip in clips:
                    # upload the (possibly extracted) clip
                    clip_url = await resolve_reference(client, clip.path)
                    content.append({
                        "type": "video_url",
                        "video_url": {"url": clip_url},
                        "role": role,
                    })

            # Reference audio
            for raw in ref_audio:
                path_str, role = _parse_ref(raw, "reference_audio")
                url = await resolve_reference(client, path_str)
                content.append({
                    "type": "audio_url",
                    "audio_url": {"url": url},
                    "role": role,
                })

            task_id = await client.create_video_task(
                model=model,
                content=content,
                duration=duration_val,
                resolution=resolution,
                ratio=ratio,
            )
            _print(f"[green]Task submitted: {task_id}[/green]")
            rec = TaskRecord(
                task_id=task_id,
                mode="r2v",
                prompt_excerpt=prompt[:100],
            )
            insert_task(rec)
            update_task(task_id, status="submitted")

            if wait:
                await _wait_and_download(client, task_id, cfg)

    asyncio.run(_run())


# ============================================================ v0.1.3b prepare / commit

# Shared click options reused by the prepare r2v / t2v / i2v subcommands.
_COMMON_PREPARE_OPTS = [
    click.option("--prompt", required=True, help="Natural-language brief (will be sent through H3-Context-IR by default)."),
    click.option("--model", "model", type=click.Choice(API_MODELS, case_sensitive=False), default="MiniMax-H3"),
    click.option("--duration", type=int, default=None),
    click.option("--ratio", default=None),
    click.option("--resolution", default="768P"),
    click.option("--no-context-ir", "use_context_ir", is_flag=True, default=True, help="Disable H3-Context-IR prompt rewrite (use raw prompt)."),
    click.option("--yes", "auto_commit", is_flag=True, default=False, help="Auto-commit after prepare (skip review)."),
]


def _add_common_options(opts: list) -> list:
    """Decorator helper: apply each click option in order (click requires this)."""
    def wrap(fn):
        for opt in reversed(opts):
            fn = opt(fn)
        return fn
    return wrap


def _print_prepare_plan(plan: dict) -> None:
    """Human-friendly rendering of a /api/prepare response."""
    if plan.get("warnings"):
        _print("[yellow]⚠ Warnings:[/yellow]")
        for w in plan["warnings"]:
            _print(f"  • {w}")
    if plan["segments"]:
        _print(f"[cyan]Segments: {plan['estimated_segments']}[/cyan]")
        for seg in plan["segments"]:
            _print(f"  • {seg['index']}: {seg['start_seconds']:.2f}s + {seg['duration_seconds']}s")
    if plan.get("used_context_ir"):
        _print(f"[green]Context-IR: ✓ rewrote prompt ({len(plan['enhanced_prompt'])} chars)[/green]")
    elif plan.get("context_ir_fallback"):
        _print(f"[yellow]Context-IR: ⚠ fallback to original prompt[/yellow]")
        if plan.get("context_ir_error"):
            _print(f"  reason: {plan['context_ir_error']}")
    _print("Content items:")
    for i, item in enumerate(plan["content_preview"]):
        kind = item.get("type", "?")
        if kind == "text":
            preview = (item.get("text") or "")[:80].replace("\n", " ")
            _print(f"  [{i}] text: {preview}{'…' if len(item.get('text') or '') > 80 else ''}")
        elif kind == "video_url":
            _print(f"  [{i}] video_url: {item.get('url', '')[:80]}... (duration={item.get('duration')})")
        elif kind == "image_url":
            _print(f"  [{i}] image_url: {item.get('url', '')[:80]}... (role={item.get('role')})")
        elif kind == "audio_url":
            _print(f"  [{i}] audio_url: {item.get('url', '')[:80]}...")


def _build_body_from_args(
    mode: str, prompt: str, ref_image: tuple, ref_video: tuple, ref_audio: tuple,
    duration, ratio, resolution, model, use_context_ir: bool,
) -> dict:
    """Translate CLI args into the dict shape that CreateBody.model_validate expects."""
    body: dict = {
        "mode": mode, "prompt": prompt, "model": model,
        "duration": duration, "ratio": ratio, "resolution": resolution,
        "ref_images": [], "ref_videos": [], "ref_audios": [],
        "use_context_ir": use_context_ir,
    }
    for raw in ref_image:
        path, role = _parse_ref(raw, "reference_image")
        body["ref_images"].append({"path": path, "role": role})
    for raw in ref_video:
        path, role = _parse_ref(raw, "reference_video")
        body["ref_videos"].append({"path": path, "role": role})
    for raw in ref_audio:
        path, role = _parse_ref(raw, "reference_audio")
        body["ref_audios"].append({"path": path, "role": role})
    return body


def _save_plan(body_dict: dict, plan: dict) -> str:
    """Persist a prepare plan. Returns the prepare_id."""
    prepare_id = "prep-" + uuid.uuid4().hex[:12]
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
    return prepare_id


async def _commit_plan_local(
    prepare_id: str, cfg: Config, wait: bool
) -> str:
    """Commit a prepared plan from the CLI (same path as POST /api/commit)."""
    import uuid as _uuid
    rec = get_prepare(prepare_id)
    if rec is None:
        raise MiniPicError(f"prepare {prepare_id} not found")
    if rec.status == PREPARE_STATUS_COMMITTED:
        raise MiniPicError(f"prepare {prepare_id} already committed")
    if rec.expires_at <= int(time.time() * 1000):
        raise MiniPicError(f"prepare {prepare_id} has expired; please re-prepare")

    body = rec.payload
    content = rec.plan.get("content", [])
    segments_meta = rec.plan.get("segments", [])

    parent_id = "task-" + _uuid.uuid4().hex[:12]
    parent_rec = TaskRecord(
        task_id=parent_id,
        mode="multi",
        prompt_excerpt=(body.get("prompt") or "")[:100],
        status="submitted",
        extra={
            "prepare_id": prepare_id,
            "segments": [],
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

    if wait:
        # Run inline so --wait blocks the CLI until done.
        await _run_multi_segment(parent_id, body, content, segments_meta, cfg)
    else:
        # Fire-and-forget; the task runs in the background of this asyncio loop.
        asyncio.create_task(
            _run_multi_segment(parent_id, body, content, segments_meta, cfg)
        )
    return parent_id


# ---------- prepare group

@click.group(name="prepare")
def prepare_group() -> None:
    """Prepare a generation plan (no H3 call). Use 'minipic commit' to submit."""
    pass


@prepare_group.command(name="t2v")
@click.option("--prompt", required=True)
@click.option("--ratio", required=True)
@click.option("--duration", type=int, default=None)
@click.option("--resolution", default="768P")
@click.option("--model", type=click.Choice(API_MODELS, case_sensitive=False), default="MiniMax-H3")
@click.option("--no-context-ir", "use_context_ir", is_flag=True, default=True)
@click.option("--yes", "auto_commit", is_flag=True, default=False)
def prepare_t2v(
    prompt: str, ratio: str, duration, resolution, model,
    use_context_ir: bool, auto_commit: bool,
) -> None:
    """Prepare a text-to-video plan."""
    cfg = load_config()
    require_api_key(cfg)
    ensure_dirs()
    init_db()

    body_dict = _build_body_from_args(
        "t2v", prompt, (), (), (), duration, ratio, resolution, model, use_context_ir,
    )
    body = CreateBody.model_validate(body_dict)
    _validate_mode(body)

    asyncio.run(_do_prepare(body, body_dict, cfg, auto_commit))


@prepare_group.command(name="i2v")
@click.option("--prompt", required=True)
@click.option("--ref-image", required=True)
@click.option("--ref-image-role", default="first_frame")
@click.option("--duration", type=int, default=None)
@click.option("--resolution", default="768P")
@click.option("--model", type=click.Choice(API_MODELS, case_sensitive=False), default="MiniMax-H3")
@click.option("--no-context-ir", "use_context_ir", is_flag=True, default=True)
@click.option("--yes", "auto_commit", is_flag=True, default=False)
def prepare_i2v(
    prompt: str, ref_image: str, ref_image_role: str, duration, resolution, model,
    use_context_ir: bool, auto_commit: bool,
) -> None:
    """Prepare an image-to-video plan."""
    cfg = load_config()
    require_api_key(cfg)
    ensure_dirs()
    init_db()
    body_dict = _build_body_from_args(
        "i2v", prompt, (f"{ref_image}:{ref_image_role}",), (), (), duration, None, resolution, model, use_context_ir,
    )
    body = CreateBody.model_validate(body_dict)
    _validate_mode(body)
    asyncio.run(_do_prepare(body, body_dict, cfg, auto_commit))


@prepare_group.command(name="r2v")
@click.option("--prompt", required=True)
@click.option("--ref-image", multiple=True)
@click.option("--ref-video", multiple=True)
@click.option("--ref-audio", multiple=True)
@click.option("--duration", type=int, default=None)
@click.option("--ratio", default="adaptive")
@click.option("--resolution", default="768P")
@click.option("--model", type=click.Choice(API_MODELS, case_sensitive=False), default="MiniMax-H3")
@click.option("--no-context-ir", "use_context_ir", is_flag=True, default=True)
@click.option("--yes", "auto_commit", is_flag=True, default=False)
def prepare_r2v(
    prompt: str, ref_image, ref_video, ref_audio,
    duration, ratio, resolution, model,
    use_context_ir: bool, auto_commit: bool,
) -> None:
    """Prepare a reference-to-video plan (multi-segment for >15s reference videos)."""
    cfg = load_config()
    require_api_key(cfg)
    ensure_dirs()
    init_db()
    body_dict = _build_body_from_args(
        "r2v", prompt, ref_image, ref_video, ref_audio,
        duration, ratio, resolution, model, use_context_ir,
    )
    body = CreateBody.model_validate(body_dict)
    _validate_mode(body)
    asyncio.run(_do_prepare(body, body_dict, cfg, auto_commit))


async def _do_prepare(body: CreateBody, body_dict: dict, cfg: Config, auto_commit: bool) -> None:
    """Shared core for `minipic prepare *` subcommands."""
    try:
        plan = await _build_plan(body, cfg)
    except (MiniPicError, OSError) as e:
        _print(f"[red]准备失败: {e}[/red]")
        raise SystemExit(1)

    prepare_id = _save_plan(body_dict, plan)
    response = {
        "prepare_id": prepare_id,
        "estimated_segments": max(len(plan["segments"]), 1),
        "segments": plan["segments"],
        "content_preview": plan["content"],
        "warnings": plan["warnings"],
        "expires_at": int(time.time() * 1000) + PREPARE_TTL_MS,
        **plan,
    }
    _print(f"[green]Prepared: {prepare_id}[/green]")
    _print(f"  Expires in 24h. Run `minipic commit {prepare_id}` to submit.")
    _print_prepare_plan(response)

    if auto_commit:
        parent_id = await _commit_plan_local(prepare_id, cfg, wait=False)
        _print(f"[green]Auto-committed. Task: {parent_id}[/green]")
        _print("  (Running in the background; use `minipic list` to check progress.)")


# ---------- commit

@click.command(name="commit")
@click.argument("prepare_id")
@click.option("--wait/--no-wait", default=False, help="Block until the task finishes.")
def commit_cmd(prepare_id: str, wait: bool) -> None:
    """Commit a prepared plan to H3 (creates the actual task)."""
    cfg = load_config()
    require_api_key(cfg)
    ensure_dirs()
    init_db()
    try:
        parent_id = asyncio.run(_commit_plan_local(prepare_id, cfg, wait))
    except MiniPicError as e:
        _print(f"[red]{e}[/red]")
        raise SystemExit(1)
    _print(f"[green]Task submitted: {parent_id}[/green]")
    if not wait:
        _print("  (Running in the background; use `minipic list` to check progress.)")


# ---------- prepares list

@click.command(name="prepares")
@click.option("--limit", type=int, default=20)
def list_prepares_cmd(limit: int) -> None:
    """List recent (non-expired) prepare records."""
    init_db()
    rows = list_prepares(include_expired=False, limit=limit)
    if not rows:
        _print("No active prepares.")
        return
    for r in rows:
        ts = r.created_at / 1000
        import datetime as _dt
        dt = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        _print(f"[bold]{r.prepare_id}[/bold]  {r.status}  {dt}  task={r.task_id or '—'}")


def _parse_ref(raw: str, default_role: str) -> tuple[str, str]:
    """Parse 'path_or_url[:role]' into (source, role).

    Heuristics to avoid mangling inputs whose first ':' is part of the
    path/URL itself:
      - URLs (``http://``, ``https://``) are passed through untouched unless
        an explicit ``:role`` suffix is given after the URL.
      - Windows drive paths (``C:\\...``) are passed through untouched unless
        an explicit ``:role`` suffix is given after the path.
      - Otherwise, the first ':' separates the path from the role.
    """
    s = raw.strip()
    lower = s.lower()
    is_url = lower.startswith("http://") or lower.startswith("https://")
    is_windows_path = (
        len(s) >= 2 and s[0].isalpha() and s[1] == ":" and s[2] in ("\\", "/")
    )
    if is_url or is_windows_path:
        # If the user appended an explicit role (rare for URLs / Windows
        # paths), honor it. We require the role token to NOT contain a
        # path separator, which avoids accidentally splitting a URL/path
        # at a colon inside it.
        # Look for a trailing ':<token>' where <token> has no '/' or '\\'.
        import re
        m = re.search(r":([^/\\]+)$", s)
        if m:
            role = m.group(1).strip()
            source = s[: m.start()].strip()
            return source, role
        return s, default_role
    if ":" in s:
        source, role = s.split(":", 1)
        return source.strip(), role.strip()
    return s, default_role


async def _wait_and_download(client: MiniMaxClient, task_id: str, cfg: Config) -> None:
    """Poll task, download result, and update the DB record."""
    status_display = {"last": ""}

    def on_progress(status: str) -> None:
        if status != status_display["last"]:
            status_display["last"] = status
            _print(f"  → {_status_label(status)}")

    task = await poll_until_done(
        client,
        task_id,
        interval_seconds=cfg.poll_interval_seconds,
        on_progress=on_progress,
    )

    status = task.get("status", "")
    update_task(task_id, status=status)

    if status == STATUS_SUCCEEDED:
        # Extract output URL
        content_list = task.get("content") or []
        video_url: Optional[str] = None
        for entry in content_list:
            if isinstance(entry, dict) and entry.get("type") == "video":
                video_url = (
                    entry.get("video", {}).get("url")
                    or entry.get("url")
                )
                break
        if not video_url:
            # Fallback: look for any URL field in content
            for entry in content_list:
                if isinstance(entry, dict):
                    video_url = entry.get("video_url", {}).get("url") or entry.get("url")
                    if video_url:
                        break

        if not video_url:
            raise MiniPicError(
                f"task {task_id} succeeded but no video URL found in response: {task}"
            )

        videos_dir = Path(cfg.videos_dir).resolve()
        videos_dir.mkdir(parents=True, exist_ok=True)
        dest = videos_dir / f"{task_id}.mp4"

        _print(f"  Downloading → {dest}")
        _atomic_download(client, video_url, dest)
        update_task(task_id, status=STATUS_SUCCEEDED, output_path=str(dest))
        _print(f"[green]✓ Saved: {dest}[/green]")
    else:
        err = task.get("error") or task.get("base_resp") or {}
        err_msg = err.get("message") or err.get("msg") or str(err) or status
        update_task(task_id, error=err_msg, status=status)
        _print(f"[red]Task ended: {err_msg}[/red]")


# ---------------------------------------------------------------- status / wait / list


@click.command()
@click.argument("task_id")
def status(task_id: str) -> None:
    """Poll a task once and print its current status."""
    cfg = load_config()
    require_api_key(cfg)

    async def _run() -> None:
        async with MiniMaxClient(cfg) as client:
            task = await client.query_video_task(task_id)
            status_val = task.get("status", "?")
            _print(f"{_status_label(status_val)}  {task_id}")
            # Show video URL if succeeded
            if status_val == STATUS_SUCCEEDED:
                content_list = task.get("content") or []
                for entry in content_list:
                    if isinstance(entry, dict):
                        url = entry.get("video_url", {}).get("url") or entry.get("url")
                        if url:
                            _print(f"  Video: {url}")
                            break

    asyncio.run(_run())


@click.command()
@click.argument("task_id")
def wait_cmd(task_id: str) -> None:
    """Block until the task reaches a terminal state."""
    cfg = load_config()
    require_api_key(cfg)

    async def _run() -> None:
        async with MiniMaxClient(cfg) as client:
            await _wait_and_download(client, task_id, cfg)

    asyncio.run(_run())


@click.command()
@click.option(
    "--limit", type=int, default=20, help="Maximum number of tasks to show."
)
def list_cmd(limit: int) -> None:
    """List recent tasks from the local SQLite ledger."""
    init_db()
    tasks = list_tasks(limit=limit)
    if not tasks:
        _print("No tasks found.")
        return
    for rec in tasks:
        _print_task_record(rec)


@click.command()
@click.argument("task_id")
def cancel(task_id: str) -> None:
    """Cancel a queued/processing task (no-op)."""
    _print(
        "minipic does not support cancellation via API.\n"
        "Please use the MiniMax console to manage your task:\n"
        "  https://platform.minimaxi.com"
    )
    raise SystemExit(0)


@click.command()
def videos() -> None:
    """List downloaded video files."""
    cfg = load_config()
    videos_dir = Path(cfg.videos_dir).resolve()
    if not videos_dir.is_dir():
        _print(f"Videos directory does not exist: {videos_dir}")
        return
    files = sorted(videos_dir.glob("*.mp4"))
    if not files:
        _print("No videos downloaded yet.")
        return
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        _print(f"  {f.name}  ({size_mb:.1f} MB)")


# ---------------------------------------------------------------- web


@click.command()
@click.option("--port", type=int, default=7860, help="Port to listen on.")
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host to bind to. Use 0.0.0.0 to expose externally.",
)
def web(port: int, host: str) -> None:
    """Start the local web UI (FastAPI + uvicorn).

    No API key is required at startup — the user can configure it through
    the UI's "API Key" button (POST /api/config) before submitting a task.
    """
    import uvicorn

    ensure_dirs()
    init_db()

    _print(f"Starting web UI at http://{host}:{port}")
    _print(
        "Tip: configure your API key in the UI (top-right 'API Key' button) "
        "if you haven't already."
    )
    uvicorn.run(
        "minipic.web:app",
        host=host,
        port=port,
        reload=False,
    )


# ---------------------------------------------------------------- main entry point


@click.group()
def main() -> None:
    """minipic — local CLI for MiniMax H3 video generation (T2V / I2V / R2V)."""
    pass


# Register all subcommands / groups
main.add_command(config_group)
main.add_command(create_group)
main.add_command(prepare_group)
main.add_command(commit_cmd, name="commit")
main.add_command(list_prepares_cmd, name="prepares")
main.add_command(status)
main.add_command(wait_cmd, name="wait")
main.add_command(list_cmd, name="list")
main.add_command(cancel)
main.add_command(videos)
main.add_command(web)


# ---------------------------------------------------------------- top-level error handling


def _cli_wrapper() -> None:
    """Wrapper that catches exceptions and exits with appropriate codes."""
    try:
        main()
    except MiniPicError as exc:
        _print(f"[red]Error: {exc}[/red]")
        raise SystemExit(2)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    _cli_wrapper()
