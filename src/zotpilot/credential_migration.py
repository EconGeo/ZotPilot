"""Move credentials out of legacy homes and into the shared secrets file.

ZotPilot used to keep API keys in ``config.json``, in an OS keychain, and —
older still — inline in each MCP client's own config. All three are now
legacy: the one home for a key is the shared shell secrets file
(``~/.secrets.env``), which ZotPilot reads directly. ``migrate_secrets``
collects whatever it finds in the legacy locations, writes it there as an
``export`` line, and strips it from ``config.json``.

The process environment is deliberately NOT a migration source: an exported
variable is a runtime override, and capturing it would persist a value the
user only meant to apply to one invocation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _platforms
from .config import SECRET_FIELDS, Config, _default_config_dir, _old_config_path
from .runtime_settings import FIELD_TO_ENV
from .secret_store import get_secret, has_secret, set_secret
from .secrets_env import env_file_path, load_env_file, set_env_secret

_ENV_TO_SECRET_FIELD = {
    "GEMINI_API_KEY": "gemini_api_key",
    "DASHSCOPE_API_KEY": "dashscope_api_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "ZOTERO_API_KEY": "zotero_api_key",
    "S2_API_KEY": "semantic_scholar_api_key",
}

# Where a migration can put what it finds.
TARGET_ENV_FILE = "env-file"
TARGET_CONFIG = "config"
TARGET_SECRET_STORE = "secret-store"


@dataclass(frozen=True)
class MigrationResult:
    imported: dict[str, str] = field(default_factory=dict)
    preserved: list[str] = field(default_factory=list)
    config_updated: bool = False
    re_registered_platforms: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    target: str = TARGET_ENV_FILE
    env_file: Path | None = None
    removed_from_config: list[str] = field(default_factory=list)


def _config_path(path: Path | str | None = None) -> Path:
    return Path(path).expanduser() if path is not None else (_default_config_dir() / "config.json")


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _legacy_config_candidates(config_path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in (config_path, _old_config_path()):
        data = _read_json_if_exists(path)
        for config_field in SECRET_FIELDS + ("zotero_user_id",):
            value = data.get(config_field)
            if value and config_field not in found:
                found[config_field] = str(value)
    return found


def _client_candidates() -> tuple[dict[str, str], list[str]]:
    found: dict[str, str] = {}
    touched_platforms: list[str] = []
    for plat in _platforms.SUPPORTED_PLATFORM_NAMES:
        registered, _command, _args, env, config_path = _platforms._inspect_registration(plat)  # noqa: SLF001
        if not registered or not env:
            continue
        touched_platforms.append(config_path or plat)
        for env_key, config_field in _ENV_TO_SECRET_FIELD.items():
            value = env.get(env_key)
            if value and config_field not in found:
                found[config_field] = str(value)
        user_id = env.get("ZOTERO_USER_ID")
        if user_id and "zotero_user_id" not in found:
            found["zotero_user_id"] = str(user_id)
    return found, touched_platforms


def _strip_secrets_from_config(config_path: Path) -> list[str]:
    """Remove every credential key from config.json, leaving the rest as-is.

    Rewrites the raw JSON rather than round-tripping through ``Config`` so
    that unrelated keys keep their exact on-disk values.
    """
    data = _read_json_if_exists(config_path)
    removed = [key for key in SECRET_FIELDS if key in data]
    if not removed:
        return []
    for key in removed:
        data.pop(key, None)
    from .cli import _write_config_data

    _write_config_data(config_path, data)
    return removed


def _resolve_target(target: str | None, to_config: bool | None) -> str:
    """Map the modern ``target`` and the legacy ``to_config`` flag onto one value."""
    if target is not None:
        if target not in (TARGET_ENV_FILE, TARGET_CONFIG, TARGET_SECRET_STORE):
            raise ValueError(
                f"Unknown migration target {target!r}. Expected one of "
                f"{TARGET_ENV_FILE!r}, {TARGET_CONFIG!r}, {TARGET_SECRET_STORE!r}."
            )
        return target
    if to_config is True:
        return TARGET_CONFIG
    if to_config is False:
        return TARGET_SECRET_STORE
    return TARGET_ENV_FILE


def migrate_secrets(
    *,
    config_path: Path | str | None = None,
    force: bool = False,
    target: str | None = None,
    env_file: Path | str | None = None,
    to_config: bool | None = None,
) -> MigrationResult:
    target_path = _config_path(config_path)
    resolved_target = _resolve_target(target, to_config)
    config = Config.load(target_path)
    config_candidates = _legacy_config_candidates(target_path)
    client_candidates, touched_platforms = _client_candidates()

    imported: dict[str, str] = {}
    preserved: list[str] = []
    removed_from_config: list[str] = []
    secrets_file = Path(env_file).expanduser() if env_file is not None else env_file_path()

    def _candidate(config_field: str) -> tuple[str | None, str]:
        """Best value for a field, plus where it was found."""
        legacy = get_secret(config_field)
        if legacy:
            return legacy, "legacy-secret-backend"
        if config_field in client_candidates:
            return client_candidates[config_field], "client-config"
        if config_field in config_candidates:
            return config_candidates[config_field], "config-file"
        return None, ""

    if resolved_target == TARGET_ENV_FILE:
        existing = load_env_file(secrets_file)
        for config_field in _ENV_TO_SECRET_FIELD.values():
            value, source = _candidate(config_field)
            if not value:
                continue
            env_name = FIELD_TO_ENV[config_field]
            if existing.get(env_name) and not force:
                preserved.append(config_field)
                continue
            set_env_secret(env_name, value, secrets_file)
            imported[config_field] = source
        removed_from_config = _strip_secrets_from_config(target_path)

    elif resolved_target == TARGET_CONFIG:
        for config_field in _ENV_TO_SECRET_FIELD.values():
            value, source = _candidate(config_field)
            if not value:
                continue
            if getattr(config, config_field, None) and not force:
                preserved.append(config_field)
                continue
            setattr(config, config_field, value)
            imported[config_field] = source

    else:  # TARGET_SECRET_STORE
        for config_field in _ENV_TO_SECRET_FIELD.values():
            value = (
                client_candidates.get(config_field)
                or config_candidates.get(config_field)
            )
            if not value:
                continue
            if has_secret(config_field) and not force:
                preserved.append(config_field)
                continue
            set_secret(config_field, value)
            imported[config_field] = (
                "client-config" if config_field in client_candidates else "config-file"
            )

    config_updated = False
    user_id_candidate = (
        client_candidates.get("zotero_user_id")
        or config_candidates.get("zotero_user_id")
    )
    if user_id_candidate and (force or not config.zotero_user_id):
        # zotero_user_id is an account number, not a credential, so it stays in
        # the shared config rather than moving to the secrets file.
        config.zotero_user_id = user_id_candidate
        config.save(target_path)
        config_updated = True

    if resolved_target == TARGET_CONFIG and imported:
        # Config.save() no longer persists credentials, so the legacy target
        # writes them back as raw keys. This must come after the save above,
        # which would otherwise drop them again.
        from .cli import _write_config_data

        data = _read_json_if_exists(target_path)
        for config_field in imported:
            data[config_field] = getattr(config, config_field)
        _write_config_data(target_path, data)

    re_registered_platforms: list[str] = []
    backups: list[str] = list(touched_platforms)
    if imported or config_updated or touched_platforms:
        result = _platforms.reconcile_runtime(apply=True)
        re_registered_platforms = list(result.applied.registered if result.applied else ())

    return MigrationResult(
        imported=imported,
        preserved=preserved,
        config_updated=config_updated,
        re_registered_platforms=re_registered_platforms,
        backups=backups,
        target=resolved_target,
        env_file=secrets_file if resolved_target == TARGET_ENV_FILE else None,
        removed_from_config=removed_from_config,
    )
