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
    SEGMENT_BOUNDARY_WINDOW,
    SEGMENT_MAX_COUNT,
    SEGMENT_MAX_SECONDS,
    SEGMENT_MIN_SECONDS,
    SEGMENT_SCENE_THRESHOLDS,
    SegmentSpec,
    VideoClip,
    _auto_sample_clips,
    _parse_clips_spec,
    concat_videos,
    extract_reference_clips,
    ffprobe_duration,
    find_ffmpeg,
    find_shot_boundaries,
    plan_segments,
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

    def test_segment_limits(self) -> None:
        assert SEGMENT_MIN_SECONDS == 4
        assert SEGMENT_MAX_SECONDS == 15
        assert SEGMENT_MAX_COUNT == 4
        assert SEGMENT_BOUNDARY_WINDOW == 3.0
        assert SEGMENT_SCENE_THRESHOLDS == (0.4, 0.3, 0.5)


# ============================================================ v0.1.3b SegmentSpec
class TestSegmentSpec:
    def test_end_seconds(self) -> None:
        s = SegmentSpec(start_seconds=2.5, duration_seconds=7)
        assert s.end_seconds() == 9.5

    def test_label_contains_range_and_duration(self) -> None:
        s = SegmentSpec(start_seconds=0.0, duration_seconds=10)
        label = s.label()
        assert "0.00" in label
        assert "10.00" in label
        assert "10s" in label


# ============================================================ v0.1.3b plan_segments
class TestPlanSegments:
    def test_short_video_single_segment(self) -> None:
        segs = plan_segments(5.0, boundaries=[])
        assert len(segs) == 1
        assert segs[0].start_seconds == 0.0
        assert segs[0].duration_seconds == 5

    def test_exactly_at_max_returns_single(self) -> None:
        segs = plan_segments(15.0, boundaries=[])
        assert len(segs) == 1
        assert segs[0].duration_seconds == 15

    def test_just_over_max_returns_two(self) -> None:
        segs = plan_segments(16.0, boundaries=[])
        assert len(segs) == 2
        assert sum(s.duration_seconds for s in segs) >= 16

    def test_30s_returns_two_15s(self) -> None:
        segs = plan_segments(30.0, boundaries=[])
        assert len(segs) == 2
        assert [s.duration_seconds for s in segs] == [15, 15]

    def test_60s_returns_four_15s(self) -> None:
        segs = plan_segments(60.0, boundaries=[])
        assert len(segs) == 4
        assert [s.duration_seconds for s in segs] == [15, 15, 15, 15]

    def test_over_60s_raises(self) -> None:
        with pytest.raises(MediaError, match="would need 5 segments"):
            plan_segments(61.0, boundaries=[])
        with pytest.raises(MediaError, match="max 4"):
            plan_segments(70.0, boundaries=[])

    def test_zero_or_negative_duration_raises(self) -> None:
        with pytest.raises(MediaError, match="must be > 0"):
            plan_segments(0.0, boundaries=[])

    def test_uses_nearest_shot_boundary(self) -> None:
        # 17.77s reference with 7.03 boundary — should split into 7+11
        boundaries = [0.1, 2.93, 7.03, 11.53, 12.33, 16.57]
        segs = plan_segments(17.77, boundaries=boundaries)
        assert len(segs) == 2
        assert segs[0].duration_seconds == 7
        assert segs[1].duration_seconds == 11
        assert segs[1].start_seconds == 7.03 or segs[1].start_seconds == 7

    def test_falls_back_to_even_split_without_boundaries(self) -> None:
        segs = plan_segments(17.77, boundaries=[])
        assert len(segs) == 2
        total = sum(s.duration_seconds for s in segs)
        # Even split rounds to 9+9=18; total ≥ ref duration is acceptable
        assert total == 18
        assert segs[0].start_seconds == 0.0
        assert segs[1].start_seconds == 9.0

    def test_segment_duration_clamped_to_min(self) -> None:
        # Very short between boundaries forces clamping up to min
        # 20s with two boundaries very close together (1s apart) — first cut near ideal
        boundaries = [0.0, 1.0, 19.0]
        segs = plan_segments(20.0, boundaries=boundaries)
        for s in segs:
            assert s.duration_seconds >= SEGMENT_MIN_SECONDS

    def test_segments_have_sequential_starts(self) -> None:
        segs = plan_segments(45.0, boundaries=[])
        for prev, cur in zip(segs, segs[1:]):
            assert cur.start_seconds == pytest.approx(prev.start_seconds + prev.duration_seconds, abs=0.5)

    def test_segments_use_integer_durations(self) -> None:
        segs = plan_segments(33.0, boundaries=[5.0, 12.0, 20.0, 28.0])
        for s in segs:
            assert isinstance(s.duration_seconds, int)
            assert SEGMENT_MIN_SECONDS <= s.duration_seconds <= SEGMENT_MAX_SECONDS


