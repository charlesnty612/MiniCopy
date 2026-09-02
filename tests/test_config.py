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
    def test_user_config_overrides_local_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.2.0+: user config wins over local cwd config.json. The user
        config file is the single source of truth for the API key; the local
        config only matters as a developer-time fallback when no user config
        exists. base_url still falls back to local if user didn't set it."""
        user_cfg_dir = tmp_path / "user_cfg"
        user_cfg_dir.mkdir(parents=True, exist_ok=True)
        user_path = user_cfg_dir / "config.json"
        user_path.write_text(
            json.dumps({"api_key": "user-key", "base_url": "https://user"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: user_path)
        # Cwd config with a different api_key — must NOT win.
        local = tmp_path / "local_dir"
        local.mkdir()
        (local / "config.json").write_text(
            json.dumps({"api_key": "local-key"}), encoding="utf-8"
        )
        monkeypatch.chdir(local)
        cfg = load_config()
        assert cfg.api_key == "user-key"
        # base_url comes from user (user file is read after local; user wins)
        assert cfg.base_url == "https://user"

    def test_local_used_when_no_user_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.2.0+: when no user config exists, local cwd config.json is the
        file source. (conftest's autouse fixture already isolates the user
        config path to an empty tmp file.)"""
        local = tmp_path / "local_dir"
        local.mkdir()
        (local / "config.json").write_text(
            json.dumps({"api_key": "local-only"}), encoding="utf-8"
        )
        monkeypatch.chdir(local)
        cfg = load_config()
        assert cfg.api_key == "local-only"

    def test_user_wins_over_local_and_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.2.0+ acceptance: user config wins over both local config and env."""
        user_path = tmp_path / "user_cfg" / "config.json"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(json.dumps({"api_key": "user-wins"}), encoding="utf-8")
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: user_path)
        # Local config + env var both set; user must still win.
        local = tmp_path / "cwd"
        local.mkdir()
        (local / "config.json").write_text(
            json.dumps({"api_key": "local-loses"}), encoding="utf-8"
        )
        monkeypatch.chdir(local)
        monkeypatch.setenv("MINIMAX_API_KEY", "env-loses")
        cfg = load_config()
        assert cfg.api_key == "user-wins"

    def test_local_wins_over_env_when_no_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.2.0+ acceptance: no user config + local + env → local wins."""
        # conftest already isolates user config to an empty tmp file
        local = tmp_path / "cwd"
        local.mkdir()
        (local / "config.json").write_text(
            json.dumps({"api_key": "local-wins"}), encoding="utf-8"
        )
        monkeypatch.chdir(local)
        monkeypatch.setenv("MINIMAX_API_KEY", "env-loses")
        cfg = load_config()
        assert cfg.api_key == "local-wins"

    def test_env_fallback_when_no_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.2.0+ acceptance: no user / no local + env → env fallback."""
        # conftest: user is isolated to empty tmp; cwd has no config.json.
        empty = tmp_path / "no_config"
        empty.mkdir()
        monkeypatch.chdir(empty)
        monkeypatch.setenv("MINIMAX_API_KEY", "env-only")
        cfg = load_config()
        assert cfg.api_key == "env-only"

    def test_env_api_key_does_not_override_file_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.2.0+：MINIMAX_API_KEY 不再覆盖文件 api_key；UI 保存的 user
        key 必须能跨进程持久化，且 env 仅在文件无 key 时兜底。base_url
        仍然被 env 覆盖（不是 api_key 范畴）。"""
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
        # api_key：文件优先，env 不再覆盖
        assert cfg.api_key == "from-file"
        # base_url：env 仍然覆盖（行为不变）
        assert cfg.base_url == "https://env"

    def test_env_api_key_overrides_when_no_file_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.1.7+：当 user/local 文件都没有 api_key 时，env 兜底生效。"""
        # cwd 没有 config.json（conftest 已 chdir 到 tmp_path）；
        # user config 被 conftest 隔离到一个空路径。
        monkeypatch.setenv("MINIMAX_API_KEY", "just-env")
        cfg = load_config()
        assert cfg.api_key == "just-env"

    def test_env_api_key_overridden_by_user_file_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.1.7+：仅 user config 有 key、local 没有时，user key 覆盖 env。"""
        user_cfg_dir = tmp_path / "user_only"
        user_cfg_dir.mkdir(parents=True, exist_ok=True)
        user_path = user_cfg_dir / "config.json"
        user_path.write_text(json.dumps({"api_key": "user-only-key"}), encoding="utf-8")
        monkeypatch.setattr(config_mod, "_user_config_path", lambda: user_path)
        # cwd 无 config.json
        empty = tmp_path / "no_local"
        empty.mkdir()
        monkeypatch.chdir(empty)
        monkeypatch.setenv("MINIMAX_API_KEY", "from-env")
        cfg = load_config()
        assert cfg.api_key == "user-only-key"

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
            # 新增能力字段
            assert "modes" in c and len(c["modes"]) > 0
            assert "i2v_roles" in c and len(c["i2v_roles"]) > 0

    def test_h3_max_excludes_2k(self) -> None:
        from minipic.config import MODEL_CONSTRAINTS
        assert "2K" not in MODEL_CONSTRAINTS["MiniMax-H3-Max"]["resolutions"]
        assert "2K" in MODEL_CONSTRAINTS["MiniMax-H3"]["resolutions"]

    def test_h3_max_duration_min_is_5(self) -> None:
        from minipic.config import MODEL_CONSTRAINTS
        assert MODEL_CONSTRAINTS["MiniMax-H3-Max"]["duration_min"] == 5
        assert MODEL_CONSTRAINTS["MiniMax-H3"]["duration_min"] == 4

    def test_h3_modes_and_roles(self) -> None:
        from minipic.config import MODEL_CONSTRAINTS
        h3 = MODEL_CONSTRAINTS["MiniMax-H3"]
        assert h3["modes"] == ["t2v", "i2v", "r2v"]
        # 官方 API schema 的 role 枚举只有 first_frame / last_frame /
        # reference_image / reference_video / reference_audio，没有 middle_frame；
        # H3 与 H3-Max 已统一收紧为首/尾帧两角色。
        assert h3["i2v_roles"] == ["first_frame", "last_frame"]
        assert "middle_frame" not in h3["i2v_roles"]

    def test_h3_max_modes_and_roles(self) -> None:
        from minipic.config import MODEL_CONSTRAINTS
        h3m = MODEL_CONSTRAINTS["MiniMax-H3-Max"]
        assert h3m["modes"] == ["t2v", "i2v"]
        # H3-Max 不支持 r2v 与中间帧
        assert "r2v" not in h3m["modes"]
        assert "middle_frame" not in h3m["i2v_roles"]
        assert h3m["i2v_roles"] == ["first_frame", "last_frame"]


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


# --------------------------------------------------------------------------- validate_model_modes
class TestValidateModelModes:
    def test_unknown_model_raises(self) -> None:
        from minipic.config import validate_model_modes
        with pytest.raises(_ConfigError, match="unsupported model"):
            validate_model_modes("MiniMax-X9", mode="t2v")

    def test_h3_three_modes_pass(self) -> None:
        from minipic.config import validate_model_modes
        for m in ("t2v", "i2v", "r2v"):
            validate_model_modes("MiniMax-H3", mode=m)  # ok

    def test_h3_first_and_last_frame_pass(self) -> None:
        from minipic.config import validate_model_modes
        # H3 支持首帧/尾帧（v0.1.6+：中间帧被 schema 收紧后已不再支持）
        for r in ("first_frame", "last_frame"):
            validate_model_modes("MiniMax-H3", mode="i2v", i2v_role=r)

    def test_h3_rejects_middle_frame(self) -> None:
        from minipic.config import validate_model_modes
        with pytest.raises(_ConfigError, match="middle_frame"):
            validate_model_modes("MiniMax-H3", mode="i2v", i2v_role="middle_frame")

    def test_h3_max_rejects_middle_frame(self) -> None:
        from minipic.config import validate_model_modes
        with pytest.raises(_ConfigError, match="middle_frame"):
            validate_model_modes("MiniMax-H3-Max", mode="i2v", i2v_role="middle_frame")

    def test_h3_max_rejects_r2v_with_hint(self) -> None:
        from minipic.config import validate_model_modes
        with pytest.raises(_ConfigError) as ei:
            validate_model_modes("MiniMax-H3-Max", mode="r2v")
        msg = str(ei.value)
        assert "MiniMax-H3-Max" in msg
        assert "r2v" in msg
        # 必须给出明确的回退提示
        assert "不支持多模态参考" in msg
        assert "MiniMax-H3" in msg

    def test_h3_max_first_and_last_pass(self) -> None:
        from minipic.config import validate_model_modes
        validate_model_modes("MiniMax-H3-Max", mode="i2v", i2v_role="first_frame")
        validate_model_modes("MiniMax-H3-Max", mode="i2v", i2v_role="last_frame")

    def test_unknown_mode_rejected(self) -> None:
        from minipic.config import validate_model_modes
        with pytest.raises(_ConfigError, match="不支持模式"):
            validate_model_modes("MiniMax-H3", mode="bogus")

    def test_no_args_is_noop(self) -> None:
        from minipic.config import validate_model_modes
        # 不传 mode / i2v_role 时什么都不校验
        validate_model_modes("MiniMax-H3")
        validate_model_modes("MiniMax-H3-Max")
