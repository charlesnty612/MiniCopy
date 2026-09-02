"""Configuration loading and storage.

Resolution order (highest priority first):
  1. User config dir (%APPDATA%/minipic on Windows, ~/.config/minipic on Linux/Mac)
     — this is the **single source of truth for the API key**, written by the
     Web UI and read by both the UI and the CLI. A UI-saved key persists
     across restarts and wins over every other source.
  2. Local config.json (cwd) — falls back to the user config when no user
     config exists; useful for repo-local overrides during development.
  3. Environment variables (MINIMAX_API_KEY / MINIMAX_BASE_URL) — used only
     as a fallback for fields the file sources don't supply. For api_key in
     particular, MINIMAX_API_KEY only takes effect when neither user nor
     local config defines an api_key (pure fallback). MINIMAX_BASE_URL still
     overrides the file value when set, since it is the intended switch for
     sandbox/custom endpoints.

Values can be inspected and updated via the `minipic config` CLI.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from platformdirs import user_config_dir, user_data_dir

from .errors import ConfigError

APP_NAME = "minipic"
APP_AUTHOR = "minipic"

# ---- model registry (kept in sync with MiniMax /v2/video_generation docs) ----

API_MODELS: list[str] = ["MiniMax-H3", "MiniMax-H3-Max"]

MODEL_CONSTRAINTS: dict[str, dict[str, Any]] = {
    "MiniMax-H3": {
        "resolutions": ["768P", "2K"],
        "duration_min": 4,
        "duration_max": 15,
        # H3 全能力：文生视频、图生视频（首帧/尾帧）、多模态参考。
        # 注意：官方能力文本曾提过"中间帧"，但 V2 API schema 的 role 枚举
        # 只有 first_frame/last_frame/reference_image/reference_video/reference_audio，
        # 没有 middle_frame —— 这里按 schema 收紧为首/尾帧两角色。
        "modes": ["t2v", "i2v", "r2v"],
        "i2v_roles": ["first_frame", "last_frame"],
    },
    "MiniMax-H3-Max": {
        "resolutions": ["480P", "768P"],
        "duration_min": 5,
        "duration_max": 15,
        # H3-Max 极速版：仅文生视频与图生视频（首帧/尾帧），
        # 不支持中间帧与多模态参考（参考图/参考视频/参考音频）。
        "modes": ["t2v", "i2v"],
        "i2v_roles": ["first_frame", "last_frame"],
    },
}


def mask_key(key: Optional[str]) -> str:
    """Return a UI-safe representation of an API key.

    Returns "" for empty/None. For keys with 4 or fewer visible chars, returns
    the key itself. Otherwise returns ``"...<last 4 chars>"``.
    """
    if not key:
        return ""
    s = key.strip()
    if len(s) <= 4:
        return s
    return "..." + s[-4:]


def validate_model_params(
    model: str,
    *,
    resolution: Optional[str] = None,
    duration: Optional[int] = None,
) -> None:
    """Raise ConfigError if model/resolution/duration is unsupported.

    At least one of ``resolution`` / ``duration`` must be supplied; otherwise
    this is a no-op. Empty / None for the optional parameter means "skip".
    """
    if model not in MODEL_CONSTRAINTS:
        raise ConfigError(
            f"unsupported model: {model!r} (supported: {', '.join(API_MODELS)})"
        )
    constraints = MODEL_CONSTRAINTS[model]
    if resolution is not None and resolution != "":
        if resolution not in constraints["resolutions"]:
            raise ConfigError(
                f"model {model!r} does not support resolution {resolution!r} "
                f"(allowed: {', '.join(constraints['resolutions'])})"
            )
    if duration is not None:
        lo = constraints["duration_min"]
        hi = constraints["duration_max"]
        if duration < lo or duration > hi:
            raise ConfigError(
                f"model {model!r} duration must be in [{lo}, {hi}], got {duration}"
            )


def validate_model_modes(
    model: str,
    *,
    mode: Optional[str] = None,
    i2v_role: Optional[str] = None,
) -> None:
    """校验模型支持的 mode 与 i2v role。

    - model 不在 MODEL_CONSTRAINTS → ConfigError。
    - mode 不为 None 且不在 constraints["modes"] → ConfigError，
      文案含模型名、模式名与支持列表，并对 H3-Max 的 r2v 给出明确提示。
    - i2v_role 不为 None 且不在 constraints["i2v_roles"] → ConfigError，
      文案含模型名、角色名与支持列表。
    """
    if model not in MODEL_CONSTRAINTS:
        raise ConfigError(
            f"unsupported model: {model!r} (supported: {', '.join(API_MODELS)})"
        )
    constraints = MODEL_CONSTRAINTS[model]

    if mode is not None and mode not in constraints["modes"]:
        supported = ", ".join(constraints["modes"])
        hint = ""
        if model == "MiniMax-H3-Max" and mode == "r2v":
            hint = (
                "；H3-Max 不支持多模态参考（参考图/参考视频/参考音频），"
                "请改用 MiniMax-H3 或 t2v/i2v"
            )
        raise ConfigError(
            f"模型 {model} 不支持模式 {mode}（支持: {supported}）{hint}"
        )

    if i2v_role is not None and i2v_role not in constraints["i2v_roles"]:
        supported = ", ".join(constraints["i2v_roles"])
        raise ConfigError(
            f"模型 {model} 的图生视频不支持角色 {i2v_role}（支持: {supported}）"
        )


@dataclass
class Config:
    api_key: Optional[str] = None
    base_url: str = "https://api.minimaxi.com"
    poll_interval_seconds: int = 30
    default_resolution: str = "768P"
    default_duration: int = 10
    videos_dir: str = "./videos"
    request_timeout_seconds: int = 120
    extra: dict[str, Any] = field(default_factory=dict)

    def is_valid(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _user_config_path() -> Path:
    return Path(user_config_dir(APP_NAME, APP_AUTHOR, roaming=False)) / "config.json"


def _user_data_dir() -> Path:
    return Path(user_data_dir(APP_NAME, APP_AUTHOR, roaming=False))


def _local_config_path() -> Path:
    return Path.cwd() / "config.json"


def _env_override(cfg: Config) -> Config:
    """Apply env-var overrides to ``cfg``.

    Priority semantics (v0.2.0+):
      * MINIMAX_API_KEY only overrides ``cfg.api_key`` when neither the user
        config file nor the local config file defines a non-empty ``api_key``.
        This lets UI-saved (user) keys persist across restarts and take
        precedence over the environment variable; the env var is a pure
        fallback when no file key exists. (``load_config`` already arranges
        user > local precedence; this guard closes the last gap by checking
        either file before letting the env var in.)
      * MINIMAX_BASE_URL still overrides the file value when set (env is the
        intended switch for sandbox/custom endpoints).
      * MINIPIC_CONFIG_PATH overlays any matching file on top of cfg.
    """
    if (v := os.environ.get("MINIMAX_API_KEY", "").strip()):
        if not _file_has_api_key():
            cfg.api_key = v
    if (v := os.environ.get("MINIMAX_BASE_URL", "").strip()):
        cfg.base_url = v
    if (v := os.environ.get("MINIPIC_CONFIG_PATH", "").strip()):
        # allow user to point to a specific file
        path = Path(v)
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return Config.from_dict({**cfg.to_dict(), **data})
    return cfg


def _file_has_api_key() -> bool:
    """Return True if either user or local config file has a non-empty api_key.

    Used by ``_env_override`` to decide whether MINIMAX_API_KEY should win.
    Reads files defensively: a missing/unreadable file is treated as "no key".
    """
    for path in (_user_config_path(), _local_config_path()):
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data.get("api_key") or "").strip():
                return True
        except (OSError, ValueError):
            continue
    return False


def load_config() -> Config:
    """Load config with the documented priority order: user > local > env.

    User config is the **single source of truth for the API key** (v0.2.0+).
    The Web UI writes the user config file; the CLI reads the same file via
    this function. Local cwd config.json acts as a developer-time override
    when no user config exists. Env vars are the last-resort fallback for
    api_key (and the standard switch for base_url).
    """
    cfg = Config()

    # 1. local cwd config.json (lowest priority among file sources)
    local_path = _local_config_path()
    if local_path.is_file():
        data = json.loads(local_path.read_text(encoding="utf-8"))
        cfg = Config.from_dict({**cfg.to_dict(), **data})

    # 2. user config dir (wins over local — single source of truth for api_key)
    user_path = _user_config_path()
    if user_path.is_file():
        data = json.loads(user_path.read_text(encoding="utf-8"))
        cfg = Config.from_dict({**cfg.to_dict(), **data})

    # 3. env vars — pure fallback for api_key; base_url still overrides file
    cfg = _env_override(cfg)

    return cfg


def save_config(cfg: Config, scope: str = "user") -> Path:
    """Save config to user config dir (default) or cwd (scope='local')."""
    if scope == "local":
        path = _local_config_path()
    elif scope == "user":
        path = _user_config_path()
    else:
        raise ConfigError(f"unknown scope: {scope!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def ensure_dirs() -> None:
    """Make sure user data + videos dirs exist."""
    _user_data_dir().mkdir(parents=True, exist_ok=True)
    Path("./videos").mkdir(parents=True, exist_ok=True)


def user_data_path(filename: str) -> Path:
    """Return a path inside the user data dir (creates parent if needed)."""
    path = _user_data_dir() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def require_api_key(cfg: Config) -> str:
    """Return the API key or raise ConfigError with a helpful message."""
    if not cfg.is_valid():
        raise ConfigError(
            "No MiniMax API key configured.\n"
            "Set one with:\n"
            "  minipic config set api_key <YOUR_KEY>\n"
            "or env var:\n"
            "  $env:MINIMAX_API_KEY=\"<YOUR_KEY>\"  (PowerShell)\n"
            "  export MINIMAX_API_KEY=\"<YOUR_KEY>\"  (bash)\n"
            "Get a key at https://platform.minimaxi.com → 账户管理 → 接口密钥"
        )
    assert cfg.api_key is not None
    return cfg.api_key
