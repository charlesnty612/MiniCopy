"""Tests for minipic.media — ffmpeg discovery, reference resolution, ref-video validation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minipic.media import (
    REF_VIDEO_MAX_DURATION,
    REF_VIDEO_MIN_DURATION,
    VideoClip,
    build_content,
    download_task_result,
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
    async def test_exactly_at_cap_returns_single_clip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A video exactly at the 15s cap is accepted (boundary)."""
        video = tmp_path / "edge.mp4"
        video.write_bytes(b"f")
        monkeypatch.setattr("minipic.media.ffprobe_duration", lambda p: 15.0)
        clips = await prepare_reference_video(video)
        assert len(clips) == 1
        assert clips[0].path == video
        assert clips[0].duration_seconds == pytest.approx(15.0)

    @pytest.mark.asyncio
    async def test_long_video_rejected_with_chinese_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A video >15s raises MediaError with the Chinese user-facing message.

        v0.1.5+: multi-segment generation was removed; the user must pre-trim
        their footage with ffmpeg before submitting.
        """
        video = tmp_path / "long.mp4"
        video.write_bytes(b"f")
        monkeypatch.setattr("minipic.media.ffprobe_duration", lambda p: 17.77)
        with pytest.raises(MediaError) as ei:
            await prepare_reference_video(video)
        msg = str(ei.value)
        assert "超过 H3 上限 15s" in msg
        assert "多段生成已移除" in msg
        assert "ffmpeg" in msg

    @pytest.mark.asyncio
    async def test_long_video_rejected_just_over_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anything past the 15.05s tolerance is rejected."""
        video = tmp_path / "just_over.mp4"
        video.write_bytes(b"f")
        monkeypatch.setattr("minipic.media.ffprobe_duration", lambda p: 15.5)
        with pytest.raises(MediaError, match="超过 H3 上限 15s"):
            await prepare_reference_video(video)

    @pytest.mark.asyncio
    async def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(MediaError, match="video not found"):
            await prepare_reference_video(tmp_path / "nonexistent.mp4")


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
    def test_ref_video_limits(self) -> None:
        assert REF_VIDEO_MIN_DURATION == 2.0
        assert REF_VIDEO_MAX_DURATION == 15.0


# --------------------------------------------------------------------------- build_content
class TestBuildContent:
    """build_content is the single source of truth for the V2 content[] shape
    used by both CLI and Web. These tests guard against the previous drift
    (CLI accidentally nesting text, Web using the right shape — same call site
    today).
    """

    @pytest.mark.asyncio
    async def test_t2v_emits_only_text(self) -> None:
        # T2V: no references — content[] is just the text item, flat string.
        client = object()  # not touched for text-only
        content = await build_content(prompt="a cat", client=client)  # type: ignore[arg-type]
        assert content == [{"type": "text", "text": "a cat"}]
        # Crucial: text is NOT nested.
        assert not isinstance(content[0]["text"], dict)

    @pytest.mark.asyncio
    async def test_text_is_never_nested(self) -> None:
        """Regression guard: the old CLI bug was ``{"text": {"text": ...}}``.

        This asserts the official flat shape is what the shared builder emits.
        """
        client = object()
        content = await build_content(prompt="hello world", client=client)  # type: ignore[arg-type]
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "hello world"
        assert "text" not in content[0] or not isinstance(content[0]["text"], dict)

    @pytest.mark.asyncio
    async def test_i2v_first_frame_image_passed_through(self) -> None:
        client = object()
        content = await build_content(
            prompt="a scene",
            client=client,  # type: ignore[arg-type]
            ref_images=[{"path": "https://cdn/first.jpg", "role": "first_frame"}],
        )
        assert content[0] == {"type": "text", "text": "a scene"}
        img = content[1]
        assert img["type"] == "image_url"
        assert img["image_url"]["url"] == "https://cdn/first.jpg"
        assert img["role"] == "first_frame"

    @pytest.mark.asyncio
    async def test_r2v_video_carries_duration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Local video → prepare_reference_video (stubbed) → resolve_reference (stubbed)
        v = tmp_path / "v.mp4"
        v.write_bytes(b"x")
        from minipic.media import VideoClip

        async def fake_prep(source: Any) -> list[VideoClip]:
            return [VideoClip(path=source, start_seconds=0.0,
                              duration_seconds=10.0, is_split=False)]
        monkeypatch.setattr("minipic.media.prepare_reference_video", fake_prep)

        class FakeClient:
            async def upload_and_resolve(self, p: Path) -> str:
                return f"https://cdn/{p.name}"
        content = await build_content(
            prompt="x",
            client=FakeClient(),  # type: ignore[arg-type]
            ref_videos=[{"path": str(v)}],
        )
        vid = [c for c in content if c.get("type") == "video_url"][0]
        assert vid["video_url"]["url"] == f"https://cdn/{v.name}"
        assert vid["role"] == "reference_video"
        assert vid["duration"] == 10.0

    @pytest.mark.asyncio
    async def test_remote_url_passes_through_without_upload(self) -> None:
        # A remote URL must NOT trigger an upload — both for image and video.
        class FakeClient:
            called: list[Any] = []
            async def upload_and_resolve(self, p: Path) -> str:
                raise AssertionError("should not be called for remote URLs")

        content = await build_content(
            prompt="x",
            client=FakeClient(),  # type: ignore[arg-type]
            ref_images=[{"path": "https://cdn/a.jpg"}],
            ref_videos=[{"path": "https://cdn/v.mp4"}],
        )
        # 1 text + 1 image + 1 video = 3 items
        assert len(content) == 3
        img = [c for c in content if c.get("type") == "image_url"][0]
        vid = [c for c in content if c.get("type") == "video_url"][0]
        assert img["image_url"]["url"] == "https://cdn/a.jpg"
        assert vid["video_url"]["url"] == "https://cdn/v.mp4"
        # Remote video has no duration (only local clips carry it).
        assert "duration" not in vid

    @pytest.mark.asyncio
    async def test_local_image_resolved_via_upload(
        self, tmp_path: Path
    ) -> None:
        img = tmp_path / "img.jpg"
        img.write_bytes(b"x")

        class FakeClient:
            async def upload_and_resolve(self, p: Path) -> str:
                return "https://cdn/uploaded.jpg"
        content = await build_content(
            prompt="x",
            client=FakeClient(),  # type: ignore[arg-type]
            ref_images=[{"path": str(img), "role": "reference_image"}],
        )
        img_item = [c for c in content if c.get("type") == "image_url"][0]
        assert img_item["image_url"]["url"] == "https://cdn/uploaded.jpg"
        assert img_item["role"] == "reference_image"


