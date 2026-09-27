"""Tests for minipic.errors — error code → exception mapping."""

from __future__ import annotations

import pytest

from minipic.errors import (
    ApiErrorPayload,
    AuthError,
    BalanceError,
    InvalidParamsError,
    MiniPicError,
    RETRYABLE_CODES,
    RateLimitError,
    SafetyError,
    ServerError,
    TERMINAL_CODES,
    raise_for_code,
)


# --------------------------------------------------------------------------- module-level invariants
class TestModuleInvariants:
    def test_terminal_codes_frozenset(self) -> None:
        assert isinstance(TERMINAL_CODES, frozenset)
        # Codes that should NEVER be retried.
        assert {1004, 1008, 1026, 1039, 2013, 2049, 2056} <= TERMINAL_CODES

    def test_retryable_codes_frozenset(self) -> None:
        assert isinstance(RETRYABLE_CODES, frozenset)
        # Retryable network / transient errors.
        assert {1000, 1001, 1002} <= RETRYABLE_CODES

    def test_terminal_and_retryable_disjoint(self) -> None:
        assert TERMINAL_CODES & RETRYABLE_CODES == set()


# --------------------------------------------------------------------------- payload construction
class TestApiErrorPayload:
    def test_defaults(self) -> None:
        p = ApiErrorPayload(code=2013, message="bad params")
        assert p.code == 2013
        assert p.message == "bad params"
        assert p.request_id is None
        assert p.http_code is None

    def test_with_optionals(self) -> None:
        p = ApiErrorPayload(code=1004, message="auth", request_id="req-1", http_code=401)
        assert p.request_id == "req-1"
        assert p.http_code == 401


# --------------------------------------------------------------------------- raise_for_code: terminal codes
class TestRaiseForCodeTerminal:
    @pytest.mark.parametrize(
        "code,expected_cls",
        [
            (1004, AuthError),
            (2049, AuthError),
            (1008, BalanceError),
            (2056, BalanceError),
            (1026, SafetyError),
            (1039, InvalidParamsError),
            (2013, InvalidParamsError),
        ],
    )
    def test_terminal_codes_map_to_correct_subclass(
        self, code: int, expected_cls: type[MiniPicError]
    ) -> None:
        payload = ApiErrorPayload(code=code, message=f"err {code}")
        with pytest.raises(expected_cls) as ei:
            raise_for_code(payload)
        # Every terminal exception should also be a MiniPicError (catchable via base).
        assert isinstance(ei.value, MiniPicError)

    def test_message_includes_code(self) -> None:
        payload = ApiErrorPayload(code=1004, message="bad key")
        with pytest.raises(AuthError) as ei:
            raise_for_code(payload)
        assert "[1004]" in str(ei.value)
        assert "bad key" in str(ei.value)

    def test_message_includes_request_id_when_provided(self) -> None:
        payload = ApiErrorPayload(code=2013, message="bad", request_id="req-xyz")
        with pytest.raises(InvalidParamsError) as ei:
            raise_for_code(payload)
        assert "request_id=req-xyz" in str(ei.value)

    def test_message_omits_request_id_when_none(self) -> None:
        payload = ApiErrorPayload(code=1026, message="unsafe")
        with pytest.raises(SafetyError) as ei:
            raise_for_code(payload)
        assert "request_id" not in str(ei.value)

    def test_balance_error_message_distinct_from_auth(self) -> None:
        # 1008 (balance) and 1004 (auth) should not share a class.
        with pytest.raises(BalanceError):
            raise_for_code(ApiErrorPayload(code=1008, message="no money"))
        with pytest.raises(AuthError):
            raise_for_code(ApiErrorPayload(code=1004, message="no auth"))