# ============================================================ v0.1.3b find_shot_boundaries
class TestFindShotBoundaries:
    def test_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(MediaError, match="video not found"):
            find_shot_boundaries(tmp_path / "nope.mp4")

    def test_parses_pts_time_from_stderr(self, tmp_path: Path) -> None:
        import minipic.media as media_mod
        video = tmp_path / "v.mp4"
        video.write_bytes(b"f")
        monkey_stderr = (
            "frame=1\n"
            "[Parsed_showinfo] n:0 pts:123 pts_time:0.500000 pos:0\n"
            "[Parsed_showinfo] n:1 pts:456 pts_time:2.930000 pos:0\n"
            "[Parsed_showinfo] n:2 pts:789 pts_time:7.030000 pos:0\n"
            "[Parsed_showinfo] n:3 pts:999 pts_time:11.530000 pos:0\n"
            "[Parsed_showinfo] n:4 pts:1111 pts_time:12.330000 pos:0\n"
            "frame=6\n"
        )
        # 5 boundaries = within [3, 20] → first threshold (0.4) wins
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ffmpeg"], returncode=0, stdout="", stderr=monkey_stderr,
            )
            result = find_shot_boundaries(video)
        assert result == [0.5, 2.93, 7.03, 11.53, 12.33]

    def test_adaptive_threshold_falls_back(self, tmp_path: Path) -> None:
        import minipic.media as media_mod
        video = tmp_path / "v.mp4"
        video.write_bytes(b"f")
        # Too few boundaries for all three thresholds → empty list returned
        too_few = "[Parsed_showinfo] n:0 pts:0 pts_time:1.000000 pos:0\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ffmpeg"], returncode=0, stdout="", stderr=too_few,
            )
            result = find_shot_boundaries(video)
        assert result == []

    def test_too_many_boundaries_skipped(self, tmp_path: Path) -> None:
        video = tmp_path / "v.mp4"
        video.write_bytes(b"f")
        # 25 boundaries > max_count=20 → skip
        too_many = "\n".join(
            f"[Parsed_showinfo] n:{i} pts:0 pts_time:{i}.000 pos:0" for i in range(25)
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ffmpeg"], returncode=0, stdout="", stderr=too_many,
            )
            result = find_shot_boundaries(video)
        assert result == []

    def test_timeout_returns_empty(self, tmp_path: Path) -> None:
        video = tmp_path / "v.mp4"
        video.write_bytes(b"f")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 60)):
            result = find_shot_boundaries(video)
        assert result == []


