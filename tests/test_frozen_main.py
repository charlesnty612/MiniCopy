"""Tests for minipic.frozen_main — green-package auto-takeover of old MiniCopy.exe.

All ``subprocess.run`` and ``socket`` I/O are mocked; no real process / port
is ever touched. The tests cover:

- ``_port_owner_pid``: netstat output parsing (LISTENING match, no match,
  malformed input).
- ``_process_image_name``: tasklist CSV parsing (normal hit, empty output,
  "INFO:" header when PID is gone).
- ``_maybe_takeover_existing_instance`` + ``main``: takeover of an old
  MiniCopy.exe (calls taskkill, then proceeds), refusal on non-MiniCopy
  occupant (no taskkill, SystemExit(1)), graceful fallback when parsing
  fails (opens browser, no exit).
"""
from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from minipic import frozen_main


# --------------------------------------------------------------------------- helpers
NETSTAT_LISTENING_MINICOPY = (
    "Active Connections\n\n"
    "  Proto  Local Address          Foreign Address        State           PID\n"
    "  TCP    127.0.0.1:7860         0.0.0.0:0              LISTENING       40628\n"
    "  TCP    127.0.0.1:49672        127.0.0.1:40628        ESTABLISHED     1234\n"
)

NETSTAT_NO_LISTENER = (
    "Active Connections\n\n"
    "  Proto  Local Address          Foreign Address        State           PID\n"
    "  TCP    127.0.0.1:50000        0.0.0.0:0              LISTENING       7777\n"
)

TASKLIST_MINICOPY = (
    '"MiniCopy.exe","40628","Console","1","12,345 K"\r\n'
)
TASKLIST_CHROME = (
    '"chrome.exe","7777","Console","1","99,999 K"\r\n'
)
TASKLIST_INFO_EMPTY = "INFO: No tasks are running which match the specified criteria.\r\n"


def _fake_completed(stdout: str, returncode: int = 0) -> MagicMock:
    """Build a subprocess.run return-value mock."""
    cp = MagicMock()
    cp.stdout = stdout
    cp.stderr = ""
    cp.returncode = returncode
    return cp


# --------------------------------------------------------------------------- _port_owner_pid
class TestPortOwnerPid:
    def test_returns_pid_when_listening(self) -> None:
        with patch.object(
            frozen_main.subprocess, "run",
            return_value=_fake_completed(NETSTAT_LISTENING_MINICOPY),
        ) as run:
            assert frozen_main._port_owner_pid(7860) == 40628
        run.assert_called_once()

    def test_returns_none_when_no_listener(self) -> None:
        with patch.object(
            frozen_main.subprocess, "run",
            return_value=_fake_completed(NETSTAT_NO_LISTENER),
        ):
            assert frozen_main._port_owner_pid(7860) is None

    def test_returns_none_when_netstat_fails(self) -> None:
        with patch.object(
            frozen_main.subprocess, "run",
            return_value=_fake_completed("garbage", returncode=1),
        ):
            assert frozen_main._port_owner_pid(7860) is None

    def test_returns_none_when_subprocess_raises(self) -> None:
        with patch.object(
            frozen_main.subprocess, "run",
            side_effect=OSError("netstat missing"),
        ):
            assert frozen_main._port_owner_pid(7860) is None

    def test_skips_non_listening_state(self) -> None:
        """ESTABLISHED row for the port must not be confused with LISTENING."""
        sample = (
            "Active Connections\n\n"
            "  Proto  Local Address          Foreign Address        State           PID\n"
            "  TCP    127.0.0.1:7860         127.0.0.1:50000        ESTABLISHED     9999\n"
        )
        with patch.object(
            frozen_main.subprocess, "run",
            return_value=_fake_completed(sample),
        ):
            assert frozen_main._port_owner_pid(7860) is None

    def test_picks_listening_over_established_when_both_present(self) -> None:
        """When netstat lists both LISTENING and ESTABLISHED for the same port
        (an outgoing connection to a remote 7860), the LISTENING row wins."""
        sample = (
            "Active Connections\n\n"
            "  Proto  Local Address          Foreign Address        State           PID\n"
            "  TCP    192.168.1.2:7860      1.2.3.4:443            ESTABLISHED     9999\n"
            "  TCP    127.0.0.1:7860         0.0.0.0:0              LISTENING       40628\n"
        )
        with patch.object(
            frozen_main.subprocess, "run",
            return_value=_fake_completed(sample),
        ):
            assert frozen_main._port_owner_pid(7860) == 40628


