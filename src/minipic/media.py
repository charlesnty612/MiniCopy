"""Local media helpers: upload, ffmpeg discovery, shared content[] builder.

Cross-platform ffmpeg resolution:
  1. shutil.which("ffmpeg")        — system PATH
  2. imageio_ffmpeg.get_ffmpeg_exe()  — bundled static binary shipped via pip

The shared ``build_content`` / ``download_task_result`` helpers live here so
both the CLI and the FastAPI web layer use one canonical implementation of
the official MiniMax V2 content[] shape and the task-result download flow
(CLI and web used to duplicate both, with the CLI accidentally nesting text
which the official API rejects).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .client import MiniMaxClient
from .config import user_data_path
from .errors import MediaError

log = logging.getLogger(__name__)

# H3 multimodal-references limits (per official docs)
REF_VIDEO_MAX_DURATION = 15.0       # seconds
REF_VIDEO_MIN_DURATION = 2.0        # seconds
REF_AUDIO_MAX_DURATION = 15.0       # seconds (audio ≤15s)
REF_AUDIO_MIN_DURATION = 2.0        # seconds


@dataclass
class VideoClip:
    """A reference video clip. v0.1.5+ is single-clip only (no multi-segment)."""

    path: Path
    start_seconds: float
    duration_seconds: float
    is_split: bool  # always False now — kept for backward-compatible shape

    def label(self) -> str:
        return f"{self.start_seconds:.2f}s+{self.duration_seconds:.2f}s"


# ----------------------------------------------------------- ffmpeg discovery

def find_ffmpeg() -> str:
    """Return the path to a working ffmpeg binary, or raise MediaError."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # noqa: PLC0415
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled:
            return bundled
    except Exception:  # noqa: BLE001
        pass
    raise MediaError(
        "ffmpeg not found.\n"
        "Install one of:\n"
        "  - System ffmpeg: https://ffmpeg.org/download.html\n"
        "  - The bundled static binary: pip install imageio-ffmpeg"
    )


def ffprobe_duration(path: Path) -> float:
    """Return the duration of a media file in seconds (float).

    Uses ffmpeg's own ``-i`` probe (stderr parses ``Duration: HH:MM:SS.xx``),
    no separate ffprobe binary required. The imageio-ffmpeg bundled binary
    only ships ffmpeg, so we avoid depending on a system ffprobe.
    """
    ffmpeg = find_ffmpeg()
    # ffmpeg always exits non-zero when only -i (no output spec) is given;
    # ignore returncode and parse stderr instead.
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"media probe failed for {path}: {e}") from e
    except OSError as e:
        raise MediaError(f"media probe failed for {path}: {e}") from e
    blob = (result.stderr or "") + "\n" + (result.stdout or "")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", blob)
    if not m:
        raise MediaError(
            f"无法读取媒体时长（文件可能损坏或格式不支持）: {path}"
        )
    h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600.0 + mn * 60.0 + s


# ----------------------------------------------------------------- upload

async def resolve_reference(client: MiniMaxClient, source: str | Path) -> str:
    """Resolve a local file path or public URL to a public HTTPS URL.

    - If `source` is a local file, upload via V1 file API and return the CDN URL.
    - If `source` is a URL (starts with http:// or https://), return it as-is.
    - Caches the result for local files (by path + mtime).
    """
    if isinstance(source, str):
        s = source.strip()
        if s.startswith("http://") or s.startswith("https://"):
            return s
        # treat as path
        source = Path(s)

    if not source.is_file():
        raise MediaError(f"file not found: {source}")

    cache_path = _upload_cache_path(source)
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("mtime") == source.stat().st_mtime and cached.get("url"):
                return cached["url"]
        except (json.JSONDecodeError, KeyError):
            cache_path.unlink(missing_ok=True)

    url = await client.upload_and_resolve(source)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"url": url, "file_id": None, "path": str(source),
                    "mtime": source.stat().st_mtime}, ensure_ascii=False),
        encoding="utf-8",
    )
    return url


