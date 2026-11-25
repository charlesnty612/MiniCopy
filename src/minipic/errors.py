"""Error code → exception mapping for the MiniMax H3 V2 API.

Source: https://platform.minimaxi.com/docs/api-reference (verified 2026-09-27).
Vectron (api.vectron.meta-stone.com) serves the same V2 protocol but reports
errors by bare HTTP status (400 / 401 / 402 / 422 / 429 / 500) instead of the
numeric codes below, so ``raise_for_http_status`` covers it — see
``HTTP_STATUS_TO_EXC``.

Retry policy:
  - Retry: 1000 (server), 1001 (timeout), 1002 (rate limit), HTTP 429/500
  - Do NOT retry: 1004 / 2049 (auth), 1008 (balance), 1026 (safety),
                   1039 (token limit), 2013 (invalid params), 2056 (Token Plan window)

Exception class assignment (via `_CODE_TO_EXC`, fired after the retry budget
is exhausted in `client._request_with_retry`):
  - 1000 → ServerError     (upstream internal error)
  - 1001 → ServerError     (upstream timeout — no dedicated class; reuses 1000)
  - 1002 → RateLimitError  (rate limit; callers can ``except RateLimitError``)
  - 1004 / 2049 → AuthError
  - 1008 / 2056 → BalanceError
  - 1026 → SafetyError
  - 1039 / 2013 → InvalidParamsError
  - Any other code → generic MiniPicError (still carries ``[code] message``).
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
    1000: ServerError,   # upstream internal error (retryable)
    1001: ServerError,   # upstream timeout — no dedicated TimeoutError class;
                       # the retry loop in client.py already retries on
                       # RETRYABLE_CODES, so after exhaustion the caller
                       # gets ServerError, which is consistent with 1000.
    1002: RateLimitError,  # rate limit (retryable)
    1004: AuthError,
    1008: BalanceError,
    1026: SafetyError,
    1039: InvalidParamsError,  # token-limit, treated as a parameter error
    2013: InvalidParamsError,
    2049: AuthError,
    2056: BalanceError,
}

# Bare HTTP status → exception, used when the payload has no numeric code.
# Matches the status-only error table in the Vectron H3 API reference:
#   400 参数错误 · 401 API Key 无效 · 402 余额/额度不足 ·
#   422 输入涉及敏感内容 · 429 触发限流 · 500 服务端错误
HTTP_STATUS_TO_EXC = {
    400: InvalidParamsError,
    401: AuthError,
    402: BalanceError,
    403: AuthError,
    422: SafetyError,
    429: RateLimitError,
    500: ServerError,
    502: ServerError,
    503: ServerError,
    504: ServerError,
}

# HTTP statuses worth retrying with backoff (Vectron has no numeric codes).
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})


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

    # No usable numeric code — fall back to the bare HTTP status. This is the
    # normal path for Vectron, which documents status-only errors (401 API Key
    # 无效, 402 余额不足, 422 敏感内容, 429 限流, 500 服务端错误) and does
    # not put a ``(NNNN)`` suffix in the message.
    if payload.http_code is not None:
        cls = HTTP_STATUS_TO_EXC.get(int(payload.http_code))
        if cls is not None:
            http_msg = f"[http {payload.http_code}] {payload.message}"
            if payload.request_id:
                http_msg += f" (request_id={payload.request_id})"
            raise cls(http_msg)

    # Unknown code — fall through to generic, still surface the HTTP status if any.
    msg = f"[{payload.code}] {payload.message}"
    if payload.http_code is not None:
        msg += f" (http={payload.http_code})"
    raise MiniPicError(msg)
