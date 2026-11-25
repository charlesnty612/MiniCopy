"""Tests for the provider abstraction (v0.2.8+).

Covers the Vectron (拓元) integration end to end at the unit level:
  - registry / capability descriptors and model-id translation
  - base-URL resolution (provider endpoint vs explicit override)
  - provider-scoped model & resolution constraints
  - upload path and response-shape differences
  - Context-IR / image-generation capability guards
  - lowercase task statuses and status-only HTTP error mapping
  - web surface: /api/config provider payload, POST /api/config switching,
    and /api/create skipping Context-IR on a provider that lacks it
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import minipic.web as web_mod
from minipic.client import MiniMaxClient
from minipic.config import (
    DEFAULT_BASE_URL,
    PROVIDERS,
    PROVIDER_MINIMAX,
    PROVIDER_VECTRON,
    Config,
    ConfigError,
    normalize_provider,
    provider_constraints,
    provider_models,
    provider_spec,
    resolve_model_id,
    validate_model_modes,
    validate_model_params,
)
from minipic.errors import (
    AuthError,
    BalanceError,
    InvalidParamsError,
    RateLimitError,
    SafetyError,
    ServerError,
)
from minipic.poller import is_failure, is_success


def _vec_cfg(**kw: Any) -> Config:
    base: dict[str, Any] = {
        "api_key": "vk-test",
        "provider": PROVIDER_VECTRON,
        "base_url": DEFAULT_BASE_URL,  # "unset" — provider endpoint should win
    }
    base.update(kw)
    return Config(**base)


# --------------------------------------------------------------------------- registry
class TestProviderRegistry:
    def test_both_providers_registered(self) -> None:
        assert set(PROVIDERS) == {PROVIDER_MINIMAX, PROVIDER_VECTRON}

    def test_normalize_defaults_and_validates(self) -> None:
        assert normalize_provider(None) == PROVIDER_MINIMAX
        assert normalize_provider("") == PROVIDER_MINIMAX
        assert normalize_provider("  VECTRON ") == PROVIDER_VECTRON
        with pytest.raises(ConfigError, match="unknown provider"):
            normalize_provider("openai")

    def test_vectron_serves_h3_only(self) -> None:
        assert provider_models(PROVIDER_VECTRON) == ["MiniMax-H3"]
        assert provider_models(PROVIDER_MINIMAX) == ["MiniMax-H3", "MiniMax-H3-Max"]

    def test_vectron_capability_flags(self) -> None:
        spec = provider_spec(PROVIDER_VECTRON)
        assert spec["supports_context_ir"] is False
        assert spec["supports_image_generation"] is False
        assert spec["supports_prompt_expansion"] is False

    def test_vectron_upload_path_is_h3_namespaced(self) -> None:
        assert provider_spec(PROVIDER_VECTRON)["upload_path"] == "/v1/h3/files/upload"
        assert provider_spec(PROVIDER_MINIMAX)["upload_path"] == "/v1/files/upload"

    def test_model_id_translation(self) -> None:
        assert resolve_model_id(PROVIDER_VECTRON, "MiniMax-H3") == "MiniMax/MiniMax-H3"
        assert resolve_model_id(PROVIDER_MINIMAX, "MiniMax-H3") == "MiniMax-H3"
        assert (
            resolve_model_id(PROVIDER_MINIMAX, "MiniMax-H3-Max") == "MiniMax-H3-Max"
        )

    def test_model_id_rejects_model_the_provider_lacks(self) -> None:
        with pytest.raises(ConfigError, match="unsupported model"):
            resolve_model_id(PROVIDER_VECTRON, "MiniMax-H3-Max")


# --------------------------------------------------------------------------- constraints
class TestProviderConstraints:
    def test_vectron_drops_2k(self) -> None:
        c = provider_constraints(PROVIDER_VECTRON, "MiniMax-H3")
        assert c["resolutions"] == ["480P", "768P"]
        assert c["duration_min"] == 4 and c["duration_max"] == 15

    def test_official_keeps_2k(self) -> None:
        c = provider_constraints(PROVIDER_MINIMAX, "MiniMax-H3")
        assert "2K" in c["resolutions"]

    def test_override_does_not_mutate_global_registry(self) -> None:
        provider_constraints(PROVIDER_VECTRON, "MiniMax-H3")
        assert "2K" in provider_constraints(PROVIDER_MINIMAX, "MiniMax-H3")[
            "resolutions"
        ]

    def test_validate_params_rejects_2k_on_vectron(self) -> None:
        with pytest.raises(ConfigError, match="does not support resolution"):
            validate_model_params("MiniMax-H3", resolution="2K", provider=PROVIDER_VECTRON)

    def test_validate_params_accepts_2k_on_official(self) -> None:
        validate_model_params("MiniMax-H3", resolution="2K", provider=PROVIDER_MINIMAX)

    def test_validate_modes_rejects_h3_max_on_vectron(self) -> None:
        with pytest.raises(ConfigError, match="unsupported model"):
            validate_model_modes("MiniMax-H3-Max", mode="t2v", provider=PROVIDER_VECTRON)


# --------------------------------------------------------------------------- base url
class TestBaseUrlResolution:
    def test_vectron_uses_its_endpoint_when_base_url_is_default(self) -> None:
        cfg = _vec_cfg()
        assert cfg.effective_base_url() == "https://api.vectron.meta-stone.com"

    def test_explicit_override_wins(self) -> None:
        cfg = _vec_cfg(base_url="https://sandbox.example.com/")
        assert cfg.effective_base_url() == "https://sandbox.example.com"

    def test_official_default(self) -> None:
        assert Config(api_key="k").effective_base_url() == DEFAULT_BASE_URL

    def test_client_uses_effective_base_url(self) -> None:
        assert MiniMaxClient(_vec_cfg()).base_url == (
            "https://api.vectron.meta-stone.com"
        )

    def test_capability_properties_follow_provider(self) -> None:
        vec = _vec_cfg()
        assert vec.supports_context_ir is False
        assert vec.supports_image_generation is False
        assert vec.upload_path == "/v1/h3/files/upload"
        official = Config(api_key="k")
        assert official.supports_context_ir is True
        assert official.supports_image_generation is True


# --------------------------------------------------------------------------- client
def _ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body)


def _err(status: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=body)


async def _with_transport(
    cfg: Config, handler: Callable[[httpx.Request], httpx.Response]
) -> tuple[MiniMaxClient, list[httpx.Request]]:
    """Return an already-entered client wired to a MockTransport + call log.

    The returned client is inside its ``async with`` scope; callers await the
    method under test directly (no second ``async with``, which would replace
    the mocked transport).
    """
    calls: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return handler(req)

    client = MiniMaxClient(cfg)
    await client.__aenter__()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        transport=httpx.MockTransport(wrapped),
    )
    return client, calls


class TestVectronClient:
    @pytest.mark.asyncio
    async def test_create_sends_vendor_model_id(self) -> None:
        client, calls = await _with_transport(_vec_cfg(), lambda r: _ok({"task_id": "t1"}))
        tid = await client.create_video_task(
            model="MiniMax-H3",
            content=[{"type": "text", "text": "x"}],
            duration=5,
            resolution="768P",
            ratio="16:9",
        )
        assert tid == "t1"
        body = json.loads(calls[0].content)
        assert body["model"] == "MiniMax/MiniMax-H3"
        assert str(calls[0].url).startswith("https://api.vectron.meta-stone.com/v2/")

    @pytest.mark.asyncio
    async def test_create_rejects_h3_max_before_any_request(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("no request should be made")

        client, calls = await _with_transport(_vec_cfg(), handler)
        with pytest.raises(ConfigError, match="unsupported model"):
            await client.create_video_task(
                model="MiniMax-H3-Max",
                content=[{"type": "text", "text": "x"}],
                duration=5,
            )
        assert calls == []

    @pytest.mark.asyncio
    async def test_upload_uses_vectron_path_and_reads_bare_id(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.png"
        f.write_bytes(b"png")

        def handler(_r: httpx.Request) -> httpx.Response:
            return _ok({"id": "241581705087946753", "status": "active"})

        client, calls = await _with_transport(_vec_cfg(), handler)
        file_id = await client.upload_file(f)
        assert file_id == "241581705087946753"
        assert calls[0].url.path == "/v1/h3/files/upload"

    @pytest.mark.asyncio
    async def test_upload_reads_official_nested_shape(self, tmp_path: Path) -> None:
        f = tmp_path / "a.png"
        f.write_bytes(b"png")

        def handler(_r: httpx.Request) -> httpx.Response:
            return _ok({"file": {"file_id": "file-abc"}})

        client, calls = await _with_transport(Config(api_key="k"), handler)
        file_id = await client.upload_file(f)
        assert file_id == "file-abc"
        assert calls[0].url.path == "/v1/files/upload"

    @pytest.mark.asyncio
    async def test_context_ir_raises_on_vectron(self) -> None:
        client, calls = await _with_transport(_vec_cfg(), lambda r: _ok({"task_id": "x"}))
        with pytest.raises(ConfigError, match="H3-Context-IR"):
            await client.create_context_ir_task(
                model="MiniMax-H3",
                content=[{"type": "text", "text": "x"}],
                duration=5,
            )
        assert calls == []

    @pytest.mark.asyncio
    async def test_image_generation_raises_on_vectron(self) -> None:
        client, _ = await _with_transport(_vec_cfg(), lambda r: _ok({}))
        with pytest.raises(ConfigError, match="image generation"):
            await client.generate_image(prompt="a cat")


class TestVectronErrors:
    """Vectron reports bare HTTP statuses with no numeric code."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,exc",
        [
            (400, InvalidParamsError),
            (401, AuthError),
            (402, BalanceError),
            (422, SafetyError),
        ],
    )
    async def test_status_maps_to_exception(self, status: int, exc: type[Exception]) -> None:
        client, _ = await _with_transport(
            _vec_cfg(), lambda r: _err(status, {"message": "boom"})
        )
        with pytest.raises(exc):
            await client._request_with_retry("GET", "/v2/query/video_generation/x")

    @pytest.mark.asyncio
    async def test_429_retries_then_raises_rate_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import minipic.client as client_mod

        monkeypatch.setattr(client_mod, "_backoff", lambda attempt: 0.001)
        attempts = {"n": 0}

        def handler(_r: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return _err(429, {"message": "rate limit"})

        client, _ = await _with_transport(_vec_cfg(), handler)
        with pytest.raises(RateLimitError):
            await client._request_with_retry("GET", "/x")
        assert attempts["n"] == 4

    @pytest.mark.asyncio
    async def test_500_retries_then_raises_server_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import minipic.client as client_mod

        monkeypatch.setattr(client_mod, "_backoff", lambda attempt: 0.001)
        attempts = {"n": 0}

        def handler(_r: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return _err(500, {"message": "internal"})

        client, _ = await _with_transport(_vec_cfg(), handler)
        with pytest.raises(ServerError):
            await client._request_with_retry("GET", "/x")
        assert attempts["n"] == 4

    @pytest.mark.asyncio
    async def test_official_numeric_code_still_wins(self) -> None:
        """A body carrying both an HTTP status and a numeric code keeps using
        the numeric code — the Vectron path must not shadow the official one.
        """
        client, _ = await _with_transport(
            Config(api_key="k"), lambda r: _err(400, {"code": 2013, "message": "bad"})
        )
        with pytest.raises(InvalidParamsError, match=r"\[2013\]"):
            await client._request_with_retry("POST", "/x", json_body={})


# --------------------------------------------------------------------------- statuses
class TestStatusVocabulary:
    @pytest.mark.parametrize(
        "s", ["Success", "Succeeded", "succeeded", "SUCCEEDED"]
    )
    def test_success_spellings(self, s: str) -> None:
        assert is_success(s) is True
        assert is_failure(s) is False

    @pytest.mark.parametrize(
        "s", ["Failed", "failed", "Cancelled", "cancelled", "Expired", "expired"]
    )
    def test_failure_spellings(self, s: str) -> None:
        assert is_failure(s) is True
        assert is_success(s) is False

    @pytest.mark.parametrize("s", ["queued", "running", "Processing", "Queuing", ""])
    def test_non_terminal_spellings(self, s: str) -> None:
        assert is_success(s) is False
        assert is_failure(s) is False


# --------------------------------------------------------------------------- web
def _make_web_client(cfg: Config) -> TestClient:
    return TestClient(web_mod.create_app(cfg))


class TestWebProviderConfig:
    def test_config_reports_default_provider(self) -> None:
        client = _make_web_client(Config(api_key="k"))
        data = client.get("/api/config").json()
        assert data["provider"] == PROVIDER_MINIMAX
        assert data["supports_context_ir"] is True
        assert [m["name"] for m in data["models"]] == [
            "MiniMax-H3",
            "MiniMax-H3-Max",
        ]
        ids = [p["id"] for p in data["providers"]]
        assert ids == [PROVIDER_MINIMAX, PROVIDER_VECTRON]

    def test_config_reports_vectron_capabilities(self) -> None:
        client = _make_web_client(_vec_cfg())
        data = client.get("/api/config").json()
        assert data["provider"] == PROVIDER_VECTRON
        assert data["supports_context_ir"] is False
        assert [m["name"] for m in data["models"]] == ["MiniMax-H3"]
        h3 = data["models"][0]
        assert h3["resolutions"] == ["480P", "768P"]
        assert data["model_default"] == "MiniMax-H3"

    def test_post_config_switches_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "u" / "config.json"
        monkeypatch.setattr(web_mod, "_user_config_path", lambda: target)
        import minipic.config as config_mod
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: target)

        client = _make_web_client(Config())
        resp = client.post(
            "/api/config", json={"api_key": "vk-1", "provider": PROVIDER_VECTRON}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["provider"] == PROVIDER_VECTRON
        assert resp.json()["provider_changed"] is True

        saved = json.loads(target.read_text(encoding="utf-8"))
        assert saved["provider"] == PROVIDER_VECTRON
        assert saved["api_key"] == "vk-1"
        # A stale official endpoint must not survive the switch.
        assert saved["base_url"] == "https://api.vectron.meta-stone.com"

        data = client.get("/api/config").json()
        assert data["provider"] == PROVIDER_VECTRON
        assert data["has_key"] is True

    def test_post_config_rejects_unknown_provider(self) -> None:
        client = _make_web_client(Config())
        resp = client.post(
            "/api/config", json={"api_key": "k", "provider": "openai"}
        )
        assert resp.status_code == 422

    def test_post_config_without_provider_keeps_current(self) -> None:
        client = _make_web_client(_vec_cfg())
        resp = client.post("/api/config", json={"api_key": "k2"})
        assert resp.status_code == 200
        assert resp.json()["provider"] == PROVIDER_VECTRON


class TestWebCreateOnVectron:
    def test_h3_max_rejected(self) -> None:
        client = _make_web_client(_vec_cfg())
        resp = client.post(
            "/api/create",
            json={
                "mode": "t2v",
                "model": "MiniMax-H3-Max",
                "prompt": "x",
                "ratio": "16:9",
                "resolution": "768P",
                "duration": 5,
            },
        )
        assert resp.status_code == 400
        assert "unsupported model" in resp.json()["detail"]

    def test_2k_rejected(self) -> None:
        client = _make_web_client(_vec_cfg())
        resp = client.post(
            "/api/create",
            json={
                "mode": "t2v",
                "prompt": "x",
                "ratio": "16:9",
                "resolution": "2K",
                "duration": 5,
            },
        )
        assert resp.status_code == 400
        assert "does not support resolution" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_submit_skips_context_ir_and_sends_vendor_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = AsyncMock()
        fake.cfg = _vec_cfg()
        fake.create_video_task = AsyncMock(return_value="tid-vec")
        fake.create_context_ir_task = AsyncMock(return_value="ir-should-not-happen")
        fake.upload_and_resolve = AsyncMock(return_value="https://cdn/x.png")

        body = web_mod.CreateBody(
            mode="t2v", prompt="x", ratio="16:9", resolution="768P", duration=5
        )
        tid, _content, used_ir = await web_mod._submit_task(fake, body)

        assert tid == "tid-vec"
        assert used_ir is False
        fake.create_context_ir_task.assert_not_awaited()
        assert fake.create_video_task.await_args.kwargs["model"] == "MiniMax-H3"

    @pytest.mark.asyncio
    async def test_record_extra_carries_provider(self) -> None:
        """The ledger must remember which vendor billed a task so the UI can
        price it with the right table after a later provider switch.
        """
        from minipic.storage import get_task

        cfg = _vec_cfg()

        class _Fake:
            """Minimal async-CM client — an AsyncMock would return a *new*
            mock from __aenter__ and lose the configured return values.
            """

            def __init__(self, _cfg: Config) -> None:
                self.cfg = _cfg

            async def __aenter__(self) -> "_Fake":
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

            async def create_video_task(self, **_kw: Any) -> str:
                return "tid-vec-store"

            async def create_context_ir_task(self, **_kw: Any) -> str:
                raise AssertionError("Context-IR must not run on Vectron")

        import minipic.web as w

        orig = w.MiniMaxClient
        w.MiniMaxClient = _Fake  # type: ignore[assignment]
        try:
            client = TestClient(w.create_app(cfg))
            resp = client.post(
                "/api/create",
                json={
                    "mode": "t2v",
                    "prompt": "x",
                    "ratio": "16:9",
                    "resolution": "768P",
                    "duration": 5,
                },
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["extra"]["provider"] == PROVIDER_VECTRON
            rec = get_task("tid-vec-store")
            assert rec is not None and rec.extra["provider"] == PROVIDER_VECTRON
        finally:
            w.MiniMaxClient = orig  # type: ignore[assignment]