# --------------------------------------------------------------------------- download_task_result
class TestDownloadTaskResult:
    @pytest.mark.asyncio
    async def test_no_url_returns_false(self, tmp_path: Path) -> None:
        # A succeeded task with no content → silent False, no download attempted.
        client = MagicMock()
        ok = await download_task_result(
            client, {"content": []}, tmp_path / "out.mp4"  # type: ignore[arg-type]
        )
        assert ok is False
        # download_video must NOT have been called.
        client.download_video.assert_not_called()

    @pytest.mark.asyncio
    async def test_extracts_url_from_content_and_downloads(
        self, tmp_path: Path
    ) -> None:
        async def fake_dl(url: str, dest: Path) -> None:
            dest.write_bytes(b"mp4-bytes")
        client = MagicMock()
        client.download_video = AsyncMock(side_effect=fake_dl)
        task = {"content": [{"url": "https://cdn/v.mp4"}]}
        ok = await download_task_result(
            client, task, tmp_path / "out.mp4"  # type: ignore[arg-type]
        )
        assert ok is True
        out = tmp_path / "out.mp4"
        assert out.is_file()
        assert out.read_bytes() == b"mp4-bytes"
        # Atomic via .tmp + rename — no leftover .tmp
        assert not (tmp_path / "out.tmp").exists()
        client.download_video.assert_awaited_once()
        # The first arg to download_video must be the extracted URL.
        args = client.download_video.await_args.args
        assert args[0] == "https://cdn/v.mp4"

    @pytest.mark.asyncio
    async def test_falls_back_to_legacy_type_video_entry(
        self, tmp_path: Path
    ) -> None:
        """Older API shape: content[i] has type='video' with nested video.url."""
        async def fake_dl(url: str, dest: Path) -> None:
            dest.write_bytes(b"x")
        client = MagicMock()
        client.download_video = AsyncMock(side_effect=fake_dl)
        task = {"content": [{"type": "video", "video": {"url": "https://cdn/legacy.mp4"}}]}
        ok = await download_task_result(
            client, task, tmp_path / "out.mp4"  # type: ignore[arg-type]
        )
        assert ok is True
        client.download_video.assert_awaited_once()
        assert client.download_video.await_args.args[0] == "https://cdn/legacy.mp4"