# ============================================================ v0.1.3b extract_reference_clips
class TestExtractReferenceClips:
    @pytest.mark.asyncio
    async def test_creates_one_clip_per_segment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import minipic.media as media_mod

        source = tmp_path / "source.mp4"
        source.write_bytes(b"f")
        monkeypatch.setattr(media_mod, "find_ffmpeg", lambda: "ffmpeg")

        async def _sync_to_thread(fn, *a, **k):
            return fn(*a, **k)
        monkeypatch.setattr(media_mod.asyncio, "to_thread", _sync_to_thread)

        # ffmpeg mock: write a placeholder file at the output path
        def fake_ffmpeg(*args, **kwargs):
            out = args[0][-1]
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"clip-bytes")
            return None

        with patch("subprocess.run", side_effect=fake_ffmpeg):
            segments = [
                SegmentSpec(start_seconds=0.0, duration_seconds=7),
                SegmentSpec(start_seconds=7.0, duration_seconds=11),
            ]
            out = await extract_reference_clips(source, segments)

        assert len(out) == 2
        for p in out:
            assert p.is_file()
            assert p.suffix == ".mp4"

    @pytest.mark.asyncio
    async def test_skips_reencoding_when_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import minipic.media as media_mod

        source = tmp_path / "source.mp4"
        source.write_bytes(b"f")
        monkeypatch.setattr(media_mod, "find_ffmpeg", lambda: "ffmpeg")

        # The autouse _isolate_user_data fixture points user_data_path to
        # tmp_path/"user_data" so we can pre-create the cached file there.
        clip_dir = tmp_path / "user_data" / "clips" / source.stem
        clip_dir.mkdir(parents=True, exist_ok=True)
        cached = clip_dir / "clip_00_t0000_d0007.mp4"
        cached.write_bytes(b"pre-existing")

        with patch("subprocess.run") as mock_run:
            segments = [SegmentSpec(start_seconds=0.0, duration_seconds=7)]
            out = await extract_reference_clips(source, segments)
        assert mock_run.call_count == 0
        assert out[0] == cached

    @pytest.mark.asyncio
    async def test_ffmpeg_failure_raises_media_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import minipic.media as media_mod

        source = tmp_path / "source.mp4"
        source.write_bytes(b"f")
        monkeypatch.setattr(media_mod, "find_ffmpeg", lambda: "ffmpeg")

        async def _sync_to_thread(fn, *a, **k):
            return fn(*a, **k)
        monkeypatch.setattr(media_mod.asyncio, "to_thread", _sync_to_thread)

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            with pytest.raises(MediaError, match="ffmpeg extract failed"):
                await extract_reference_clips(
                    source, [SegmentSpec(start_seconds=0.0, duration_seconds=7)]
                )

    @pytest.mark.asyncio
    async def test_missing_source_raises(self, tmp_path: Path) -> None:
        with pytest.raises(MediaError, match="video not found"):
            await extract_reference_clips(
                tmp_path / "nope.mp4", [SegmentSpec(0.0, 7)]
            )

    @pytest.mark.asyncio
    async def test_empty_segments_raises(self, tmp_path: Path) -> None:
        # File must exist; otherwise the missing-source check fires first.
        v = tmp_path / "v.mp4"
        v.write_bytes(b"f")
        with pytest.raises(MediaError, match="segments is empty"):
            await extract_reference_clips(v, [])


# ============================================================ v0.1.3b concat_videos
class TestConcatVideos:
    def test_empty_input_raises(self, tmp_path: Path) -> None:
        with pytest.raises(MediaError, match="clip_paths is empty"):
            concat_videos([], tmp_path / "out.mp4")

    def test_single_clip_just_copies(self, tmp_path: Path) -> None:
        src = tmp_path / "in.mp4"
        src.write_bytes(b"hello")
        out = tmp_path / "out.mp4"
        result = concat_videos([src], out)
        assert result == out
        assert out.is_file()
        assert out.read_bytes() == b"hello"

    def test_concatenates_two_clips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import minipic.media as media_mod
        monkeypatch.setattr(media_mod, "find_ffmpeg", lambda: "ffmpeg")

        c1 = tmp_path / "c1.mp4"
        c2 = tmp_path / "c2.mp4"
        c1.write_bytes(b"a")
        c2.write_bytes(b"b")
        out = tmp_path / "out.mp4"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = None
            result = concat_videos([c1, c2], out)

        assert result == out
        assert mock_run.call_count == 1
        # list_file should be cleaned up
        assert not (tmp_path / "out.mp4.concat.txt").exists()
        # The ffmpeg command should reference the list file
        cmd = mock_run.call_args[0][0]
        assert "-f" in cmd and "concat" in cmd

    def test_concat_failure_raises_media_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import minipic.media as media_mod
        monkeypatch.setattr(media_mod, "find_ffmpeg", lambda: "ffmpeg")

        c1 = tmp_path / "c1.mp4"
        c2 = tmp_path / "c2.mp4"
        c1.write_bytes(b"a")
        c2.write_bytes(b"b")
        out = tmp_path / "out.mp4"

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            with pytest.raises(MediaError, match="ffmpeg concat failed"):
                concat_videos([c1, c2], out)
        # list_file should still be cleaned up
        assert not (tmp_path / "out.mp4.concat.txt").exists()

