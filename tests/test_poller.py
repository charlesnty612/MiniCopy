"""Tests for minipic.poller — async task polling, terminal states, timeout."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from minipic.client import MiniMaxClient
from minipic.config import Config
from minipic.errors import TaskError
from minipic.poller import (
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TERMINAL_STATES,
    poll_until_done,
)


# --------------------------------------------------------------------------- constants
class TestPollerConstants:
    def test_terminal_states_are_exhaustive(self) -> None:
        assert STATUS_SUCCEEDED in TERMINAL_STATES
        assert STATUS_FAILED in TERMINAL_STATES
        assert STATUS_CANCELLED in TERMINAL_STATES
        assert STATUS_EXPIRED in TERMINAL_STATES
        assert STATUS_PENDING not in TERMINAL_STATES
        assert STATUS_QUEUED not in TERMINAL_STATES
        assert STATUS_RUNNING not in TERMINAL_STATES

    def test_terminal_states_is_a_set(self) -> None:
        assert isinstance(TERMINAL_STATES, (set, frozenset))


# --------------------------------------------------------------------------- poll_until_done — success
class TestPollSuccess:
    @pytest.mark.asyncio
    async def test_returns_task_on_success(self) -> None:
        done_task = {"status": STATUS_SUCCEEDED, "content": {"url": "https://x/v.mp4"}}
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(return_value=done_task)

        result = await poll_until_done(mock_client, "tid-1", interval_seconds=0)
        assert result == done_task
        mock_client.query_video_task.assert_called_once_with("tid-1")

    @pytest.mark.asyncio
    async def test_returns_task_after_several_polls(self) -> None:
        poll_seq = [
            {"status": STATUS_PENDING},
            {"status": STATUS_QUEUED},
            {"status": STATUS_RUNNING},
            {"status": STATUS_SUCCEEDED, "content": {"url": "https://x/v.mp4"}},
        ]
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(side_effect=poll_seq)

        result = await poll_until_done(mock_client, "tid-2", interval_seconds=0)
        assert result["status"] == STATUS_SUCCEEDED
        assert mock_client.query_video_task.call_count == 4


# --------------------------------------------------------------------------- poll_until_done — terminal failure
class TestPollFailure:
    @pytest.mark.asyncio
    async def test_raises_task_error_on_failed(self) -> None:
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(
            return_value={"status": STATUS_FAILED, "error": "processing error"}
        )
        with pytest.raises(TaskError) as ei:
            await poll_until_done(mock_client, "tid-fail", interval_seconds=0)
        assert "tid-fail" in str(ei.value)
        assert "Failed" in str(ei.value)

    @pytest.mark.asyncio
    async def test_raises_task_error_on_cancelled(self) -> None:
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(return_value={"status": STATUS_CANCELLED})
        with pytest.raises(TaskError) as ei:
            await poll_until_done(mock_client, "tid-cancel", interval_seconds=0)
        assert "Cancelled" in str(ei.value)

    @pytest.mark.asyncio
    async def test_raises_task_error_on_expired(self) -> None:
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(return_value={"status": STATUS_EXPIRED})
        with pytest.raises(TaskError) as ei:
            await poll_until_done(mock_client, "tid-exp", interval_seconds=0)
        assert "Expired" in str(ei.value)

    @pytest.mark.asyncio
    async def test_error_message_includes_base_resp(self) -> None:
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(
            return_value={"status": STATUS_FAILED, "base_resp": {"error_message": "safety filter"}}
        )
        with pytest.raises(TaskError) as ei:
            await poll_until_done(mock_client, "tid-err", interval_seconds=0)
        assert "safety filter" in str(ei.value)


# --------------------------------------------------------------------------- poll_until_done — timeout
class TestPollTimeout:
    @pytest.mark.asyncio
    async def test_respects_max_wait_seconds(self) -> None:
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(return_value={"status": STATUS_RUNNING})

        with pytest.raises(TaskError, match="timed out"):
            await poll_until_done(mock_client, "tid-timeout", interval_seconds=0.001, max_wait_seconds=0.01)

    @pytest.mark.asyncio
    async def test_timeout_mentions_task_id(self) -> None:
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(return_value={"status": STATUS_RUNNING})

        with pytest.raises(TaskError) as ei:
            await poll_until_done(mock_client, "tid-my", interval_seconds=0.001, max_wait_seconds=0.01)
        assert "tid-my" in str(ei.value)


# --------------------------------------------------------------------------- poll_until_done — on_progress callback
class TestPollOnProgress:
    @pytest.mark.asyncio
    async def test_calls_callback_on_each_new_status(self) -> None:
        poll_seq = [
            {"status": STATUS_PENDING},
            {"status": STATUS_QUEUED},
            {"status": STATUS_RUNNING},
            {"status": STATUS_SUCCEEDED, "content": {"url": "https://x"}},
        ]
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(side_effect=poll_seq)

        seen: list[str] = []

        async def cb(status: str) -> None:
            seen.append(status)

        await poll_until_done(mock_client, "tid-cb", interval_seconds=0, on_progress=cb)
        assert STATUS_PENDING in seen
        assert STATUS_QUEUED in seen
        assert STATUS_RUNNING in seen
        assert STATUS_SUCCEEDED in seen

    @pytest.mark.asyncio
    async def test_calls_callback_only_on_status_change(self) -> None:
        # Same status twice should only fire callback once
        poll_seq = [
            {"status": STATUS_RUNNING},
            {"status": STATUS_RUNNING},
            {"status": STATUS_RUNNING},
            {"status": STATUS_SUCCEEDED, "content": {"url": "https://x"}},
        ]
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(side_effect=poll_seq)

        seen: list[str] = []

        async def cb(status: str) -> None:
            seen.append(status)

        await poll_until_done(mock_client, "tid-cb2", interval_seconds=0, on_progress=cb)
        # Should be called only on transitions (initial + success)
        # status change: pending->running (1), running->success (2)
        # but we started from first poll so:
        assert seen.count(STATUS_RUNNING) == 1

    @pytest.mark.asyncio
    async def test_sync_callback_is_accepted(self) -> None:
        poll_seq = [
            {"status": STATUS_SUCCEEDED, "content": {"url": "https://x"}},
        ]
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(side_effect=poll_seq)

        seen: list[str] = []

        def cb(status: str) -> None:  # sync, not async
            seen.append(status)

        await poll_until_done(mock_client, "tid-sync", interval_seconds=0, on_progress=cb)
        assert STATUS_SUCCEEDED in seen

    @pytest.mark.asyncio
    async def test_none_callback_is_accepted(self) -> None:
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(
            return_value={"status": STATUS_SUCCEEDED, "content": {"url": "https://x"}}
        )
        # Must not raise
        await poll_until_done(mock_client, "tid-none", interval_seconds=0, on_progress=None)


# --------------------------------------------------------------------------- poll_until_done — interval behavior
class TestPollInterval:
    @pytest.mark.asyncio
    async def test_polls_at_correct_interval(self) -> None:
        poll_seq = [
            {"status": STATUS_PENDING},
            {"status": STATUS_PENDING},
            {"status": STATUS_SUCCEEDED, "content": {"url": "https://x"}},
        ]
        mock_client = AsyncMock()
        mock_client.query_video_task = AsyncMock(side_effect=poll_seq)

        import asyncio

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await poll_until_done(mock_client, "tid-int", interval_seconds=5.0, max_wait_seconds=999)
            # Should have slept twice (after each pending poll before success)
            assert mock_sleep.call_count >= 1
            # Check the interval value was passed
            args = [c.args[0] for c in mock_sleep.call_args_list]
            assert 5.0 in args
