"""Runtime configuration resolution for ZotPilot."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import SECRET_FIELDS, Config, _default_config_dir, _old_config_path
from .secret_store import describe_backend, get_secret
from .secrets_env import describe_env_file, load_env_file

__all__ = [
    "ENV_ALIASES",
    "ENV_LOOKUP",
    "ENV_TO_FIELD",
    "FIELD_TO_ENV",
    "SECRET_FIELDS",
    "ResolvedRuntimeSettings",
    "resolve_runtime_config",
    "resolve_runtime_settings",
]

ENV_TO_FIELD: dict[str, str] = {
    "GEMINI_API_KEY": "gemini_api_key",
    "DASHSCOPE_API_KEY": "dashscope_api_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "ZOTERO_API_KEY": "zotero_api_key",
    "ZOTERO_USER_ID": "zotero_user_id",
    "OPENALEX_EMAIL": "openalex_email",
    "S2_API_KEY": "semantic_scholar_api_key",
}

# Alternative spellings accepted from the environment and from the secrets
# file. Only unambiguous aliases belong here: a generic name such as
# GOOGLE_API_KEY may well have been exported for something else entirely.
ENV_ALIASES: dict[str, str] = {
    "SEMANTIC_SCHOLAR_API_KEY": "semantic_scholar_api_key",
}

# Canonical names first, so a canonical spelling wins when both are present.
ENV_LOOKUP: dict[str, str] = {**ENV_TO_FIELD, **ENV_ALIASES}

# The env var name each config field is written as in the secrets file.
FIELD_TO_ENV: dict[str, str] = {field: key for key, field in ENV_TO_FIELD.items()}


@dataclass(frozen=True)
class ResolvedRuntimeSettings:
    config: Config
    sources: dict[str, str]
    secret_backend: str
    legacy_sources: dict[str, str]
    runtime_config_path: Path
    # Where the shared shell secrets file lives and whether it could be read.
    env_file: Path | None = None
    env_file_readable: bool = False
    env_file_detail: str | None = None


def _resolved_config_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    config_path = _default_config_dir() / "config.json"
    if config_path.exists():
        return config_path
    old_path = _old_config_path()
    if old_path.exists():
        return old_path
    return config_path


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data: Any = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _apply_env_mapping(
    source: dict[str, str],
    label: str,
    updates: dict[str, object],
    sources: dict[str, str],
) -> None:
    """Overlay one name->value mapping onto the resolved fields.

    ENV_LOOKUP lists canonical names before their aliases, so a field already
    filled from this same mapping is never overwritten by an alias of itself.
    """
    for env_key, field in ENV_LOOKUP.items():
        if sources.get(field) == label:
            continue
        value = source.get(env_key)
        if value:
            updates[field] = value
            sources[field] = label


def _collect_legacy_config_secrets(path: Path | str | None = None) -> dict[str, str]:
    config_path = _resolved_config_path(path)
    candidates: list[Path] = [config_path]
    old_path = _old_config_path()
    if old_path not in candidates:
        candidates.append(old_path)

    found: dict[str, str] = {}
    for candidate in candidates:
        data = _read_json_if_exists(candidate)
        for field in SECRET_FIELDS:
            value = data.get(field)
            if value and field not in found:
                found[field] = str(value)
    return found


def resolve_runtime_settings(
    path: Path | str | None = None,
    *,
    overrides: dict[str, str | None] | None = None,
) -> ResolvedRuntimeSettings:
    base = Config.load(path)
    updates: dict[str, object] = {}
    sources: dict[str, str] = {}
    backend = describe_backend()
    legacy_sources = _collect_legacy_config_secrets(path)

    for field in SECRET_FIELDS:
        config_value = getattr(base, field, None)
        if config_value:
            sources[field] = "config"
            continue
        secret_value = get_secret(field)
        if secret_value:
            updates[field] = secret_value
            sources[field] = f"legacy-{backend.name}"

    # The shared shell secrets file (~/.secrets.env) is the intended home for
    # every API key. It is read directly rather than inherited, because a
    # GUI-launched MCP client starts the server with a minimal environment
    # that never sourced the user's shell startup files.
    env_file_info = describe_env_file()
    _apply_env_mapping(load_env_file(), "secrets-env", updates, sources)

    # A real process environment variable is an explicit, deliberate override
    # and outranks the file.
    _apply_env_mapping(dict(os.environ), "env-override", updates, sources)

    for field, value in (overrides or {}).items():
        if value is not None:
            updates[field] = value
            sources[field] = "cli-override"

    resolved = replace(base, **updates)  # type: ignore[arg-type]

    # If values still came from the shared config, record that source explicitly.
    for field in ("zotero_user_id", "openalex_email"):
        value = getattr(resolved, field, None)
        if value and field not in sources:
            sources[field] = "config"

    return ResolvedRuntimeSettings(
        config=resolved,
        sources=sources,
        secret_backend=backend.name,
        legacy_sources=legacy_sources,
        runtime_config_path=_resolved_config_path(path),
        env_file=env_file_info.path,
        env_file_readable=env_file_info.readable,
        env_file_detail=env_file_info.detail,
    )


def resolve_runtime_config(
    path: Path | str | None = None,
    *,
    overrides: dict[str, str | None] | None = None,
) -> Config:
    return resolve_runtime_settings(path, overrides=overrides).config
