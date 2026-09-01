"""Tests for minipic.config — loading, env override, save."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minipic.config import (
    APP_NAME,
    Config,
    ConfigError,  # re-exported from errors
    _env_override,
    _local_config_path,
    _user_config_path,
    _user_data_dir,
    ensure_dirs,
    load_config,
    require_api_key,
    save_config,
    user_data_path,
)
from minipic.errors import ConfigError as _ConfigError  # explicit
import minipic.config as config_mod


# --------------------------------------------------------------------------- Config dataclass
class TestConfigDataclass:
    def test_default_values(self) -> None:
        c = Config()
        assert c.api_key is None
        assert c.base_url == "https://api.minimaxi.com"
        assert c.poll_interval_seconds == 30
        assert c.default_resolution == "768P"
        assert c.default_duration == 10
        assert c.videos_dir == "./videos"
        assert c.request_timeout_seconds == 120
        assert c.extra == {}

    def test_is_valid_false_when_no_key(self) -> None:
        assert Config().is_valid() is False

    def test_is_valid_true_with_non_empty_key(self) -> None:
        assert Config(api_key="x").is_valid() is True

    def test_is_valid_false_with_whitespace_key(self) -> None:
        assert Config(api_key="   ").is_valid() is False

    def test_to_dict_round_trip(self) -> None:
        c = Config(api_key="k", base_url="https://x", extra={"a": 1})
        d = c.to_dict()
        assert d["api_key"] == "k"
        assert d["base_url"] == "https://x"
        assert d["extra"] == {"a": 1}
        # from_dict discards unknown keys
        c2 = Config.from_dict({**d, "unknown": "ignored"})
        assert c2.api_key == "k"
        assert c2.base_url == "https://x"

    def test_from_dict_ignores_unknown_keys(self) -> None:
        c = Config.from_dict({"api_key": "k", "this_is_not_a_field": True})
        assert c.api_key == "k"


# --------------------------------------------------------------------------- load_config — defaults
class TestLoadConfigDefaults:
    def test_returns_default_when_no_files_or_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The conftest already isolates the user data dir to tmp; just ensure
        # cwd has no config.json either.
        cfg = load_config()
        assert isinstance(cfg, Config)
        # api_key should be None (no env, no file)
        assert cfg.api_key is None

    def test_app_name_constant(self) -> None:
        assert APP_NAME == "minipic"


# --------------------------------------------------------------------------- load_config — file precedence
class TestLoadConfigFilePrecedence:
    def test_local_config_overrides_user_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Plant a user config and a local config; local must win.
        user_cfg_dir = tmp_path / "user_cfg"
        user_cfg_dir.mkdir(parents=True, exist_ok=True)
        user_path = user_cfg_dir / "config.json"
        user_path.write_text(
            json.dumps({"api_key": "user-key", "base_url": "https://user"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: user_path)
        # Cwd config
        local = tmp_path / "local_dir"
        local.mkdir()
        (local / "config.json").write_text(
            json.dumps({"api_key": "local-key"}), encoding="utf-8"
        )
        monkeypatch.chdir(local)
        cfg = load_config()
        assert cfg.api_key == "local-key"
        # base_url should still be the user one (local didn't set it)
        assert cfg.base_url == "https://user"

    def test_env_overrides_file_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local = tmp_path / "cwd"
        local.mkdir()
        (local / "config.json").write_text(
            json.dumps({"api_key": "from-file", "base_url": "https://file"}),
            encoding="utf-8",
        )
        monkeypatch.chdir(local)
        monkeypatch.setenv("MINIMAX_API_KEY", "from-env")
        monkeypatch.setenv("MINIMAX_BASE_URL", "https://env")
        cfg = load_config()
        assert cfg.api_key == "from-env"
        assert cfg.base_url == "https://env"

    def test_env_only_no_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIMAX_API_KEY", "just-env")
        cfg = load_config()
        assert cfg.api_key == "just-env"
        # Default base_url
        assert cfg.base_url == "https://api.minimaxi.com"

    def test_malformed_user_config_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json {", encoding="utf-8")
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: bad)
        # Production code propagates JSONDecodeError; we do NOT silently
        # fall through (a broken config is a real error).
        import json as _json
        with pytest.raises(_json.JSONDecodeError):
            load_config()


# --------------------------------------------------------------------------- env var: MINIPIC_CONFIG_PATH
class TestMinipicConfigPath:
    def test_minipic_config_path_overrides_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom = tmp_path / "custom.json"
        custom.write_text(
            json.dumps({"api_key": "custom-key", "default_duration": 7}),
            encoding="utf-8",
        )
        # Also set a local config that should be overridden by MINIPIC_CONFIG_PATH.
        local = tmp_path / "cwd"
        local.mkdir()
        (local / "config.json").write_text(
            json.dumps({"api_key": "local-key"}), encoding="utf-8"
        )
        monkeypatch.chdir(local)
        monkeypatch.setenv("MINIPIC_CONFIG_PATH", str(custom))
        cfg = load_config()
        assert cfg.api_key == "custom-key"
        assert cfg.default_duration == 7

    def test_minipic_config_path_missing_file_silently_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINIPIC_CONFIG_PATH", "/nonexistent/path/config.json")
        # Should not raise; should fall back to other sources
        cfg = load_config()
        assert cfg.api_key is None


# --------------------------------------------------------------------------- _env_override
class TestEnvOverride:
    def test_blank_env_does_not_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIMAX_API_KEY", "")
        monkeypatch.setenv("MINIMAX_BASE_URL", "")
        cfg = Config(api_key="k", base_url="https://x")
        out = _env_override(cfg)
        assert out.api_key == "k"
        assert out.base_url == "https://x"

    def test_whitespace_env_does_not_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIMAX_API_KEY", "   ")
        cfg = Config(api_key="k")
        out = _env_override(cfg)
        assert out.api_key == "k"


# --------------------------------------------------------------------------- save_config
class TestSaveConfig:
    def test_save_user_creates_parent_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Redirect user config to tmp
        target = tmp_path / "deep" / "nested" / "config.json"
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: target)
        path = save_config(Config(api_key="k"), scope="user")
        assert path == target
        assert target.is_file()
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["api_key"] == "k"

    def test_save_local_writes_cwd_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        path = save_config(Config(api_key="local"), scope="local")
        assert path == tmp_path / "config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["api_key"] == "local"

    def test_save_unknown_scope_raises(self) -> None:
        with pytest.raises(_ConfigError, match="unknown scope"):
            save_config(Config(), scope="bogus")

    def test_save_then_load_round_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "u" / "config.json"
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: target)
        cfg = Config(api_key="round-trip", default_duration=12, extra={"x": 1})
        save_config(cfg, scope="user")
        loaded = load_config()
        assert loaded.api_key == "round-trip"
        assert loaded.default_duration == 12
        assert loaded.extra == {"x": 1}


# --------------------------------------------------------------------------- require_api_key
class TestRequireApiKey:
    def test_returns_key_when_valid(self) -> None:
        cfg = Config(api_key="secret")
        assert require_api_key(cfg) == "secret"

    def test_raises_when_no_key(self) -> None:
        cfg = Config()
        with pytest.raises(_ConfigError) as ei:
            require_api_key(cfg)
        msg = str(ei.value)
        # Helpful message should mention both the env var and CLI setup
        assert "MINIMAX_API_KEY" in msg
        assert "minipic config set api_key" in msg

    def test_raises_when_whitespace_key(self) -> None:
        cfg = Config(api_key="   ")
        with pytest.raises(_ConfigError):
            require_api_key(cfg)


# --------------------------------------------------------------------------- ensure_dirs / user_data_path
class TestEnsureDirs:
    def test_ensure_dirs_creates_user_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The conftest already monkeypatches _user_data_dir; we assert that
        # the function under test (ensure_dirs) actually creates the dir.
        # NOTE: look up via the module so the monkeypatch is observed.
        data_dir = config_mod._user_data_dir()
        assert not data_dir.is_dir()  # confirm starting state
        ensure_dirs()
        assert data_dir.is_dir()

    def test_user_data_path_creates_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPDATA", str(tmp_path / "ap"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "ap"))
        p = user_data_path("foo/bar.json")
        assert p.parent.is_dir()
        # The file itself should NOT be created
        assert not p.is_file()


# --------------------------------------------------------------------------- path helpers
class TestPathHelpers:
    def test_user_config_path_is_under_user_config_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # platformdirs will pick up APPDATA on Windows.
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
        p = _user_config_path()
        assert p.name == "config.json"
        assert p.parent.is_dir() or tmp_path in p.parents

    def test_user_data_dir_is_a_path(self) -> None:
        p = _user_data_dir()
        assert isinstance(p, Path)

    def test_local_config_path_uses_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert _local_config_path() == tmp_path / "config.json"


# --------------------------------------------------------------------------- mask_key
class TestMaskKey:
    def test_empty_returns_empty(self) -> None:
        from minipic.config import mask_key
        assert mask_key(None) == ""
        assert mask_key("") == ""

    def test_whitespace_returns_empty(self) -> None:
        from minipic.config import mask_key
        assert mask_key("   ") == ""

    def test_short_key_returns_as_is(self) -> None:
        from minipic.config import mask_key
        assert mask_key("abcd") == "abcd"
        assert mask_key("a") == "a"

    def test_long_key_keeps_last_four(self) -> None:
        from minipic.config import mask_key
        assert mask_key("eyJhbGciOiJIUzI1NiJ9.abcdef123456hYmM") == "...hYmM"

    def test_strips_whitespace_before_masking(self) -> None:
        from minipic.config import mask_key
        # Whitespace is stripped first; the surviving content is then masked
        # by the same rules. "abcdef" is > 4 chars so we get "...XXXX".
        assert mask_key("  abcdef  ") == "...cdef"


# --------------------------------------------------------------------------- model registry + validate_model_params
class TestModelRegistry:
    def test_api_models_lists_both(self) -> None:
        from minipic.config import API_MODELS
        assert "MiniMax-H3" in API_MODELS
        assert "MiniMax-H3-Max" in API_MODELS

    def test_constraints_have_required_fields(self) -> None:
        from minipic.config import MODEL_CONSTRAINTS
        for name, c in MODEL_CONSTRAINTS.items():
            assert "resolutions" in c and len(c["resolutions"]) > 0
            assert "duration_min" in c and "duration_max" in c
            assert c["duration_min"] <= c["duration_max"]

    def test_h3_max_excludes_2k(self) -> None:
        from minipic.config import MODEL_CONSTRAINTS
        assert "2K" not in MODEL_CONSTRAINTS["MiniMax-H3-Max"]["resolutions"]
        assert "2K" in MODEL_CONSTRAINTS["MiniMax-H3"]["resolutions"]

    def test_h3_max_duration_min_is_5(self) -> None:
        from minipic.config import MODEL_CONSTRAINTS
        assert MODEL_CONSTRAINTS["MiniMax-H3-Max"]["duration_min"] == 5
        assert MODEL_CONSTRAINTS["MiniMax-H3"]["duration_min"] == 4


class TestValidateModelParams:
    def test_unknown_model_raises(self) -> None:
        from minipic.config import validate_model_params
        with pytest.raises(_ConfigError, match="unsupported model"):
            validate_model_params("MiniMax-X9")

    def test_h3_accepts_768p_and_2k(self) -> None:
        from minipic.config import validate_model_params
        validate_model_params("MiniMax-H3", resolution="768P", duration=10)  # ok
        validate_model_params("MiniMax-H3", resolution="2K", duration=4)     # ok

    def test_h3_rejects_480p(self) -> None:
        from minipic.config import validate_model_params
        with pytest.raises(_ConfigError, match="480P"):
            validate_model_params("MiniMax-H3", resolution="480P", duration=10)

    def test_h3_rejects_duration_3(self) -> None:
        from minipic.config import validate_model_params
        with pytest.raises(_ConfigError, match="duration"):
            validate_model_params("MiniMax-H3", resolution="768P", duration=3)

    def test_h3_max_accepts_480p_and_768p(self) -> None:
        from minipic.config import validate_model_params
        validate_model_params("MiniMax-H3-Max", resolution="480P", duration=5)  # ok
        validate_model_params("MiniMax-H3-Max", resolution="768P", duration=15)  # ok

    def test_h3_max_rejects_2k(self) -> None:
        from minipic.config import validate_model_params
        with pytest.raises(_ConfigError, match="2K"):
            validate_model_params("MiniMax-H3-Max", resolution="2K", duration=10)

    def test_h3_max_rejects_duration_4(self) -> None:
        from minipic.config import validate_model_params
        with pytest.raises(_ConfigError, match="duration"):
            validate_model_params("MiniMax-H3-Max", resolution="768P", duration=4)

    def test_no_params_is_noop(self) -> None:
        from minipic.config import validate_model_params
        validate_model_params("MiniMax-H3")  # no resolution/duration → ok
        validate_model_params("MiniMax-H3-Max")  # ok

    def test_resolution_only(self) -> None:
        from minipic.config import validate_model_params
        # Resolution-only: H3 supports both 768P and 2K
        validate_model_params("MiniMax-H3", resolution="768P")
        validate_model_params("MiniMax-H3", resolution="2K")
        # H3-Max + 2K is the real failure case
        with pytest.raises(_ConfigError):
            validate_model_params("MiniMax-H3-Max", resolution="2K")
        # H3-Max + 480P is fine
        validate_model_params("MiniMax-H3-Max", resolution="480P")
