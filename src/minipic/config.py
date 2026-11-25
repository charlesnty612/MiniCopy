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

Providers (v0.2.8+)
-------------------
The same MiniMax H3 V2 protocol is served by more than one vendor. ``provider``
selects which one the client talks to; it changes the base URL, the upload
endpoint, the wire model id, and the capability set (Vectron, for instance, has
no H3-Max, no 2K, and no Context-IR). The canonical model names
(``MiniMax-H3`` / ``MiniMax-H3-Max``) stay fixed everywhere in this codebase,
in the ledger and in the UI; :func:`resolve_model_id` translates them to the
vendor's wire id right before the request body is built.

Values can be inspected and updated via the `minipic config` CLI.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from platformdirs import user_config_dir, user_data_dir

from .errors import ConfigError

log = logging.getLogger(__name__)

APP_NAME = "minipic"
APP_AUTHOR = "minipic"

# ---------------------------------------------------------------------------
# Provider registry (v0.2.8+)
# ---------------------------------------------------------------------------
# More than one vendor serves the MiniMax H3 V2 protocol. ``provider`` picks
# which one we talk to; it decides the base URL, the upload endpoint, the wire
# model id, and the capability set. Everything else in the codebase keeps using
# the canonical model names below, so the ledger / UI / CLI never have to know
# about vendor-specific ids.

PROVIDER_MINIMAX = "minimax"
PROVIDER_VECTRON = "vectron"
DEFAULT_PROVIDER = PROVIDER_MINIMAX
DEFAULT_BASE_URL = "https://api.minimaxi.com"

PROVIDERS: dict[str, dict[str, Any]] = {
    PROVIDER_MINIMAX: {
        "label": "MiniMax 开放平台（官方）",
        "short": "官方",
        "base_url": DEFAULT_BASE_URL,
        # V1 file API; response carries file.file_id (or a top-level file_id).
        "upload_path": "/v1/files/upload",
        "models": ["MiniMax-H3", "MiniMax-H3-Max"],
        # canonical name -> id on the wire
        "model_ids": {
            "MiniMax-H3": "MiniMax-H3",
            "MiniMax-H3-Max": "MiniMax-H3-Max",
        },
        "supports_context_ir": True,
        "supports_image_generation": True,
        "supports_prompt_expansion": True,
        "key_hint": "https://platform.minimaxi.com → 账户管理 → 接口密钥",
    },
    PROVIDER_VECTRON: {
        "label": "拓元 Vectron（H3 内测 · 官方 1 折）",
        "short": "拓元",
        "base_url": "https://api.vectron.meta-stone.com",
        # Vectron exposes the same file protocol under /v1/h3/files and returns
        # a bare ``{"id": "..."}`` instead of the official nested file object.
        "upload_path": "/v1/h3/files/upload",
        "models": ["MiniMax-H3"],
        "model_ids": {"MiniMax-H3": "MiniMax/MiniMax-H3"},
        # Vectron ships the H3 model only: no H3-Max, no Context-IR, no image-01.
        "supports_context_ir": False,
        "supports_image_generation": False,
        "supports_prompt_expansion": False,
        "key_hint": "拓元平台内测开通后由群主发放 key",
    },
}

# ---- model registry (canonical names, kept in sync with MiniMax /v2/video_generation docs) ----

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
        # H3-Max 极速版：文生视频、图生视频（首帧/尾帧），
        # 多模态参考（参考图/参考视频/参考音频，r2v）。
        # 仍不支持中间帧（i2v_roles 仅 first/last_frame）。
        "modes": ["t2v", "i2v", "r2v"],
        "i2v_roles": ["first_frame", "last_frame"],
    },
}

# Per-provider overrides of MODEL_CONSTRAINTS. Vectron serves one H3 tier whose
# resolution ladder is 480P/768P (no 2K) — see the Vectron H3 API reference.
PROVIDER_CONSTRAINT_OVERRIDES: dict[str, dict[str, dict[str, Any]]] = {
    PROVIDER_VECTRON: {
        "MiniMax-H3": {"resolutions": ["480P", "768P"]},
    },
}


def normalize_provider(provider: Optional[str]) -> str:
    """Return a canonical provider key, defaulting to the official platform."""
    key = (provider or "").strip().lower()
    if not key:
        return DEFAULT_PROVIDER
    if key not in PROVIDERS:
        raise ConfigError(
            f"unknown provider: {provider!r} "
            f"(supported: {', '.join(PROVIDERS)})"
        )
    return key


def provider_spec(provider: Optional[str]) -> dict[str, Any]:
    """Return the provider's capability descriptor (see PROVIDERS)."""
    return PROVIDERS[normalize_provider(provider)]


def provider_models(provider: Optional[str]) -> list[str]:
    """Canonical model names available on ``provider``."""
    return list(provider_spec(provider)["models"])


def resolve_model_id(provider: Optional[str], model: str) -> str:
    """Translate a canonical model name to the vendor's wire id.

    Both surfaces use the canonical names internally; only the request body
    sent to the vendor differs (e.g. ``MiniMax-H3`` → ``MiniMax/MiniMax-H3``).
    """
    spec = provider_spec(provider)
    model_id = spec["model_ids"].get(model)
    if not model_id:
        raise ConfigError(
            f"unsupported model: {model!r} on provider "
            f"{normalize_provider(provider)!r} "
            f"(available: {', '.join(spec['models'])})"
        )
    return model_id


