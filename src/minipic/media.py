"""Local media helpers: upload, video splitting, ffmpeg discovery.

Cross-platform ffmpeg resolution:
  1. shutil.which("ffmpeg")        — system PATH
  2. imageio_ffmpeg.get_ffmpeg_exe()  — bundled static binary shipped via pip
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .client import MiniMaxClient
from .config import user_data_path
from .errors import MediaError

log = logging.getLogger(__name__)

# H3 multimodal-references limits (per official docs)
REF_VIDEO_MAX_DURATION = 15.0       # seconds
REF_VIDEO_MIN_DURATION = 2.0        # seconds
REF_VIDEO_MAX_CLIPS = 3
REF_VIDEO_TOTAL_BUDGET = 15.0       # sum of durations in seconds
REF_VIDEO_CLIP_LEN_AUTO = 5.0       # default sample length when auto-sampling

# v0.1.3b multi-segment planning limits
SEGMENT_MIN_SECONDS = 4             # H3 min (per single generation call)
SEGMENT_MAX_SECONDS = 15            # H3 max (per single generation call)
SEGMENT_MAX_COUNT = 4               # cap the number of segments per multi-segment task
SEGMENT_BOUNDARY_WINDOW = 3.0       # ±3s window when snapping to a shot boundary
SEGMENT_SCENE_THRESHOLDS = (0.4, 0.3, 0.5)  # adaptive ffmpeg scene detection thresholds


@dataclass
class VideoClip:
    path: Path
    start_seconds: float
    duration_seconds: float
    is_split: bool  # True if extracted from a longer source

    def label(self) -> str:
        return f"{self.start_seconds:.2f}s+{self.duration_seconds:.2f}s"


@dataclass
class SegmentSpec:
    """A planned cut for multi-segment generation (v0.1.3b).

    Distinct from VideoClip: SegmentSpec is the *plan* (start + integer duration
    that the segment should produce). VideoClip is the *executed* split file
    on disk. The two are produced by different code paths in v0.1.3b
    (`plan_segments` vs `prepare_reference_video`).
    """

    start_seconds: float
    duration_seconds: int

    def end_seconds(self) -> float:
        return self.start_seconds + float(self.duration_seconds)

    def label(self) -> str:
        return f"{self.start_seconds:.2f}s–{self.end_seconds():.2f}s ({self.duration_seconds}s)"


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
    """Return the duration of a media file in seconds (float)."""
    ffmpeg = find_ffmpeg()  # ffprobe ships with the same package
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe") if "ffmpeg" in Path(ffmpeg).name else "ffprobe"
    ffprobe = shutil.which("ffprobe") or ffprobe
    try:
        out = subprocess.check_output(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise MediaError(f"ffprobe failed for {path}: {e}") from e
    data = json.loads(out)
    return float(data["format"]["duration"])


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


# ------------------------------------------------- reference video splitting

def _parse_clips_spec(spec: str, source_duration: float) -> list[VideoClip]:
    """Parse a `--ref-video-clips` argument.

    Format: "start:duration,start:duration,..."  (seconds, may be float)
    Validates count <= 3 and total <= 15s.
    """
    if not spec:
        return []
    clips: list[VideoClip] = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        if ":" not in raw:
            raise MediaError(f"clip spec must be 'start:duration', got {raw!r}")
        s, d = raw.split(":", 1)
        try:
            start = float(s)
            dur = float(d)
        except ValueError as e:
            raise MediaError(f"clip spec must be numeric, got {raw!r}") from e
        if dur < REF_VIDEO_MIN_DURATION or dur > REF_VIDEO_MAX_DURATION:
            raise MediaError(
                f"clip duration {dur}s out of range "
                f"[{REF_VIDEO_MIN_DURATION}, {REF_VIDEO_MAX_DURATION}]"
            )
        if start < 0 or start + dur > source_duration + 0.05:
            raise MediaError(
                f"clip {start}:{dur} extends past source duration {source_duration:.2f}s"
            )
        clips.append(VideoClip(path=Path(), start_seconds=start, duration_seconds=dur, is_split=False))
    if len(clips) > REF_VIDEO_MAX_CLIPS:
        raise MediaError(f"too many clips: {len(clips)} (max {REF_VIDEO_MAX_CLIPS})")
    total = sum(c.duration_seconds for c in clips)
    if total > REF_VIDEO_TOTAL_BUDGET + 0.05:
        raise MediaError(
            f"total clip duration {total:.2f}s exceeds budget {REF_VIDEO_TOTAL_BUDGET}s"
        )
    return clips


def _auto_sample_clips(source_duration: float) -> list[VideoClip]:
    """Default sampling: up to 3 evenly-spaced clips, fitting the 15s budget.

    - If source fits in one ≤15s clip, return a single clip of the full duration.
    - Otherwise, return up to 3 evenly-spaced clips, each capped at 5s.
    """
    if source_duration <= REF_VIDEO_MAX_DURATION + 0.05:
        # Short video — single clip of the full thing
        return [VideoClip(
            path=Path(), start_seconds=0.0,
            duration_seconds=source_duration, is_split=True,
        )]
    n = min(REF_VIDEO_MAX_CLIPS, max(1, int(source_duration / REF_VIDEO_CLIP_LEN_AUTO)))
    if n == 1:
        starts = [0.0]
    else:
        last_start = max(0.0, source_duration - REF_VIDEO_CLIP_LEN_AUTO)
        starts = [last_start * i / (n - 1) for i in range(n)]
    return [
        VideoClip(
            path=Path(),
            start_seconds=round(s, 2),
            duration_seconds=min(REF_VIDEO_CLIP_LEN_AUTO, source_duration - s),
            is_split=True,
        )
        for s in starts
    ]


async def prepare_reference_video(
    source: Path,
    *,
    clips_spec: Optional[str] = None,
) -> list[VideoClip]:
    """Inspect source video. Return a list of VideoClip (paths may be temp files).

    If the source is already within H3 limits, returns a single-clip list pointing
    at the original file with is_split=False. Otherwise splits into a temp directory.
    """
    if not source.is_file():
        raise MediaError(f"video not found: {source}")
    duration = ffprobe_duration(source)

    if duration <= REF_VIDEO_MAX_DURATION + 0.05:
        if clips_spec:
            # user wants to clip something already short — use as-is but warn
            log.warning("video is already short (%.2fs); ignoring --ref-video-clips", duration)
        return [VideoClip(path=source, start_seconds=0.0, duration_seconds=duration, is_split=False)]

    if clips_spec:
        clips = _parse_clips_spec(clips_spec, duration)
    else:
        clips = _auto_sample_clips(duration)
        log.info(
            "auto-sampling %d x %.1fs clips from %.1fs source (fits %ds budget)",
            len(clips), REF_VIDEO_CLIP_LEN_AUTO, duration, int(REF_VIDEO_TOTAL_BUDGET),
        )

    # Extract each clip into the cache dir
    cache_dir = user_data_path("clips") / source.stem
    cache_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    extracted: list[VideoClip] = []
    for i, clip in enumerate(clips):
        out = cache_dir / f"clip_{i:02d}_t{int(clip.start_seconds):04d}_d{int(clip.duration_seconds):04d}.mp4"
        if not out.is_file():
            cmd = [
                ffmpeg, "-y", "-loglevel", "error",
                "-ss", f"{clip.start_seconds:.3f}",
                "-i", str(source),
                "-t", f"{clip.duration_seconds:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(out),
            ]
            log.debug("ffmpeg %s", " ".join(cmd))
            try:
                await asyncio.to_thread(subprocess.run, cmd, check=True, timeout=120)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                raise MediaError(f"ffmpeg extract failed: {e}") from e
        extracted.append(VideoClip(
            path=out, start_seconds=clip.start_seconds,
            duration_seconds=clip.duration_seconds, is_split=True,
        ))
    return extracted


# =========================================================== v0.1.3b multi-segment

def find_shot_boundaries(
    video_path: Path,
    *,
    thresholds: tuple[float, ...] = SEGMENT_SCENE_THRESHOLDS,
    min_count: int = 3,
    max_count: int = 20,
) -> list[float]:
    """Detect shot boundaries in a video using ffmpeg's scene filter.

    Tries each ``threshold`` in order; returns the first list whose length falls
    inside ``[min_count, max_count]``. Falls back to an empty list when no
    threshold produces a usable result (caller is expected to evenly split).

    Implementation notes
    --------------------
    * Uses ``select='gt(scene,THR)',showinfo`` so we can read the pts_time of
      each detected frame transition from ffmpeg's log output.
    * This is intentionally hand-rolled (~30 lines) so we do not pull in
      PySceneDetect as a dependency.
    """
    if not video_path.is_file():
        raise MediaError(f"video not found: {video_path}")
    ffmpeg = find_ffmpeg()
    for threshold in thresholds:
        try:
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-i", str(video_path),
                    "-filter:v", f"select='gt(scene,{threshold})',showinfo",
                    "-f", "null", "-",
                ],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8",
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("ffmpeg scene detect failed at threshold=%s: %s", threshold, e)
            continue
        # ffmpeg prints "pts_time:NNN" on the showinfo line for every selected frame.
        boundaries: list[float] = []
        for line in proc.stderr.splitlines():
            idx = line.find("pts_time:")
            if idx < 0:
                continue
            tail = line[idx + len("pts_time:"):].strip()
            head = tail.split()[0] if tail else ""
            try:
                boundaries.append(float(head))
            except ValueError:
                continue
        if min_count <= len(boundaries) <= max_count:
            log.info(
                "scene detect: threshold=%s found %d boundaries in %s",
                threshold, len(boundaries), video_path.name,
            )
            return sorted(boundaries)
        log.debug(
            "scene detect: threshold=%s produced %d boundaries (target %d..%d), retrying",
            threshold, len(boundaries), min_count, max_count,
        )
    log.warning("scene detect: all thresholds failed; will fall back to even split")
    return []


def plan_segments(
    ref_duration: float,
    boundaries: list[float],
    *,
    max_segment_duration: int = SEGMENT_MAX_SECONDS,
    min_segment_duration: int = SEGMENT_MIN_SECONDS,
    max_segments: int = SEGMENT_MAX_COUNT,
    boundary_window: float = SEGMENT_BOUNDARY_WINDOW,
) -> list[SegmentSpec]:
    """Plan how to split a reference video into multiple H3 generations.

    Each generated segment must be an *integer* number of seconds (H3 only
    accepts integer durations). The total of all segments may exceed the
    reference duration slightly (H3 clips always end at the next second).

    Raises
    ------
    MediaError
        When the reference would require more than ``max_segments`` segments.
    """
    if ref_duration <= 0:
        raise MediaError("ref_duration must be > 0")
    n_segments = max(1, math.ceil(ref_duration / max_segment_duration))
    if n_segments > max_segments:
        raise MediaError(
            f"reference video is {ref_duration:.1f}s, would need {n_segments} segments "
            f"(max {max_segments}). Trim the video to \u2264{max_segments * max_segment_duration}s, "
            f"or split into multiple minipic calls."
        )

    # Short video that fits in one segment: return a single segment, no snapping.
    if ref_duration <= max_segment_duration + 0.05:
        return [SegmentSpec(
            start_seconds=0.0,
            duration_seconds=int(math.ceil(ref_duration)),
        )]

    target_per_seg = round(ref_duration / n_segments)
    segments: list[SegmentSpec] = []
    cursor = 0.0
    for i in range(n_segments):
        if i == n_segments - 1:
            # Final segment: extends to the end of the reference (rounded up to integer).
            duration = int(math.ceil(ref_duration - cursor))
        else:
            ideal = cursor + target_per_seg
            candidates = [
                b for b in boundaries
                if abs(b - ideal) <= boundary_window
                and b > cursor + 0.5
                and b < ref_duration - 1.0
            ]
            if candidates:
                cut_at = min(candidates, key=lambda b: abs(b - ideal))
            else:
                # No usable shot boundary near the ideal point — fall back to even split.
                cut_at = ideal
            duration = int(round(cut_at - cursor))
            duration = max(min_segment_duration, min(max_segment_duration, duration))
        segments.append(SegmentSpec(start_seconds=cursor, duration_seconds=duration))
        cursor += duration
    return segments


async def extract_reference_clips(
    source: Path,
    segments: list[SegmentSpec],
) -> list[Path]:
    """Cut ``source`` into one .mp4 per SegmentSpec. Caches by (stem, start, dur).

    Cache layout
    ------------
    ``%APPDATA%/minipic/clips/<source.stem>/clip_NN_tSSSS_dDDDD.mp4``

    Re-running the same plan with the same source skips re-encoding for any
    clip whose output file already exists. This means commit can be retried
    cheaply after a failed H3 submission.
    """
    if not source.is_file():
        raise MediaError(f"video not found: {source}")
    if not segments:
        raise MediaError("extract_reference_clips: segments is empty")
    ffmpeg = find_ffmpeg()
    cache_dir = user_data_path("clips") / source.stem
    cache_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    for i, seg in enumerate(segments):
        out = cache_dir / (
            f"clip_{i:02d}_t{int(seg.start_seconds):04d}_d{int(seg.duration_seconds):04d}.mp4"
        )
        if not out.is_file():
            cmd = [
                ffmpeg, "-y", "-loglevel", "error",
                "-ss", f"{seg.start_seconds:.3f}",
                "-i", str(source),
                "-t", f"{seg.duration_seconds:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(out),
            ]
            log.debug("ffmpeg extract segment %d: %s", i, " ".join(cmd))
            try:
                await asyncio.to_thread(subprocess.run, cmd, check=True, timeout=120)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                raise MediaError(
                    f"ffmpeg extract failed for segment {i} ({seg.label()}): {e}"
                ) from e
        extracted.append(out)
    return extracted


def concat_videos(clip_paths: list[Path], out_path: Path) -> Path:
    """Concatenate ``clip_paths`` into ``out_path`` using the ffmpeg concat demuxer.

    Single-clip fast path: just copies the file. Hard-cut (no crossfade) — v0.1.3b
    keeps it simple. ``out_path`` parent dirs are created as needed.
    """
    if not clip_paths:
        raise MediaError("concat_videos: clip_paths is empty")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], out_path)
        return out_path
    ffmpeg = find_ffmpeg()
    list_file = out_path.with_suffix(out_path.suffix + ".concat.txt")
    try:
        list_file.write_text(
            "\n".join(f"file '{_concat_escape(p.resolve())}'" for p in clip_paths),
            encoding="utf-8",
        )
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy",
            str(out_path),
        ]
        log.debug("ffmpeg concat: %s", " ".join(cmd))
        subprocess.run(cmd, check=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        raise MediaError(f"ffmpeg concat failed: {e}") from e
    finally:
        list_file.unlink(missing_ok=True)
    return out_path


def _concat_escape(p: Path) -> str:
    """Escape a path for ffmpeg concat demuxer's ``file '...'`` directive.

    On Windows paths may contain a colon (e.g. ``C:\...``) which the demuxer
    treats as an option prefix. We also escape single quotes.
    """
    s = str(p)
    return s.replace("'", "'\\''")