# --------------------------------------------------------------------------- _process_image_name
class TestProcessImageName:
    def test_parses_minicopy(self) -> None:
        with patch.object(
            frozen_main.subprocess, "run",
            return_value=_fake_completed(TASKLIST_MINICOPY),
        ):
            assert frozen_main._process_image_name(40628) == "MiniCopy.exe"

    def test_parses_chrome(self) -> None:
        with patch.object(
            frozen_main.subprocess, "run",
            return_value=_fake_completed(TASKLIST_CHROME),
        ):
            assert frozen_main._process_image_name(7777) == "chrome.exe"

    def test_returns_empty_on_info_header(self) -> None:
        with patch.object(
            frozen_main.subprocess, "run",
            return_value=_fake_completed(TASKLIST_INFO_EMPTY),
        ):
            assert frozen_main._process_image_name(99999) == ""

    def test_returns_empty_when_subprocess_raises(self) -> None:
        with patch.object(
            frozen_main.subprocess, "run",
            side_effect=OSError("tasklist missing"),
        ):
            assert frozen_main._process_image_name(40628) == ""


# --------------------------------------------------------------------------- _wait_for_port_release
class TestWaitForPortRelease:
    def test_returns_true_when_port_free_immediately(self) -> None:
        with patch.object(frozen_main, "_port_in_use", return_value=False):
            assert frozen_main._wait_for_port_release(timeout=5.0) is True

    def test_returns_true_after_polling(self) -> None:
        # First two polls: still in use. Third: free.
        with patch.object(
            frozen_main, "_port_in_use",
            side_effect=[True, True, False],
        ), patch.object(frozen_main.time, "sleep") as sleep:
            assert frozen_main._wait_for_port_release(timeout=5.0) is True
        assert sleep.call_count == 2  # only slept while still busy

    def test_returns_false_on_timeout(self) -> None:
        # Always busy → timeout expires.
        with patch.object(
            frozen_main, "_port_in_use", return_value=True,
        ), patch.object(frozen_main.time, "sleep"), \
           patch.object(frozen_main.time, "monotonic",
                        side_effect=[0.0, 100.0]):
            assert frozen_main._wait_for_port_release(timeout=5.0) is False


# --------------------------------------------------------------------------- _maybe_takeover_existing_instance
class TestMaybeTakeoverExistingInstance:
    def test_returns_true_when_port_free(self) -> None:
        with patch.object(frozen_main, "_port_in_use", return_value=False):
            assert frozen_main._maybe_takeover_existing_instance() is True

    def test_takeover_calls_taskkill_for_minicopy(self) -> None:
        """Old MiniCopy.exe on 7860 → taskkill + wait_for_port_release,
        then normal startup path."""
        with patch.object(frozen_main, "_port_in_use", return_value=True), \
             patch.object(frozen_main, "_port_owner_pid", return_value=40628), \
             patch.object(
                 frozen_main, "_process_image_name", return_value="MiniCopy.exe",
             ), \
             patch.object(frozen_main, "subprocess") as sp_mod, \
             patch.object(frozen_main, "_wait_for_port_release", return_value=True), \
             patch.object(frozen_main, "_open_browser") as open_browser:
            sp_mod.run.return_value = _fake_completed("success")
            assert frozen_main._maybe_takeover_existing_instance() is True
            # taskkill was the *first* subprocess.run call (after netstat+tasklist
            # were already handled). The kill command must be present.
            kill_calls = [
                c for c in sp_mod.run.call_args_list
                if c.args and c.args[0][:1] == ["taskkill"]
            ]
            assert kill_calls, f"taskkill not called: {sp_mod.run.call_args_list}"
            cmd = kill_calls[0].args[0]
            assert cmd[:3] == ["taskkill", "/F", "/PID"]
            assert cmd[3] == "40628"
            # Port released → no fallback browser open from the takeover path.
            open_browser.assert_not_called()

    def test_non_minicopy_occupant_exits_with_sys_exit(self) -> None:
        """chrome.exe owns 7860 → no taskkill, SystemExit(1)."""
        with patch.object(frozen_main, "_port_in_use", return_value=True), \
             patch.object(frozen_main, "_port_owner_pid", return_value=7777), \
             patch.object(
                 frozen_main, "_process_image_name", return_value="chrome.exe",
             ), \
             patch.object(frozen_main, "subprocess") as sp_mod, \
             patch.object(frozen_main, "_open_browser") as open_browser:
            with pytest.raises(SystemExit) as ei:
                frozen_main._maybe_takeover_existing_instance()
            assert ei.value.code == 1
            # Must NOT kill someone else's process.
            kill_calls = [
                c for c in sp_mod.run.call_args_list
                if c.args and c.args[0][:1] == ["taskkill"]
            ]
            assert not kill_calls
            # And must NOT open the browser (would navigate to chrome's app).
            open_browser.assert_not_called()

    def test_pid_unavailable_falls_back_to_open_browser(self) -> None:
        """netstat fails (no PID) → keep legacy behavior: print + open browser."""
        with patch.object(frozen_main, "_port_in_use", return_value=True), \
             patch.object(frozen_main, "_port_owner_pid", return_value=None), \
             patch.object(
                 frozen_main, "_process_image_name", return_value="",
             ), \
             patch.object(frozen_main, "_open_browser") as open_browser:
            assert frozen_main._maybe_takeover_existing_instance() is True
            open_browser.assert_called_once()

    def test_kill_timeout_falls_back_to_open_browser(self) -> None:
        """taskkill ran but port didn't release in time → warn + open browser,
        don't exit. Old instance is dead-but-socket-stuck; user can refresh."""
        with patch.object(frozen_main, "_port_in_use", return_value=True), \
             patch.object(frozen_main, "_port_owner_pid", return_value=40628), \
             patch.object(
                 frozen_main, "_process_image_name", return_value="MiniCopy.exe",
             ), \
             patch.object(frozen_main, "subprocess") as sp_mod, \
             patch.object(
                 frozen_main, "_wait_for_port_release", return_value=False,
             ), \
             patch.object(frozen_main, "_open_browser") as open_browser:
            sp_mod.run.return_value = _fake_completed("success")
            assert frozen_main._maybe_takeover_existing_instance() is True
            open_browser.assert_called_once()