# ----------------------------------------------------------------- raise_for_code: retryable
class TestRaiseForCodeMapping:
    def test_unknown_code_raises_base(self) -> None:
        payload = ApiErrorPayload(code=99999, message="weird")
        with pytest.raises(MiniPicError) as ei:
            raise_for_code(payload)
        # Should NOT be a more specific subclass
        assert type(ei.value) is MiniPicError

    @pytest.mark.parametrize(
        "code,expected_cls",
        [
            (1000, ServerError),     # upstream internal error
            (1001, ServerError),     # upstream timeout (no dedicated class —
                                     # exhaustion of RETRYABLE_CODES retries
                                     # surfaces as ServerError, consistent with 1000)
            (1002, RateLimitError),  # rate limit (retryable)
        ],
    )
    def test_retryable_codes_map_to_dedicated_subclass(
        self, code: int, expected_cls: type[MiniPicError]
    ) -> None:
        """v0.2.6+: retryable codes (1000/1001/1002) now map to dedicated
        subclasses so the caller can ``except RateLimitError`` instead of
        only ``except MiniPicError``. The retry loop in client.py still
        owns the retry-then-give-up decision; raise_for_code only fires
        after that budget is exhausted.
        """
        payload = ApiErrorPayload(code=code, message=f"err {code}")
        with pytest.raises(expected_cls) as ei:
            raise_for_code(payload)
        # Every subclass must still be catchable as the base MiniPicError.
        assert isinstance(ei.value, MiniPicError)

    def test_rate_limit_error_includes_code_in_message(self) -> None:
        """The mapped message format must surface the extracted code so the
        caller can grep it (mirrors the SafetyError / AuthError tests above)."""
        payload = ApiErrorPayload(code=1002, message="rate limit, please retry later")
        with pytest.raises(RateLimitError) as ei:
            raise_for_code(payload)
        assert "[1002]" in str(ei.value)
        assert "rate limit, please retry later" in str(ei.value)

    def test_server_error_for_1000_and_1001_have_distinct_messages(self) -> None:
        """Both 1000 and 1001 funnel into ServerError; the message must still
        carry the code so the two root causes don't collapse indistinguishably.
        """
        with pytest.raises(ServerError) as ei1000:
            raise_for_code(ApiErrorPayload(code=1000, message="internal"))
        with pytest.raises(ServerError) as ei1001:
            raise_for_code(ApiErrorPayload(code=1001, message="timeout"))
        assert "[1000]" in str(ei1000.value)
        assert "[1001]" in str(ei1001.value)
        # Different codes → different strings.
        assert str(ei1000.value) != str(ei1001.value)

    def test_rate_limit_error_is_subclass_of_minipic(self) -> None:
        """Regression guard: RateLimitError must remain a MiniPicError so
        existing ``except MiniPicError`` catch sites keep working."""
        assert issubclass(RateLimitError, MiniPicError)

    def test_zero_code_raises_base(self) -> None:
        """0 means 'unknown code', must still raise something."""
        with pytest.raises(MiniPicError):
            raise_for_code(ApiErrorPayload(code=0, message="no code"))

    def test_http_code_does_not_affect_class_mapping(self) -> None:
        """http_code is for context only — class mapping is driven by `code`."""
        # 200 + bad code shouldn't suddenly return success
        with pytest.raises(InvalidParamsError):
            raise_for_code(
                ApiErrorPayload(code=2013, message="x", http_code=200)
            )
        # 500 + auth code should still map to AuthError
        with pytest.raises(AuthError):
            raise_for_code(
                ApiErrorPayload(code=1004, message="x", http_code=500)
            )


# --------------------------------------------------------------------------- exception hierarchy
class TestHierarchy:
    def test_all_specific_errors_subclass_minipic(self) -> None:
        for cls in (AuthError, BalanceError, SafetyError, InvalidParamsError,
                    ServerError, RateLimitError):
            assert issubclass(cls, MiniPicError)

    def test_specific_subclasses_can_be_caught_as_base(self) -> None:
        try:
            raise SafetyError("oops")
        except MiniPicError as e:
            assert e is not None

    def test_subclasses_not_unnecessarily_nested(self) -> None:
        # Sanity: each terminal subclass is direct child, not a chain.
        assert AuthError.__bases__ == (MiniPicError,)
        assert BalanceError.__bases__ == (MiniPicError,)
        assert InvalidParamsError.__bases__ == (MiniPicError,)
        assert SafetyError.__bases__ == (MiniPicError,)