def _upload_cache_path(local_path: Path) -> Path:
    # one cache file per (absolute) source path
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(local_path.resolve()))
    return user_data_path("uploads") / f"{safe}.json"


# ------------------------------------------------- reference video validation

async def prepare_reference_video(source: Path) -> list[VideoClip]:
    """Inspect a reference video. Returns a single-element list of VideoClip.

    v0.1.5+: multi-segment generation has been removed. Reference videos longer
    than H3's per-call cap (15s) are rejected outright — the user is expected
    to pre-trim their footage with ffmpeg before submitting. The return type
    stays a list for backward-compatible call sites; the list always has
    exactly one element (the source itself).
    """
    if not source.is_file():
        raise MediaError(f"video not found: {source}")
    duration = ffprobe_duration(source)

    if duration > REF_VIDEO_MAX_DURATION + 0.05:
        raise MediaError(
            f"参考视频 {duration:.1f}s 超过 H3 上限 15s，多段生成已移除，"
            f"请先用 ffmpeg 截取到 ≤15s 再重试"
        )

    return [VideoClip(
        path=source,
        start_seconds=0.0,
        duration_seconds=duration,
        is_split=False,
    )]


# ------------------------------------------------- reference audio validation

@dataclass
class AudioClip:
    """A reference audio clip. v0.1.6+: single clip only (no multi-segment)."""

    path: Path
    start_seconds: float
    duration_seconds: float
    is_split: bool  # always False now — kept for backward-compatible shape

    def label(self) -> str:
        return f"{self.start_seconds:.2f}s+{self.duration_seconds:.2f}s"


async def prepare_reference_audio(source: Path) -> list[AudioClip]:
    """Inspect a reference audio. Returns a single-element list of AudioClip.

    v0.1.6+: aligns with the MiniMax V2 audio-reference cap (≤15s per clip,
    no multi-segment generation). Longer sources are rejected outright — the
    user is expected to pre-trim with ffmpeg before submitting. The return
    type is a list for symmetry with ``prepare_reference_video``; the list
    always has exactly one element (the source itself).

    Remote URLs cannot be probed cheaply here, so callers must pre-resolve
    remote audio to a local file before invoking this helper.
    """
    if not source.is_file():
        raise MediaError(f"audio not found: {source}")
    duration = ffprobe_duration(source)

    if duration > REF_AUDIO_MAX_DURATION + 0.05:
        raise MediaError(
            f"音频参考 {duration:.1f}s 超过 H3 上限 15s，请先用 ffmpeg "
            f"截取到 ≤15s 再重试"
        )
    if duration < REF_AUDIO_MIN_DURATION - 0.05:
        raise MediaError(
            f"音频参考 {duration:.1f}s 短于 H3 下限 2s，请提供 ≥2s 的音频"
        )

    return [AudioClip(
        path=source,
        start_seconds=0.0,
        duration_seconds=duration,
        is_split=False,
    )]


# ----------------------------------------------------------- shared content[] builder