def provider_constraints(provider: Optional[str], model: str) -> dict[str, Any]:
    """Return MODEL_CONSTRAINTS[model] with this provider's overrides applied."""
    spec = provider_spec(provider)
    if model not in spec["models"]:
        raise ConfigError(
            f"unsupported model: {model!r} on provider "
            f"{normalize_provider(provider)!r} "
            f"(available: {', '.join(spec['models'])})"
        )
    base = dict(MODEL_CONSTRAINTS[model])
    base.update(PROVIDER_CONSTRAINT_OVERRIDES.get(normalize_provider(provider), {}).get(model, {}))
    return base



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
    provider: Optional[str] = None,
) -> None:
    """Raise ConfigError if model/resolution/duration is unsupported.

    At least one of ``resolution`` / ``duration`` must be supplied; otherwise
    this is a no-op. Empty / None for the optional parameter means "skip".

    ``provider`` selects the capability set: a provider that does not serve
    ``model`` at all, or serves it with a narrower ladder (Vectron: no 2K),
    is rejected here with the provider's actual allowed values.
    """
    constraints = provider_constraints(provider, model)
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
    provider: Optional[str] = None,
) -> None:
    """校验模型支持的 mode 与 i2v role。

    - model 不在该 provider 的可用模型里 → ConfigError。
    - mode 不为 None 且不在 constraints["modes"] → ConfigError，
      文案含模型名、模式名与支持列表。
    - i2v_role 不为 None 且不在 constraints["i2v_roles"] → ConfigError，
      文案含模型名、角色名与支持列表。
    """
    constraints = provider_constraints(provider, model)

    if mode is not None and mode not in constraints["modes"]:
        supported = ", ".join(constraints["modes"])
        raise ConfigError(
            f"模型 {model} 不支持模式 {mode}（支持: {supported}）"
        )

    if i2v_role is not None and i2v_role not in constraints["i2v_roles"]:
        supported = ", ".join(constraints["i2v_roles"])
        raise ConfigError(
            f"模型 {model} 的图生视频不支持角色 {i2v_role}（支持: {supported}）"
        )


@dataclass
class Config:
    api_key: Optional[str] = None
    base_url: str = DEFAULT_BASE_URL
    provider: str = DEFAULT_PROVIDER
    poll_interval_seconds: int = 30
    default_resolution: str = "768P"
    default_duration: int = 10
    videos_dir: str = "./videos"
    request_timeout_seconds: int = 120
    extra: dict[str, Any] = field(default_factory=dict)

    def is_valid(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    def provider_key(self) -> str:
        """Normalized provider key ("minimax" when unset/unknown)."""
        try:
            return normalize_provider(self.provider)
        except ConfigError:
            return DEFAULT_PROVIDER

    def provider_spec(self) -> dict[str, Any]:
        return provider_spec(self.provider)

    @property
    def upload_path(self) -> str:
        return str(self.provider_spec()["upload_path"])

    @property
    def supports_context_ir(self) -> bool:
        return bool(self.provider_spec()["supports_context_ir"])

    @property
    def supports_image_generation(self) -> bool:
        return bool(self.provider_spec()["supports_image_generation"])

    def effective_base_url(self) -> str:
        """Base URL to actually call.

        ``base_url`` doubles as the user's explicit override (env var or a
        custom endpoint). It is only treated as an override when it differs
        from the official default; otherwise the selected provider's own
        endpoint wins. That keeps saved configs — which always persist a
        concrete ``base_url`` — from pinning the official host after the user
        switches providers.
        """
        raw = (self.base_url or "").strip().rstrip("/")
        if raw and raw != DEFAULT_BASE_URL:
            return raw
        return str(self.provider_spec()["base_url"]).rstrip("/")

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
      * MINIPIC_PROVIDER selects the vendor (v0.2.8+). Like the API key it is
        a fallback: a provider saved through the UI wins, the env var only
        fills the gap for CLI/developer runs that never saved one.
      * MINIPIC_CONFIG_PATH overlays any matching file on top of cfg.
    """
    if (v := os.environ.get("MINIMAX_API_KEY", "").strip()):
        if not _file_has_api_key():
            cfg.api_key = v
    if (v := os.environ.get("MINIMAX_BASE_URL", "").strip()):
        cfg.base_url = v
    if (v := os.environ.get("MINIPIC_PROVIDER", "").strip()):
        if not _file_has_provider():
            try:
                cfg.provider = normalize_provider(v)
            except ConfigError:
                log.warning("ignoring unknown MINIPIC_PROVIDER=%r", v)
    if (v := os.environ.get("MINIPIC_CONFIG_PATH", "").strip()):
        # allow user to point to a specific file
        path = Path(v)
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return Config.from_dict({**cfg.to_dict(), **data})
    return cfg


def _file_has_provider() -> bool:
    """Return True if either user or local config file pins a provider.

    Same defensive read as ``_file_has_api_key``: an unknown/unreadable entry
    counts as "not set" so the env var can still fill it in.
    """
    for path in (_user_config_path(), _local_config_path()):
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data.get("provider") or "").strip():
                return True
        except (OSError, ValueError):
            continue
    return False


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
        spec = cfg.provider_spec()
        raise ConfigError(
            "No MiniMax API key configured.\n"
            "Set one with:\n"
            "  minipic config set api_key <YOUR_KEY>\n"
            "or env var:\n"
            "  $env:MINIMAX_API_KEY=\"<YOUR_KEY>\"  (PowerShell)\n"
            "  export MINIMAX_API_KEY=\"<YOUR_KEY>\"  (bash)\n"
            f"Get a key at {spec['key_hint']}"
        )
    assert cfg.api_key is not None
    return cfg.api_key
