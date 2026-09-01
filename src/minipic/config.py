"""Configuration loading and storage.

Resolution order (highest priority first):
  1. Environment variables (MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMIC_CONFIG_PATH)
  2. Local config.json (cwd or MINIMIC_CONFIG_PATH)
  3. User config dir (%APPDATA%/minipic on Windows, ~/.config/minipic on Linux/Mac)

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
    },
    "MiniMax-H3-Max": {
        "resolutions": ["480P", "768P"],
        "duration_min": 5,
        "duration_max": 15,
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
    """Override config with environment variables where set."""
    if (v := os.environ.get("MINIMAX_API_KEY", "").strip()):
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


def load_config() -> Config:
    """Load config with the documented priority order."""
    cfg = Config()

    # 1. user config dir (lowest priority among file sources)
    user_path = _user_config_path()
    if user_path.is_file():
        data = json.loads(user_path.read_text(encoding="utf-8"))
        cfg = Config.from_dict({**cfg.to_dict(), **data})

    # 2. local cwd config.json (overrides user config)
    local_path = _local_config_path()
    if local_path.is_file():
        data = json.loads(local_path.read_text(encoding="utf-8"))
        cfg = Config.from_dict({**cfg.to_dict(), **data})

    # 3. env vars (highest priority)
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
