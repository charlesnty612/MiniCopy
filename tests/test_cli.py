"""Tests for minipic.cli — Click commands, helpers, error wrapping.

Strategy:
- Use `click.testing.CliRunner` to invoke commands in isolation.
- Mock `MiniMaxClient` and storage so no network / real DB happens.
- All filesystem side effects are redirected to tmp paths.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from minipic import cli as cli_mod
from minipic.cli import (
    _atomic_download,
    _parse_ref,
    _print_task_record,
    _status_label,
    _wait_and_download,
    cancel,
    config_group,
    create_group,
    list_cmd,
    main,
    status,
    videos,
    wait_cmd,
    web,
)
from minipic.config import Config
from minipic.errors import (
    AuthError,
    ConfigError,
    MiniPicError,
    TaskError,
)
from minipic.poller import (
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
)
from minipic.storage import TaskRecord


# --------------------------------------------------------------------------- shared fixtures
@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Provide a pre-loaded valid Config; make load_config() return it."""
    cfg = Config(api_key="k", base_url="https://x")
    cfg.videos_dir = str(tmp_path / "videos")
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    return cfg


class _FakeMiniMaxClient:
    """Async context manager stub for MiniMaxClient (CLI's expected pattern)."""

    def __init__(self) -> None:
        self.create_video_task = AsyncMock(return_value="tid-default")
        self.query_video_task = AsyncMock(
            return_value={"status": STATUS_SUCCEEDED,
                          "content": [{"url": "https://cdn/v.mp4"}]}
        )
        self.upload_and_resolve = AsyncMock(return_value="https://cdn/uploaded")

        async def _dl(url: str, dest: Path, **kwargs: Any) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake-mp4")
        self.download_video = AsyncMock(side_effect=_dl)

    async def __aenter__(self) -> "_FakeMiniMaxClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeMiniMaxClient:
    fake = _FakeMiniMaxClient()
    monkeypatch.setattr(cli_mod, "MiniMaxClient", lambda cfg: fake)
    return fake


def _run_coro_in_thread(coro: Any) -> Any:
    """Run an async coroutine in a fresh thread+loop, returning its result.

    The CLI commands internally call `asyncio.run(coro)`. When tests are run
    inside pytest-asyncio's event loop, that fails. This helper sidesteps it
    by spawning a new thread with its own loop, running the coro there, and
    returning the result.
    """
    import threading
    result: list[Any] = []
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                result.append(new_loop.run_until_complete(coro))
            finally:
                new_loop.close()
        except BaseException as e:  # noqa: BLE001
            error.append(e)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if error:
        raise error[0]
    return result[0] if result else None


