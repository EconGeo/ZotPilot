"""Read and write the shared shell secrets file (``~/.secrets.env``).

ZotPilot's MCP server is started by whichever client registered it, and a
GUI-launched client does not source ``~/.zshenv`` — the server process gets a
minimal environment with no user exports in it.  Relying on the process
environment to carry API keys therefore only works from a terminal.  This
module lets ZotPilot read the secrets file directly so a single ``export``
line is enough regardless of how the server was launched.

The file is parsed, never executed: only ``NAME=value`` and
``export NAME=value`` lines are recognised, and command substitution or
variable expansion in a value is left as literal text.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ENV_FILE_OVERRIDE = "ZOTPILOT_ENV_FILE"
DEFAULT_ENV_FILE = "~/.secrets.env"

_ASSIGNMENT = re.compile(
    r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$""",
)


@dataclass(frozen=True)
class EnvFileInfo:
    """What ``describe_env_file`` found on disk."""

    path: Path
    exists: bool
    readable: bool
    permissions_ok: bool
    detail: str | None = None


def env_file_path() -> Path:
    """Path to the shared secrets file, honouring ``ZOTPILOT_ENV_FILE``."""
    override = os.environ.get(ENV_FILE_OVERRIDE)
    if override:
        return Path(override).expanduser()
    return Path(DEFAULT_ENV_FILE).expanduser()


def _permissions_ok(path: Path) -> bool:
    """True when the file is not readable by group or other.

    Windows has no equivalent bit, so the check passes there and the
    permission story is left to NTFS ACLs.
    """
    if sys.platform == "win32":
        return True
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return not (mode & (stat.S_IRWXG | stat.S_IRWXO))


def _unquote(raw: str) -> str:
    """Strip one layer of shell quoting from an assignment's right-hand side."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == '"':
            # Only the escapes that survive inside double quotes in sh.
            for escaped, literal in (('\\"', '"'), ("\\\\", "\\"), ("\\$", "$"), ("\\`", "`")):
                inner = inner.replace(escaped, literal)
        return inner
    return value


def describe_env_file(path: Path | str | None = None) -> EnvFileInfo:
    """Report the state of the secrets file without reading its values."""
    target = Path(path).expanduser() if path is not None else env_file_path()
    if not target.exists():
        return EnvFileInfo(target, False, False, True, "file does not exist")
    if not target.is_file():
        return EnvFileInfo(target, True, False, False, "not a regular file")
    perms_ok = _permissions_ok(target)
    if not perms_ok:
        mode = stat.S_IMODE(target.stat().st_mode)
        return EnvFileInfo(
            target,
            True,
            False,
            False,
            f"{oct(mode)} is group/world readable; expected 0600 — run: chmod 600 {target}",
        )
    if not os.access(target, os.R_OK):
        return EnvFileInfo(target, True, False, True, "not readable by the current user")
    return EnvFileInfo(target, True, True, True, None)


def load_env_file(path: Path | str | None = None) -> dict[str, str]:
    """Return the assignments in the secrets file, or ``{}`` if unusable.

    A missing file is normal.  A file with loose permissions is refused
    rather than read, so a secret is never picked up from somewhere other
    users can also read it; ``describe_env_file`` reports why.
    """
    info = describe_env_file(path)
    if not info.readable:
        return {}
    try:
        text = info.path.read_text(encoding="utf-8")
    except OSError:
        return {}

    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        name, raw = match.group(1), match.group(2)
        values[name] = _unquote(raw)
    return values


def _shell_quote(value: str) -> str:
    """Render a value as a double-quoted shell word."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix="zotpilot_")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        if sys.platform != "win32":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError as exc:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise RuntimeError(f"Failed to write {path}: {exc}") from exc


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    if sys.platform != "win32":
        os.chmod(backup, 0o600)
    return backup


def set_env_secret(name: str, value: str, path: Path | str | None = None) -> Path:
    """Add or update one ``export NAME="value"`` line, leaving the rest intact.

    Comments, ordering and unrelated assignments are preserved.  An existing
    assignment is rewritten in place, keeping whichever form (``export`` or
    bare) the file already used.
    """
    target = Path(path).expanduser() if path is not None else env_file_path()
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []

    rendered_export = f'export {name}={_shell_quote(value)}'
    replaced = False
    updated: list[str] = []
    for line in lines:
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) == name and not replaced:
            keeps_export = line.lstrip().startswith("export ")
            updated.append(rendered_export if keeps_export else f"{name}={_shell_quote(value)}")
            replaced = True
            continue
        if match and match.group(1) == name:
            continue  # drop later duplicates of the same name
        updated.append(line)

    if not replaced:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append(rendered_export)

    _backup(target)
    _write_atomic(target, "\n".join(updated).rstrip("\n") + "\n")
    return target


def unset_env_secret(name: str, path: Path | str | None = None) -> bool:
    """Remove every assignment of ``name``. Returns True if anything was removed."""
    target = Path(path).expanduser() if path is not None else env_file_path()
    if not target.exists():
        return False
    lines = target.read_text(encoding="utf-8").splitlines()
    kept = [
        line for line in lines
        if not (_ASSIGNMENT.match(line) and _ASSIGNMENT.match(line).group(1) == name)  # type: ignore[union-attr]
    ]
    if len(kept) == len(lines):
        return False
    _backup(target)
    _write_atomic(target, "\n".join(kept).rstrip("\n") + "\n")
    return True
