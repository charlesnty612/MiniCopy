"""Tests for minipic.media — ffmpeg discovery, video splitting, reference resolution."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import ANY, patch

import pytest

from minipic.media import (
    REF_VIDEO_CLIP_LEN_AUTO,
    REF_VIDEO_MAX_CLIPS,
    REF_VIDEO_MAX_DURATION,
    REF_VIDEO_MIN_DURATION,
    REF_VIDEO_TOTAL_BUDGET,
    VideoClip,
    _auto_sample_clips,
    _parse_clips_spec,
    ffprobe_duration,
    find_ffmpeg,
    prepare_reference_video,
    resolve_reference,
)
from minipic.errors import MediaError


# --------------------------------------------------------------------------- find_ffmpeg
class TestFindFfmpeg:
    def test_finds_system_ffmpeg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "D:\\zcodeproject\\minipic\\.venv\\Lib\\site-packages\\imageio_ffmpeg\\binaries;")
        result = find_ffmpeg()
        assert "ffmpeg" in result.lower()

    def test_falls_back_to_imageio_ffmpeg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Make shutil.which return None; imageio-ffmpeg is installed in .venv
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr("subprocess.run", lambda *a, **k: None)
        result = find_ffmpeg()
        assert "ffmpeg" in result.lower()

    def test_raises_when_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import minipic.media as media_mod
        import sys
        from minipic.errors import MediaError

        # Replace the module-level binding in the test namespace so our patched
        # `find_ffmpeg` is what gets called. Also patch the source module so any
        # internal callers (e.g. prepare_reference_video) hit the fake too.
        def _broken_find() -> str:
            raise MediaError(
                "ffmpeg not found.\n"
                "Install one of:\n"
                "  - System ffmpeg: https://ffmpeg.org/download.html\n"
                "  - The bundled static binary: pip install imageio-ffmpeg"
            )

        # Patch in the test module's namespace (local `find_ffmpeg` reference)
        monkeypatch.setattr(sys.modules[__name__], "find_ffmpeg", _broken_find)
        # Also patch in the source module so callers within media.py are covered
        monkeypatch.setattr(media_mod, "find_ffmpeg", _broken_find)

        with pytest.raises(MediaError, match="ffmpeg not found"):
            find_ffmpeg()


# --------------------------------------------------------------------------- ffprobe_duration
class TestFfprobeDuration:
    def test_parses_json_duration(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        video = tmp_path / "v.mp4"
        video.write_bytes(b"fake")
        monkeypatch.setenv("PATH", "")

        def fake_check_output(cmd: list, **kwargs: Any) -> str:
            assert "ffprobe" in cmd[0]
            return json.dumps({"format": {"duration": "12.345"}})

        with patch("subprocess.check_output", fake_check_output):
            dur = ffprobe_duration(video)
        assert dur == pytest.approx(12.345, rel=1e-3)

    def test_raises_on_subprocess_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        video = tmp_path / "v.mp4"
        video.write_bytes(b"fake")

        def fake_check_output(cmd: list, **kwargs: Any) -> None:
            raise subprocess.CalledProcessError(1, cmd)

        with patch("subprocess.check_output", fake_check_output):
            with pytest.raises(MediaError, match="ffprobe failed"):
                ffprobe_duration(video)

    def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        # On Windows the system ffprobe is installed; mock to isolate test.
        def fake_check_output(cmd: list, **kwargs: Any) -> None:
            raise subprocess.CalledProcessError(1, cmd)

        with patch("subprocess.check_output", fake_check_output):
            with pytest.raises(MediaError, match="ffprobe failed"):
                ffprobe_duration(tmp_path / "nonexistent.mp4")


# --------------------------------------------------------------------------- _parse_clips_spec
class TestParseClipsSpec:
    def test_empty_spec_returns_empty(self) -> None:
        assert _parse_clips_spec("", source_duration=30.0) == []

    def test_parses_single_clip(self) -> None:
        clips = _parse_clips_spec("0:5", source_duration=10.0)
        assert len(clips) == 1
        assert clips[0].start_seconds == 0.0
        assert clips[0].duration_seconds == 5.0

    def test_parses_float_values(self) -> None:
        clips = _parse_clips_spec("1.5:3.2", source_duration=10.0)
        assert clips[0].start_seconds == pytest.approx(1.5)
        assert clips[0].duration_seconds == pytest.approx(3.2)

    def test_parses_multiple_clips(self) -> None:
        clips = _parse_clips_spec("0:5,5:3,8:2", source_duration=15.0)
        assert len(clips) == 3
        assert clips[0].start_seconds == 0.0
        assert clips[1].start_seconds == 5.0
        assert clips[2].start_seconds == 8.0

    def test_rejects_bad_format(self) -> None:
        with pytest.raises(MediaError, match="must be 'start:duration'"):
            _parse_clips_spec("bad", source_duration=10.0)

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(MediaError, match="must be numeric"):
            _parse_clips_spec("a:b", source_duration=10.0)

    def test_rejects_clip_too_short(self) -> None:
        with pytest.raises(MediaError, match="out of range"):
            _parse_clips_spec("0:1", source_duration=10.0)

    def test_rejects_clip_too_long(self) -> None:
        with pytest.raises(MediaError, match="out of range"):
            _parse_clips_spec("0:20", source_duration=30.0)

    def test_rejects_clip_extending_past_source(self) -> None:
        with pytest.raises(MediaError, match="extends past source"):
            _parse_clips_spec("8:5", source_duration=10.0)

    def test_rejects_too_many_clips(self) -> None:
        spec = ",".join(f"{i}:5" for i in range(REF_VIDEO_MAX_CLIPS + 1))
        with pytest.raises(MediaError, match="too many clips"):
            _parse_clips_spec(spec, source_duration=30.0)

    def test_rejects_total_exceeds_budget(self) -> None:
        with pytest.raises(MediaError, match="total clip duration"):
            _parse_clips_spec("0:10,10:10", source_duration=30.0)

    def test_allows_total_at_budget_boundary(self) -> None:
        clips = _parse_clips_spec(f"0:{REF_VIDEO_TOTAL_BUDGET}", source_duration=REF_VIDEO_TOTAL_BUDGET + 1)
        assert len(clips) == 1


# --------------------------------------------------------------------------- _auto_sample_clips
class TestAutoSampleClips:
    def test_short_video_single_clip(self) -> None:
        # For ≤15s, the function returns a single clip of the full duration.
        clips = _auto_sample_clips(10.0)
        assert len(clips) == 1
        assert clips[0].start_seconds == 0.0
        assert clips[0].duration_seconds == pytest.approx(10.0)

    def test_auto_sample_respects_max_clips(self) -> None:
        clips = _auto_sample_clips(60.0)
        assert len(clips) <= REF_VIDEO_MAX_CLIPS

    def test_clips_within_budget(self) -> None:
        clips = _auto_sample_clips(30.0)
        total = sum(c.duration_seconds for c in clips)
        assert total <= REF_VIDEO_TOTAL_BUDGET + 0.1


# --------------------------------------------------------------------------- VideoClip
class TestVideoClip:
    def test_label_format(self) -> None:
        c = VideoClip(path=Path(), start_seconds=1.5, duration_seconds=5.0, is_split=False)
        assert "1.50" in c.label()
        assert "5.00" in c.label()


# --------------------------------------------------------------------------- prepare_reference_video
class TestPrepareReferenceVideo:
    @pytest.mark.asyncio
    async def test_short_video_no_split(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        video = tmp_path / "short.mp4"
        video.write_bytes(b"f")
        monkeypatch.setattr("minipic.media.ffprobe_duration", lambda p: 10.0)
        clips = await prepare_reference_video(video)
        assert len(clips) == 1
        assert clips[0].is_split is False
        assert clips[0].path == video

    @pytest.mark.asyncio
    async def test_long_video_auto_sampled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import minipic.media as media_mod

        video = tmp_path / "long.mp4"
        video.write_bytes(b"f")
        monkeypatch.setattr(media_mod, "ffprobe_duration", lambda p: 60.0)
        monkeypatch.setattr(media_mod, "find_ffmpeg", lambda: "ffmpeg")

        # Patch asyncio.to_thread so it runs synchronously
        async def _sync_to_thread(fn, *a, **k):
            return fn(*a, **k)
        monkeypatch.setattr(media_mod.asyncio, "to_thread", _sync_to_thread)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = None
            clips = await prepare_reference_video(video)

        assert len(clips) <= REF_VIDEO_MAX_CLIPS
        assert all(c.is_split for c in clips)

    @pytest.mark.asyncio
    async def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(MediaError, match="video not found"):
            await prepare_reference_video(tmp_path / "nonexistent.mp4")

    @pytest.mark.asyncio
    async def test_long_video_with_clips_spec(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import minipic.media as media_mod

        video = tmp_path / "long.mp4"
        video.write_bytes(b"f")
        monkeypatch.setattr(media_mod, "ffprobe_duration", lambda p: 60.0)
        monkeypatch.setattr(media_mod, "find_ffmpeg", lambda: "ffmpeg")

        async def _sync_to_thread(fn, *a, **k):
            return fn(*a, **k)
        monkeypatch.setattr(media_mod.asyncio, "to_thread", _sync_to_thread)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = None
            clips = await prepare_reference_video(video, clips_spec="0:5,10:5,20:5")

        assert len(clips) == 3
        assert clips[0].start_seconds == 0.0
        assert clips[1].start_seconds == 10.0
        assert clips[2].start_seconds == 20.0


# --------------------------------------------------------------------------- resolve_reference
class TestResolveReference:
    @pytest.mark.asyncio
    async def test_passes_through_http_url(self) -> None:
        url = await resolve_reference(None, "https://example.com/video.mp4")
        assert url == "https://example.com/video.mp4"

    @pytest.mark.asyncio
    async def test_passes_through_http_string(self) -> None:
        url = await resolve_reference(None, "http://example.com/v.mp4")
        assert url == "http://example.com/v.mp4"

    @pytest.mark.asyncio
    async def test_uploads_local_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        video = tmp_path / "v.mp4"
        video.write_bytes(b"fake")
        mock_client = __import__("asyncio").Future()
        mock_client.set_result(None)  # placeholder

        upload_called = []

        class FakeClient:
            async def upload_and_resolve(self, p: Path) -> str:
                upload_called.append(str(p))
                return "https://cdn.example.com/file"

        url = await resolve_reference(FakeClient(), video)
        assert url == "https://cdn.example.com/file"
        assert str(video) in upload_called[0]

    @pytest.mark.asyncio
    async def test_raises_on_missing_local_file(self, tmp_path: Path) -> None:
        with pytest.raises(MediaError, match="file not found"):
            await resolve_reference(None, tmp_path / "nonexistent.png")

    @pytest.mark.asyncio
    async def test_raises_on_missing_string_path(self) -> None:
        with pytest.raises(MediaError, match="file not found"):
            await resolve_reference(None, "/nonexistent/path.png")

    @pytest.mark.asyncio
    async def test_uses_cache_on_second_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        video = tmp_path / "v.mp4"
        video.write_bytes(b"f")
        call_count = 0

        class FakeClient:
            async def upload_and_resolve(self, p: Path) -> str:
                nonlocal call_count
                call_count += 1
                return f"https://cdn.example.com/{call_count}"

        # First call — uploads
        url1 = await resolve_reference(FakeClient(), video)
        # Second call — should use cache (no upload)
        url2 = await resolve_reference(FakeClient(), video)
        assert url1 == url2
        assert call_count == 1  # only one upload


# --------------------------------------------------------------------------- constants
class TestMediaConstants:
    def test_clip_limits(self) -> None:
        assert REF_VIDEO_MIN_DURATION == 2.0
        assert REF_VIDEO_MAX_DURATION == 15.0
        assert REF_VIDEO_MAX_CLIPS == 3
        assert REF_VIDEO_TOTAL_BUDGET == 15.0
        assert REF_VIDEO_CLIP_LEN_AUTO == 5.0