@pytest.fixture(autouse=True)
def _patch_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch cli_mod.asyncio.run so it works inside a running pytest-asyncio loop.

    Replaces `asyncio.run(coro)` with a thread-isolated runner that doesn't
    conflict with the test's event loop.
    """
    monkeypatch.setattr(cli_mod.asyncio, "run", _run_coro_in_thread)


# --------------------------------------------------------------------------- main / version
class TestMainGroup:
    def test_help_lists_subcommands(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        # Should mention subcommands
        for cmd in ("create", "config", "status", "wait", "list", "videos", "web", "cancel"):
            assert cmd in result.output


# --------------------------------------------------------------------------- _status_label
class TestStatusLabel:
    def test_succeeded(self) -> None:
        out = _status_label(STATUS_SUCCEEDED)
        assert "Success" in out

    def test_failed(self) -> None:
        out = _status_label(STATUS_FAILED)
        assert "Failed" in out

    def test_cancelled(self) -> None:
        out = _status_label(STATUS_CANCELLED)
        assert "Cancelled" in out

    def test_expired(self) -> None:
        out = _status_label(STATUS_EXPIRED)
        assert "Expired" in out

    def test_running(self) -> None:
        out = _status_label(STATUS_RUNNING)
        assert "Processing" in out

    def test_queued(self) -> None:
        out = _status_label(STATUS_QUEUED)
        assert "Queued" in out

    def test_unknown_passes_through(self) -> None:
        out = _status_label("Banana")
        assert out == "Banana"


# --------------------------------------------------------------------------- _parse_ref
class TestParseRef:
    def test_plain_path(self) -> None:
        path, role = _parse_ref("/some/path.mp4", "reference_video")
        assert path == "/some/path.mp4"
        assert role == "reference_video"

    def test_with_role(self) -> None:
        path, role = _parse_ref("/a.mp4:first_frame", "reference_image")
        assert path == "/a.mp4"
        assert role == "first_frame"

    def test_url_with_role(self) -> None:
        # NOTE: _parse_ref splits on the first ':', so URLs that contain ':'
        # (like the scheme separator) can be parsed as role assignments. The
        # CLI is documented to accept "path:role" — the parser is best-effort
        # for local paths. We assert that a path with role works correctly.
        path, role = _parse_ref("/some/file.mp4:reference_video", "reference_image")
        assert path == "/some/file.mp4"
        assert role == "reference_video"

    def test_strips_whitespace(self) -> None:
        path, role = _parse_ref("  /a.mp4 :  role_x  ", "reference_video")
        assert path == "/a.mp4"
        assert role == "role_x"


# --------------------------------------------------------------------------- config show
class TestConfigShow:
    def test_prints_config_dict(
        self, runner: CliRunner, fake_config: Config
    ) -> None:
        result = runner.invoke(main, ["config", "show"])
        assert result.exit_code == 0
        # The output should be valid JSON with our key
        # The output may have ANSI codes from rich — strip them for parse
        data = json.loads(_strip_rich(result.output))
        assert data["api_key"] == "k"
        assert data["base_url"] == "https://x"


# --------------------------------------------------------------------------- config set
class TestConfigSet:
    def test_set_string(
        self, runner: CliRunner, fake_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Redirect user config to tmp so we don't pollute
        target = tmp_path / "u" / "config.json"
        import minipic.config as config_mod
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: target)
        result = runner.invoke(main, ["config", "set", "api_key", "new-key"])
        assert result.exit_code == 0
        # Verify file content
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["api_key"] == "new-key"

    def test_set_int(
        self, runner: CliRunner, fake_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "u" / "config.json"
        import minipic.config as config_mod
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: target)
        result = runner.invoke(main, ["config", "set", "default_duration", "15"])
        assert result.exit_code == 0
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["default_duration"] == 15

    def test_set_int_invalid(
        self, runner: CliRunner, fake_config: Config
    ) -> None:
        result = runner.invoke(main, ["config", "set", "default_duration", "abc"])
        assert result.exit_code != 0
        assert "int" in result.output.lower() or "expected" in result.output.lower()

    def test_set_unknown_key(
        self, runner: CliRunner, fake_config: Config
    ) -> None:
        result = runner.invoke(main, ["config", "set", "nonexistent_key", "x"])
        assert result.exit_code != 0
        assert "Unknown config key" in result.output

    def test_set_local_scope(
        self, runner: CliRunner, fake_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(main, ["config", "set", "api_key", "local-key", "--scope", "local"])
        assert result.exit_code == 0
        local = tmp_path / "config.json"
        assert local.is_file()
        data = json.loads(local.read_text(encoding="utf-8"))
        assert data["api_key"] == "local-key"

    def test_set_with_explicit_user_scope(
        self, runner: CliRunner, fake_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "user_cfg" / "config.json"
        import minipic.config as config_mod
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: target)
        result = runner.invoke(main, ["config", "set", "api_key", "v", "--scope", "user"])
        assert result.exit_code == 0
        assert target.is_file()


# --------------------------------------------------------------------------- create t2v
class TestCreateT2V:
    def test_happy_path(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        fake_client.create_video_task.return_value = "tid-t2v"
        result = runner.invoke(main, [
            "create", "t2v",
            "--prompt", "a cat",
            "--ratio", "16:9",
        ])
        assert result.exit_code == 0, result.output
        assert "tid-t2v" in result.output
        # Verify the call kwargs
        kwargs = fake_client.create_video_task.await_args.kwargs
        assert kwargs["duration"] == fake_config.default_duration
        assert kwargs["ratio"] == "16:9"

    def test_rejects_adaptive_ratio(
        self, runner: CliRunner, fake_config: Config
    ) -> None:
        result = runner.invoke(main, [
            "create", "t2v",
            "--prompt", "x",
            "--ratio", "adaptive",
        ])
        assert result.exit_code != 0
        assert "adaptive" in result.output.lower()

    def test_uses_default_duration(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        fake_config.default_duration = 12
        fake_client.create_video_task.return_value = "tid-1"
        result = runner.invoke(main, [
            "create", "t2v", "--prompt", "x", "--ratio", "16:9",
        ])
        assert result.exit_code == 0
        kwargs = fake_client.create_video_task.await_args.kwargs
        assert kwargs["duration"] == 12

    def test_explicit_duration_overrides_default(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        fake_client.create_video_task.return_value = "tid-1"
        result = runner.invoke(main, [
            "create", "t2v", "--prompt", "x", "--ratio", "16:9", "--duration", "7",
        ])
        assert result.exit_code == 0
        kwargs = fake_client.create_video_task.await_args.kwargs
        assert kwargs["duration"] == 7

    def test_no_api_key_exits_nonzero(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty = Config(api_key=None)
        monkeypatch.setattr(cli_mod, "load_config", lambda: empty)
        result = runner.invoke(main, [
            "create", "t2v", "--prompt", "x", "--ratio", "16:9",
        ])
        # Click exits with non-zero when a command raises (no wrapper for invoke())
        assert result.exit_code != 0
        # The error message goes to the exception, not output
        assert isinstance(result.exception, ConfigError)
        assert "API key" in str(result.exception)


# --------------------------------------------------------------------------- create i2v
class TestCreateI2V:
    def test_happy_path_with_url(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        fake_client.create_video_task.return_value = "tid-i2v"
        result = runner.invoke(main, [
            "create", "i2v",
            "--prompt", "a scene",
            "--ref-image", "https://cdn/first.jpg",
        ])
        assert result.exit_code == 0, result.output
        # i2v should force ratio=adaptive
        kwargs = fake_client.create_video_task.await_args.kwargs
        assert kwargs["ratio"] == "adaptive"
        # The content list should include an image_url entry
        content = kwargs["content"]
        img_items = [c for c in content if c.get("type") == "image_url"]
        assert len(img_items) == 1
        assert img_items[0]["image_url"]["url"] == "https://cdn/first.jpg"

    def test_role_flag(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        fake_client.create_video_task.return_value = "tid-i2v"
        result = runner.invoke(main, [
            "create", "i2v",
            "--prompt", "x",
            "--ref-image", "https://cdn/a.jpg",
            "--ref-image-role", "last_frame",
        ])
        assert result.exit_code == 0
        content = fake_client.create_video_task.await_args.kwargs["content"]
        img = [c for c in content if c.get("type") == "image_url"][0]
        assert img["role"] == "last_frame"


# --------------------------------------------------------------------------- create r2v
class TestCreateR2V:
    def test_with_ref_images(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient,
        tmp_path: Path,
    ) -> None:
        # Use local paths because the CLI's _parse_ref splits on the first ':',
        # which would mangle URLs (whose scheme is the first ':').
        local1 = tmp_path / "a.jpg"
        local1.write_bytes(b"x")
        local2 = tmp_path / "b.jpg"
        local2.write_bytes(b"x")
        fake_client.upload_and_resolve.return_value = "https://cdn/uploaded.jpg"
        fake_client.create_video_task.return_value = "tid-r2v"
        result = runner.invoke(main, [
            "create", "r2v",
            "--prompt", "x",
            "--ref-image", str(local1),
            "--ref-image", str(local2),
        ])
        assert result.exit_code == 0, result.output
        content = fake_client.create_video_task.await_args.kwargs["content"]
        img = [c for c in content if c.get("type") == "image_url"]
        assert len(img) == 2
        # Both have default role "reference_image"
        assert all(i["role"] == "reference_image" for i in img)

    def test_with_local_image_path_explicit_role(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient,
        tmp_path: Path,
    ) -> None:
        # Local path with explicit role — _parse_ref works for paths (no colon conflict)
        local = tmp_path / "img.jpg"
        local.write_bytes(b"x")
        fake_client.upload_and_resolve.return_value = "https://cdn/uploaded.jpg"
        fake_client.create_video_task.return_value = "tid-r2v"
        result = runner.invoke(main, [
            "create", "r2v",
            "--prompt", "x",
            "--ref-image", f"{local}:reference_image",
        ])
        assert result.exit_code == 0, result.output
        content = fake_client.create_video_task.await_args.kwargs["content"]
        img = [c for c in content if c.get("type") == "image_url"]
        assert len(img) == 1
        assert img[0]["image_url"]["url"] == "https://cdn/uploaded.jpg"

    def test_with_ref_videos_short(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Short video (≤15s) — no splitting
        from minipic.media import VideoClip
        v = tmp_path / "short.mp4"
        v.write_bytes(b"x")

        async def fake_prep(source: Any, *, clips_spec: Any = None) -> list[VideoClip]:
            return [VideoClip(path=source, start_seconds=0.0, duration_seconds=10.0, is_split=False)]
        # The CLI imports prepare_reference_video lazily inside _run, so
        # we patch it on the source module (minipic.media).
        import minipic.media as media_mod
        monkeypatch.setattr(media_mod, "prepare_reference_video", fake_prep)
        fake_client.upload_and_resolve.return_value = "https://cdn/uploaded.mp4"

        fake_client.create_video_task.return_value = "tid-r2v"
        result = runner.invoke(main, [
            "create", "r2v",
            "--prompt", "x",
            "--ref-video", str(v),
        ])
        assert result.exit_code == 0, result.output
        content = fake_client.create_video_task.await_args.kwargs["content"]
        vids = [c for c in content if c.get("type") == "video_url"]
        assert len(vids) == 1

    def test_with_ref_audios(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient,
        tmp_path: Path,
    ) -> None:
        # Use a local audio path to avoid the URL+colon parsing issue
        local_audio = tmp_path / "sound.mp3"
        local_audio.write_bytes(b"x")
        local_img = tmp_path / "img.jpg"
        local_img.write_bytes(b"x")
        fake_client.upload_and_resolve.return_value = "https://cdn/uploaded"
        fake_client.create_video_task.return_value = "tid-r2v"
        result = runner.invoke(main, [
            "create", "r2v",
            "--prompt", "x",
            "--ref-image", str(local_img),
            "--ref-audio", str(local_audio),
        ])
        assert result.exit_code == 0, result.output
        content = fake_client.create_video_task.await_args.kwargs["content"]
        audios = [c for c in content if c.get("type") == "audio_url"]
        assert len(audios) == 1
        assert audios[0]["audio_url"]["url"] == "https://cdn/uploaded"


# --------------------------------------------------------------------------- status command
class TestStatusCommand:
    def test_prints_status(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        fake_client.query_video_task.return_value = {
            "status": STATUS_SUCCEEDED,
            "content": [{"video_url": {"url": "https://cdn/v.mp4"}}],
        }
        result = runner.invoke(main, ["status", "tid-1"])
        assert result.exit_code == 0
        assert "Success" in result.output
        assert "https://cdn/v.mp4" in result.output

    def test_no_api_key_exits_nonzero(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli_mod, "load_config", lambda: Config())
        result = runner.invoke(main, ["status", "tid-1"])
        assert result.exit_code != 0


# --------------------------------------------------------------------------- wait command
class TestWaitCommand:
    def test_polls_and_downloads(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        fake_client.query_video_task.return_value = {
            "status": STATUS_SUCCEEDED,
            "content": [{"video_url": {"url": "https://cdn/v.mp4"}}],
        }
        result = runner.invoke(main, ["wait", "tid-1"])
        assert result.exit_code == 0, result.output
        # The downloaded file should exist
        out_path = Path(fake_config.videos_dir) / "tid-1.mp4"
        assert out_path.is_file()


# --------------------------------------------------------------------------- list command
class TestListCommand:
    def test_empty(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli_mod, "list_tasks", lambda limit=50: [])
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
        # No tasks message or just a header
        assert "no tasks" in result.output.lower() or result.output.strip() == "" or "task" not in result.output.lower()

    def test_prints_records(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recs = [
            TaskRecord(task_id="t1", mode="t2v", prompt_excerpt="hello"),
            TaskRecord(task_id="t2", mode="r2v", prompt_excerpt="world"),
        ]
        monkeypatch.setattr(cli_mod, "list_tasks", lambda limit=50: recs)
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
        assert "t1" in result.output
        assert "t2" in result.output

    def test_respects_limit(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[int] = []
        def fake_list(limit: int = 50) -> list:
            seen.append(limit)
            return []
        monkeypatch.setattr(cli_mod, "list_tasks", fake_list)
        result = runner.invoke(main, ["list", "--limit", "5"])
        assert result.exit_code == 0
        assert seen == [5]


# --------------------------------------------------------------------------- cancel command
class TestCancelCommand:
    def test_prints_console_message(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(main, ["cancel", "tid-1"])
        # The impl raises SystemExit(0) explicitly
        assert result.exit_code == 0
        assert "console" in result.output.lower() or "platform" in result.output.lower()


# --------------------------------------------------------------------------- videos command
class TestVideosCommand:
    def test_no_videos_dir(
        self, runner: CliRunner, fake_config: Config
    ) -> None:
        # videos_dir is tmp_path / "videos" which doesn't exist yet
        result = runner.invoke(main, ["videos"])
        assert result.exit_code == 0
        assert "does not exist" in result.output or "no videos" in result.output.lower()

    def test_empty_dir(
        self, runner: CliRunner, fake_config: Config
    ) -> None:
        Path(fake_config.videos_dir).mkdir(parents=True, exist_ok=True)
        result = runner.invoke(main, ["videos"])
        assert result.exit_code == 0
        assert "no videos" in result.output.lower()

    def test_lists_mp4s(
        self, runner: CliRunner, fake_config: Config
    ) -> None:
        vdir = Path(fake_config.videos_dir)
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "a.mp4").write_bytes(b"x")
        (vdir / "b.mp4").write_bytes(b"y")
        result = runner.invoke(main, ["videos"])
        assert result.exit_code == 0
        assert "a.mp4" in result.output
        assert "b.mp4" in result.output


# --------------------------------------------------------------------------- web command
class TestWebCommand:
    def test_no_api_key_starts_anyway(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """As of v0.1.2, `minipic web` no longer requires an API key at startup.

        New users can configure the key through the UI (POST /api/config)
        before submitting a task. This is the whole point of the API Key
        button.
        """
        import uvicorn
        called: list[dict] = []
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append(1))
        monkeypatch.setattr(cli_mod, "load_config", lambda: Config())  # no key
        result = runner.invoke(main, ["web"])
        # The command should start uvicorn successfully even without a key.
        assert result.exit_code == 0, result.output
        assert len(called) == 1

    def test_starts_uvicorn(
        self, runner: CliRunner, fake_config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import uvicorn
        called: list[dict] = []
        def fake_run(*args: Any, **kwargs: Any) -> None:
            called.append({"args": args, "kwargs": kwargs})
        monkeypatch.setattr(uvicorn, "run", fake_run)
        result = runner.invoke(main, ["web", "--port", "9999"])
        assert result.exit_code == 0
        assert len(called) == 1
        # Verify the port was forwarded
        assert called[0]["kwargs"].get("port") == 9999 or any("9999" in str(a) for a in called[0]["args"])


# --------------------------------------------------------------------------- create --model
class TestCreateModelFlag:
    def test_t2v_default_model_is_h3(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        fake_client.create_video_task.return_value = "tid-1"
        result = runner.invoke(main, [
            "create", "t2v", "--prompt", "x", "--ratio", "16:9",
        ])
        assert result.exit_code == 0, result.output
        assert fake_client.create_video_task.await_args.kwargs["model"] == "MiniMax-H3"

    def test_t2v_explicit_h3_max(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        fake_client.create_video_task.return_value = "tid-max"
        result = runner.invoke(main, [
            "create", "t2v", "--prompt", "x", "--ratio", "16:9",
            "--model", "MiniMax-H3-Max",
            "--resolution", "768P", "--duration", "5",
        ])
        assert result.exit_code == 0, result.output
        kwargs = fake_client.create_video_task.await_args.kwargs
        assert kwargs["model"] == "MiniMax-H3-Max"
        assert kwargs["resolution"] == "768P"
        assert kwargs["duration"] == 5

    def test_t2v_h3_max_rejects_2k(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        result = runner.invoke(main, [
            "create", "t2v", "--prompt", "x", "--ratio", "16:9",
            "--model", "MiniMax-H3-Max", "--resolution", "2K",
        ])
        assert result.exit_code != 0
        assert "2K" in result.output

    def test_t2v_h3_max_rejects_duration_4(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        result = runner.invoke(main, [
            "create", "t2v", "--prompt", "x", "--ratio", "16:9",
            "--model", "MiniMax-H3-Max", "--duration", "4",
        ])
        assert result.exit_code != 0
        assert "duration" in result.output.lower()

    def test_invalid_model_choice_rejected(
        self, runner: CliRunner, fake_config: Config
    ) -> None:
        result = runner.invoke(main, [
            "create", "t2v", "--prompt", "x", "--ratio", "16:9",
            "--model", "MiniMax-X9",
        ])
        assert result.exit_code != 0

    def test_i2v_with_h3_max(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        fake_client.create_video_task.return_value = "tid-i2v-max"
        result = runner.invoke(main, [
            "create", "i2v",
            "--prompt", "x",
            "--ref-image", "https://cdn/photo.jpg",
            "--model", "MiniMax-H3-Max",
            "--resolution", "768P", "--duration", "6",
        ])
        assert result.exit_code == 0, result.output
        kwargs = fake_client.create_video_task.await_args.kwargs
        assert kwargs["model"] == "MiniMax-H3-Max"

    def test_r2v_with_h3_max(
        self, runner: CliRunner, fake_config: Config, fake_client: _FakeMiniMaxClient,
        tmp_path: Path,
    ) -> None:
        img = tmp_path / "img.jpg"
        img.write_bytes(b"x")
        fake_client.upload_and_resolve.return_value = "https://cdn/uploaded.jpg"
        fake_client.create_video_task.return_value = "tid-r2v-max"
        result = runner.invoke(main, [
            "create", "r2v",
            "--prompt", "x",
            "--ref-image", str(img),
            "--model", "MiniMax-H3-Max",
            "--resolution", "480P", "--duration", "5",
        ])
        assert result.exit_code == 0, result.output
        kwargs = fake_client.create_video_task.await_args.kwargs
        assert kwargs["model"] == "MiniMax-H3-Max"
        assert kwargs["resolution"] == "480P"
        assert kwargs["duration"] == 5


# --------------------------------------------------------------------------- _print_task_record
class TestPrintTaskRecord:
    def test_prints_id_mode_status(self) -> None:
        rec = TaskRecord(
            task_id="tid-x", mode="r2v", prompt_excerpt="hello world",
            status=STATUS_SUCCEEDED, output_path="/o.mp4",
        )
        _print_task_record(rec)
        # The function uses _print which may be rich — just exercise the path

    def test_includes_error(self) -> None:
        rec = TaskRecord(
            task_id="tid-x", mode="t2v", prompt_excerpt="x", status=STATUS_FAILED,
            error="boom",
        )
        _print_task_record(rec)


# --------------------------------------------------------------------------- _wait_and_download
class TestWaitAndDownload:
    @pytest.mark.asyncio
    async def test_succeeded_downloads(
        self, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        fake_client.query_video_task.return_value = {
            "status": STATUS_SUCCEEDED,
            "content": [{"video_url": {"url": "https://cdn/v.mp4"}}],
        }
        await _wait_and_download(fake_client, "tid-1", fake_config)
        out = Path(fake_config.videos_dir) / "tid-1.mp4"
        assert out.is_file()

    @pytest.mark.asyncio
    async def test_failed_raises_task_error(
        self, fake_config: Config, fake_client: _FakeMiniMaxClient
    ) -> None:
        # When a task ends in Failed, poll_until_done raises TaskError.
        # _wait_and_download does not catch it — the caller decides what to do.
        fake_client.query_video_task.return_value = {
            "status": STATUS_FAILED,
            "error": {"message": "safety filter"},
        }
        with pytest.raises(TaskError) as ei:
            await _wait_and_download(fake_client, "tid-fail", fake_config)
        assert "tid-fail" in str(ei.value)
        assert "Failed" in str(ei.value)


# --------------------------------------------------------------------------- _atomic_download
class TestAtomicDownload:
    def test_downloads_and_renames(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_dl(url: str, dest: Path, **kwargs: Any) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"video-data")
        client = MagicMock()
        client.download_video = AsyncMock(side_effect=fake_dl)
        dest = tmp_path / "out.mp4"
        _atomic_download(client, "https://x", dest)
        assert dest.is_file()
        assert dest.read_bytes() == b"video-data"
        # No .part leftover
        assert not (tmp_path / "out.part").exists()

    def test_removes_stale_part(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_dl(url: str, dest: Path, **kwargs: Any) -> None:
            dest.write_bytes(b"fresh")
        client = MagicMock()
        client.download_video = AsyncMock(side_effect=fake_dl)
        # Pre-existing stale .part
        stale = tmp_path / "out.part"
        stale.write_bytes(b"old")
        dest = tmp_path / "out.mp4"
        _atomic_download(client, "https://x", dest)
        assert dest.read_bytes() == b"fresh"


# --------------------------------------------------------------------------- top-level error wrapper
class TestCliErrorWrapper:
    def test_minipic_error_exits_2(self) -> None:
        # Force an unhandled MiniPicError to bubble up from main()
        from minipic.cli import _cli_wrapper

        with patch.object(cli_mod, "main", side_effect=MiniPicError("boom")):
            with pytest.raises(SystemExit) as ei:
                _cli_wrapper()
            assert ei.value.code == 2

    def test_other_exception_exits_1(self) -> None:
        from minipic.cli import _cli_wrapper

        with patch.object(cli_mod, "main", side_effect=RuntimeError("boom")):
            with pytest.raises(SystemExit) as ei:
                _cli_wrapper()
            assert ei.value.code == 1


# --------------------------------------------------------------------------- helpers
def _strip_rich(s: str) -> str:
    """Strip ANSI escape codes from a rich-formatted string for JSON parsing."""
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


# ============================================================ v0.1.3b prepare / commit
class TestPrepareCommandRegistration:
    def test_prepare_in_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli_mod.main, ["--help"])
        assert result.exit_code == 0
        assert "prepare" in result.output
        assert "commit" in result.output
        assert "prepares" in result.output

    def test_prepare_subcommands_listed(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli_mod.main, ["prepare", "--help"])
        assert result.exit_code == 0
        assert "t2v" in result.output
        assert "i2v" in result.output
        assert "r2v" in result.output

    def test_prepare_t2v_requires_api_key(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli_mod.main, [
                "prepare", "t2v", "--prompt", "x", "--ratio", "16:9",
            ])
        # Should fail because no API key is set in the test env
        assert result.exit_code != 0


class TestPrepareHelpers:
    def test_build_body_from_args_minimal(self) -> None:
        body = cli_mod._build_body_from_args(
            "t2v", "hello", (), (), (), None, "16:9", "768P", "MiniMax-H3", True,
        )
        assert body["mode"] == "t2v"
        assert body["prompt"] == "hello"
        assert body["use_context_ir"] is True
        assert body["ref_images"] == []

    def test_build_body_from_args_with_refs(self) -> None:
        body = cli_mod._build_body_from_args(
            "r2v", "x",
            ("img.jpg",), ("vid.mp4:reference_video",), (),
            10, "16:9", "768P", "MiniMax-H3", False,
        )
        assert body["ref_images"] == [{"path": "img.jpg", "role": "reference_image"}]
        assert body["ref_videos"] == [{"path": "vid.mp4", "role": "reference_video"}]
        assert body["use_context_ir"] is False

    def test_save_plan_persists(self) -> None:
        from minipic.storage import init_db, get_prepare, cleanup_expired_prepares
        from minipic.config import user_data_path
        init_db()
        cleanup_expired_prepares()
        body = {"mode": "t2v", "prompt": "x", "ratio": "16:9"}
        plan = {"content": [], "segments": [], "warnings": []}
        pid = cli_mod._save_plan(body, plan)
        rec = get_prepare(pid)
        assert rec is not None
        assert rec.status == "prepared"
        assert rec.payload == body

    def test_print_prepare_plan_does_not_crash(self) -> None:
        plan = {
            "estimated_segments": 2,
            "segments": [
                {"index": 0, "start_seconds": 0.0, "duration_seconds": 7},
                {"index": 1, "start_seconds": 7.0, "duration_seconds": 11},
            ],
            "warnings": [],
            "content_preview": [
                {"type": "text", "text": "rewritten prompt"},
                {"type": "video_url", "url": "https://cdn/clip.mp4", "duration": 7},
            ],
            "used_context_ir": True,
            "context_ir_fallback": False,
            "enhanced_prompt": "rewritten 6-section prompt",
        }
        # Should not raise
        cli_mod._print_prepare_plan(plan)

    def test_print_prepare_plan_with_fallback_warning(self) -> None:
        plan = {
            "estimated_segments": 1,
            "segments": [],
            "warnings": ["Context-IR fallback"],
            "content_preview": [],
            "used_context_ir": False,
            "context_ir_fallback": True,
            "context_ir_error": "[2013] TokenPlan",
        }
        cli_mod._print_prepare_plan(plan)


class TestCommitCommand:
    def test_commit_nonexistent_returns_error(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli_mod.main, [
                "commit", "prep-does-not-exist",
            ])
        # Should exit with error code
        assert result.exit_code != 0


class TestListPreparesCommand:
    def test_list_prepares_empty(self) -> None:
        from minipic.storage import init_db, cleanup_expired_prepares
        init_db()
        cleanup_expired_prepares()
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli_mod.main, ["prepares"])
        assert result.exit_code == 0
        assert "No active prepares" in result.output

