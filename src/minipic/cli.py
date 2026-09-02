"""Click-based CLI for MiniCopy."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from pathlib import Path
from typing import Any, Optional

import click

# Fixed web UI port — all launch paths (CLI `web` command and
# `python -m minipic.web`) bind to this same port so we never
# accidentally run on a different one (e.g. 8765).
WEB_PORT = 7860

from .config import (
    API_MODELS,
    Config,
    ensure_dirs,
    load_config,
    require_api_key,
    save_config,
    validate_model_modes,
    validate_model_params,
)
from .client import MiniMaxClient
from .errors import ConfigError, MiniPicError
from .media import build_content, download_task_result
from .poller import (
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    poll_until_done,
)
from .storage import (
    TaskRecord,
    get_task,
    init_db,
    insert_task,
    list_tasks,
    update_task,
)

# ---------------------------------------------------------------- try rich

log = logging.getLogger(__name__)

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


async def _maybe_apply_context_ir(
    client: MiniMaxClient,
    *,
    model: str,
    content: list[dict[str, Any]],
    duration: int,
    ratio: str,
) -> bool:
    """Optionally rewrite ``content[0].text`` via H3-Context-IR.

    Returns True if the prompt was actually rewritten, False otherwise
    (skipped, H3-Max, empty response, or any error). All failures are
    swallowed silently with a warning log — Context-IR is a nice-to-have,
    never a gate for the real submission.
    """
    if model != "MiniMax-H3":
        return False
    if not content or content[0].get("type") != "text":
        return False
    try:
        ir_task_id = await client.create_context_ir_task(
            model=model,
            content=content,
            duration=duration,
            ratio=ratio,
        )
        enhanced = await client.fetch_context_ir_prompt(ir_task_id)
    except Exception as e:  # noqa: BLE001 — graceful fallback
        log.warning(
            "context-ir failed (%s), falling back to original prompt",
            e.__class__.__name__,
        )
        return False
    if not enhanced:
        return False
    content[0]["text"] = enhanced
    return True


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
@click.option(
    "--no-context-ir",
    is_flag=True,
    default=False,
    help="Disable H3-Context-IR prompt rewrite (H3 only; H3-Max auto-skips).",
)
def create_t2v(
    prompt: str,
    duration: Optional[int],
    ratio: str,
    resolution: str,
    model: str,
    wait: bool,
    no_context_ir: bool,
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
        validate_model_modes(model, mode="t2v")
    except ConfigError as e:
        _print(f"[red]{e}[/red]")
        raise SystemExit(1)

    async def _run() -> None:
        async with MiniMaxClient(cfg) as client:
            content = await build_content(prompt=prompt, client=client)
            if not no_context_ir and await _maybe_apply_context_ir(
                client, model=model, content=content,
                duration=duration_val, ratio=ratio,
            ):
                _print("[green]Context-IR: \u2713 \u63d0\u793a\u8bcd\u5df2\u4f18\u5316[/green]")
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
@click.option(
    "--no-context-ir",
    is_flag=True,
    default=False,
    help="Disable H3-Context-IR prompt rewrite (H3 only; H3-Max auto-skips).",
)
def create_i2v(
    prompt: str,
    ref_image: str,
    ref_image_role: str,
    duration: Optional[int],
    resolution: str,
    model: str,
    wait: bool,
    no_context_ir: bool,
) -> None:
    """Image-to-video: first-frame image + text prompt → video."""
    cfg = load_config()
    require_api_key(cfg)

    ensure_dirs()
    init_db()

    duration_val = duration if duration is not None else cfg.default_duration

    try:
        validate_model_params(model, resolution=resolution, duration=duration_val)
        validate_model_modes(model, mode="i2v", i2v_role=ref_image_role)
    except ConfigError as e:
        _print(f"[red]{e}[/red]")
        raise SystemExit(1)

    async def _run() -> None:
        async with MiniMaxClient(cfg) as client:
            content = await build_content(
                prompt=prompt,
                client=client,
                ref_images=[{"path": ref_image, "role": ref_image_role}],
            )
            # i2v forces ratio=adaptive for both Context-IR and the real task.
            if not no_context_ir and await _maybe_apply_context_ir(
                client, model=model, content=content,
                duration=duration_val, ratio="adaptive",
            ):
                _print("[green]Context-IR: \u2713 \u63d0\u793a\u8bcd\u5df2\u4f18\u5316[/green]")
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
    "May be repeated. Reference videos must be ≤15s (H3 per-call cap).",
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
@click.option(
    "--no-context-ir",
    is_flag=True,
    default=False,
    help="Disable H3-Context-IR prompt rewrite (H3 only; H3-Max auto-skips).",
)
def create_r2v(
    prompt: str,
    ref_image: tuple[str, ...],
    ref_video: tuple[str, ...],
    ref_audio: tuple[str, ...],
    ratio: str,
    duration: Optional[int],
    resolution: str,
    model: str,
    wait: bool,
    no_context_ir: bool,
) -> None:
    """Reference video generation: text + multimodal references → video."""
    cfg = load_config()
    require_api_key(cfg)

    ensure_dirs()
    init_db()

    duration_val = duration if duration is not None else cfg.default_duration

    try:
        validate_model_params(model, resolution=resolution, duration=duration_val)
        validate_model_modes(model, mode="r2v")
    except ConfigError as e:
        _print(f"[red]{e}[/red]")
        raise SystemExit(1)

    async def _run() -> None:
        async with MiniMaxClient(cfg) as client:
            # Parse all ref specs up-front (cheap, no I/O)
            ref_image_dicts = [
                {"path": path_str, "role": role}
                for raw in ref_image
                for path_str, role in (_parse_ref(raw, "reference_image"),)
            ]
            ref_video_dicts = [
                {"path": path_str, "role": role}
                for raw in ref_video
                for path_str, role in (_parse_ref(raw, "reference_video"),)
            ]
            ref_audio_dicts = [
                {"path": path_str, "role": role}
                for raw in ref_audio
                for path_str, role in (_parse_ref(raw, "reference_audio"),)
            ]
            content = await build_content(
                prompt=prompt,
                client=client,
                ref_images=ref_image_dicts,
                ref_videos=ref_video_dicts,
                ref_audios=ref_audio_dicts,
            )

            ir_ratio = ratio or "adaptive"
            if not no_context_ir and await _maybe_apply_context_ir(
                client, model=model, content=content,
                duration=duration_val, ratio=ir_ratio,
            ):
                _print("[green]Context-IR: \u2713 \u63d0\u793a\u8bcd\u5df2\u4f18\u5316[/green]")

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
        videos_dir = Path(cfg.videos_dir).resolve()
        videos_dir.mkdir(parents=True, exist_ok=True)
        dest = videos_dir / f"{task_id}.mp4"

        _print(f"  Downloading → {dest}")
        # Shared URL-extract + atomic download (with web layer). Returns False
        # when the response carries no URL — surface as a user-facing error.
        ok = await download_task_result(client, task, dest)
        if not ok:
            raise MiniPicError(
                f"task {task_id} succeeded but no video URL found in response: {task}"
            )
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
        "MiniCopy does not support cancellation via API.\n"
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
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host to bind to. Use 0.0.0.0 to expose externally.",
)
def web(host: str) -> None:
    """Start the local web UI (FastAPI + uvicorn) on port 7860.

    No API key is required at startup — the user can configure it through
    the UI's "API Key" button (POST /api/config) before submitting a task.

    The port is fixed at 7860 (WEB_PORT) to keep all launch paths consistent;
    it cannot be overridden via CLI.
    """
    import uvicorn

    ensure_dirs()
    init_db()

    _print(f"Starting web UI at http://{host}:{WEB_PORT}")
    _print(
        "Tip: configure your API key in the UI (top-right 'API Key' button) "
        "if you haven't already."
    )
    uvicorn.run(
        "minipic.web:app",
        host=host,
        port=WEB_PORT,
        reload=False,
    )


# ---------------------------------------------------------------- main entry point


@click.group()
def main() -> None:
    """MiniCopy — local CLI for MiniMax H3 video generation (T2V / I2V / R2V)."""
    pass


# Register all subcommands / groups
main.add_command(config_group)
main.add_command(create_group)
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
