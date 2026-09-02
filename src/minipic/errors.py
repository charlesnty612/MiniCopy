"""Error code → exception mapping for the MiniMax public API.

Source: https://platform.minimaxi.com/docs/api-reference (verified 2026-09-01).
Retry policy:
  - Retry: 1000 (server), 1001 (timeout), 1002 (rate limit)
  - Do NOT retry: 1004 / 2049 (auth), 1008 (balance), 1026 (safety),
                   1039 (token limit), 2013 (invalid params), 2056 (Token Plan window)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Terminal error codes (do NOT retry)
TERMINAL_CODES = frozenset({1004, 1008, 1026, 1039, 2013, 2049, 2056})
# Retryable codes
RETRYABLE_CODES = frozenset({1000, 1001, 1002})


class MiniPicError(Exception):
    """Base exception for minipic."""


class ConfigError(MiniPicError):
    """Configuration is missing or invalid (e.g. no API key)."""


class AuthError(MiniPicError):
    """API key invalid or missing permission."""


class BalanceError(MiniPicError):
    """Insufficient account balance or Token Plan window exceeded."""


class SafetyError(MiniPicError):
    """Prompt or media rejected by safety filter (code 1026)."""


class InvalidParamsError(MiniPicError):
    """Request parameters are invalid (code 2013)."""


class RateLimitError(MiniPicError):
    """Rate limit hit (code 1002). Retryable with backoff."""


class ServerError(MiniPicError):
    """Upstream internal error (code 1000). Retryable with backoff."""


class UploadError(MiniPicError):
    """File upload to MiniMax file API failed."""


class MediaError(MiniPicError):
    """Local media file is invalid (bad codec, unreadable, etc.)."""


class TaskError(MiniPicError):
    """Provider task ended in a terminal failure (failed / cancelled / expired)."""


@dataclass
class ApiErrorPayload:
    code: int
    message: str
    request_id: Optional[str] = None
    http_code: Optional[int] = None


_CODE_TO_EXC = {
    1004: AuthError,
    1008: BalanceError,
    1026: SafetyError,
    1039: InvalidParamsError,  # token-limit, treated as a parameter error
    2013: InvalidParamsError,
    2049: AuthError,
    2056: BalanceError,
}


def raise_for_code(payload: ApiErrorPayload) -> None:
    """Map an API error payload to the matching exception class and raise it."""
    if payload.code in _CODE_TO_EXC:
        cls = _CODE_TO_EXC[payload.code]
        msg = f"[{payload.code}] {payload.message}"
        if payload.http_code is not None:
            msg += f" (http={payload.http_code})"
        if payload.request_id:
            msg += f" (request_id={payload.request_id})"
        raise cls(msg)

    # Unknown code — fall through to generic, still surface the HTTP status if any.
    msg = f"[{payload.code}] {payload.message}"
    if payload.http_code is not None:
        msg += f" (http={payload.http_code})"
    raise MiniPicError(msg)