async def build_content(
    *,
    prompt: str,
    client: MiniMaxClient,
    ref_images: Optional[list[dict[str, Any]]] = None,
    ref_videos: Optional[list[dict[str, Any]]] = None,
    ref_audios: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Build the official MiniMax V2 content[] list from a prompt + references.

    Single source of truth for the ``content`` array shape used by both the
    CLI and the web layer (see ``cli.create_*`` and ``web._submit_task``).

    Shape (per official V2 docs):
      - ``{"type":"text","text":"..."}``  ← **flat string**, never nested.
      - ``{"type":"image_url","image_url":{"url":"..."},"role":"..."}``
      - ``{"type":"video_url","video_url":{"url":"..."},"duration":N,"role":"..."}``
      - ``{"type":"audio_url","audio_url":{"url":"..."},"duration":N,"role":"..."}``

    For each reference, a remote ``http(s)://...`` path is passed through
    unchanged; a local path is resolved via :func:`resolve_reference` (which
    uploads the file via the V1 file API). Video and audio clips additionally
    carry ``duration`` (seconds) so the server can validate the per-call cap.

    Role defaults:
      - images → ``reference_image``  (i2v first/last_frame call sites pass an
        explicit role)
      - videos → ``reference_video``
      - audios → ``reference_audio``

    This function performs **no** mode-level validation — that's the
    caller's responsibility (see ``web._validate_mode`` and
    ``cli.validate_model_modes``).
    """
    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
    ]

    # ---- Reference images
    for ref in ref_images or []:
        path = ref.get("path") or ""
        role = ref.get("role") or "reference_image"
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = await resolve_reference(client, path)
        content.append(
            {"type": "image_url", "image_url": {"url": url}, "role": role}
        )

    # ---- Reference videos (single-clip only; multi-segment removed)
    for ref in ref_videos or []:
        path = ref.get("path") or ""
        role = ref.get("role") or "reference_video"
        if path.startswith("http://") or path.startswith("https://"):
            # Remote URL — pass through; if it's >15s the server will reject.
            content.append(
                {"type": "video_url", "video_url": {"url": path}, "role": role}
            )
            continue
        clips = await prepare_reference_video(Path(path))
        for clip in clips:
            clip_url = await resolve_reference(client, clip.path)
            content.append({
                "type": "video_url",
                "video_url": {"url": clip_url},
                "duration": clip.duration_seconds,
                "role": role,
            })

    # ---- Reference audio (single-clip only; v0.1.6+ aligns with H3 ≤15s cap)
    for ref in ref_audios or []:
        path = ref.get("path") or ""
        role = ref.get("role") or "reference_audio"
        if path.startswith("http://") or path.startswith("https://"):
            # Remote URL — pass through; if it's >15s the server will reject.
            content.append(
                {"type": "audio_url", "audio_url": {"url": path}, "role": role}
            )
            continue
        clips = await prepare_reference_audio(Path(path))
        for clip in clips:
            clip_url = await resolve_reference(client, clip.path)
            content.append({
                "type": "audio_url",
                "audio_url": {"url": clip_url},
                "duration": clip.duration_seconds,
                "role": role,
            })

    return content


# ----------------------------------------------------------- shared task-result download


def _extract_video_url(task: dict[str, Any]) -> Optional[str]:
    """Pull the output video URL from a succeeded MiniMax task response.

    Tolerant of the shapes the API has been observed to emit:
      - ``content["url"]`` (current shape — content is a dict, e.g.
        {"url": "https://..."} for video tasks, {"prompt": ...} for Context-IR)
      - ``content[0]["url"]`` (flat url on the first list entry)
      - ``content[i]["type"] == "video"`` with either ``video.url`` or
        ``video_url.url`` (older / alternate shape)
    """
    content = task.get("content") or {}
    if isinstance(content, dict):
        url = content.get("url")
        if url:
            return url
        return None
    if isinstance(content, list) and content:
        # Prefer the first entry's ``url`` field (current API shape)
        first = content[0]
        if isinstance(first, dict):
            url = first.get("url")
            if url:
                return url
        # Fallback: scan for a type=video entry (compatibility)
        for entry in content:
            if isinstance(entry, dict) and entry.get("type") == "video":
                url = (
                    entry.get("video", {}).get("url")
                    or entry.get("url")
                    or entry.get("video_url", {}).get("url")
                )
                if url:
                    return url
        # Last resort: any entry with a url
        for entry in content:
            if isinstance(entry, dict):
                url = entry.get("video_url", {}).get("url") or entry.get("url")
                if url:
                    return url
    return None


async def download_task_result(
    client: MiniMaxClient, task: dict[str, Any], dest: Path,
) -> bool:
    """Download the result MP4 from a succeeded task response to ``dest``.

    Returns ``True`` if a URL was found and the file was downloaded,
    ``False`` otherwise (silent no-URL — callers decide whether to raise).

    The download is atomic via ``dest.with_suffix(".tmp")`` + ``replace`` so
    a partial download never overwrites a previous good file.
    """
    url = _extract_video_url(task)
    if not url:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    await client.download_video(url, tmp)
    tmp.replace(dest)
    return True