# --------------------------------------------------------------------------- main() happy paths
class TestMainEntry:
    def test_main_calls_uvicorn_when_port_free(self) -> None:
        """No old instance → uvicorn.run is called normally."""
        fake_uvicorn = MagicMock()
        with patch.object(frozen_main, "_port_in_use", return_value=False), \
             patch.object(frozen_main, "_open_browser"), \
             patch.dict(sys.modules, {"uvicorn": fake_uvicorn}):
            frozen_main.main()
        fake_uvicorn.run.assert_called_once()
        args, kwargs = fake_uvicorn.run.call_args
        # host/port passed through
        assert kwargs.get("host") == frozen_main.HOST
        assert kwargs.get("port") == frozen_main.PORT

    def test_main_after_takeover_calls_uvicorn(self) -> None:
        """Old MiniCopy.exe detected → killed → port released → uvicorn.run."""
        fake_uvicorn = MagicMock()
        # First _port_in_use call (inside _maybe_takeover): busy.
        # Second (inside main, after takeover): free.
        with patch.object(
            frozen_main, "_port_in_use", side_effect=[True, False],
        ), \
             patch.object(frozen_main, "_port_owner_pid", return_value=40628), \
             patch.object(
                 frozen_main, "_process_image_name", return_value="MiniCopy.exe",
             ), \
             patch.object(frozen_main, "subprocess") as sp_mod, \
             patch.object(frozen_main, "_wait_for_port_release", return_value=True), \
             patch.object(frozen_main, "_open_browser"), \
             patch.dict(sys.modules, {"uvicorn": fake_uvicorn}):
            sp_mod.run.return_value = _fake_completed("success")
            frozen_main.main()
        # uvicorn.run was invoked (took over and started fresh).
        fake_uvicorn.run.assert_called_once()

    def test_main_exits_when_non_minicopy_holds_port(self) -> None:
        """chrome.exe on 7860 → SystemExit(1) from takeover; main never
        starts uvicorn."""
        fake_uvicorn = MagicMock()
        with patch.object(frozen_main, "_port_in_use", return_value=True), \
             patch.object(frozen_main, "_port_owner_pid", return_value=7777), \
             patch.object(
                 frozen_main, "_process_image_name", return_value="chrome.exe",
             ), \
             patch.object(frozen_main, "subprocess"), \
             patch.object(frozen_main, "_open_browser"), \
             patch.dict(sys.modules, {"uvicorn": fake_uvicorn}):
            with pytest.raises(SystemExit) as ei:
                frozen_main.main()
            assert ei.value.code == 1
        fake_uvicorn.run.assert_not_called()