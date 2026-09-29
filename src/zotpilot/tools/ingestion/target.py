"""Which Zotero library an ingest writes into.

The browser connector saves into whatever library is selected in Zotero
Desktop, while every lookup after the save (local API, SQLite, Web API)
addresses a library explicitly. An ingest therefore names one target library
and every step — dedup, save verification, PDF check, INBOX routing, the DOI
API fallback — uses it.
"""
from __future__ import annotations

from dataclasses import dataclass

from ...state import ToolError
from ...zotero_client import ZoteroClient

_PERSONAL_ALIASES = {"", "user", "my library", "personal", "me"}


@dataclass(frozen=True)
class IngestTarget:
    local_library_id: int
    """SQLite ``libraryID`` (My Library is 1)."""
    library_type: str
    """``"user"`` or ``"group"``."""
    remote_id: str | None
    """Web API library id: the Zotero user ID or the group ID."""
    name: str

    @property
    def api_prefix(self) -> str:
        """Path segment for the Zotero local API (``/api/<prefix>/items``)."""
        if self.library_type == "group":
            return f"groups/{self.remote_id}"
        return "users/0"

    @property
    def is_personal(self) -> bool:
        return self.library_type == "user"


def resolve_ingest_target(zotero, library: str | None, *, user_id: str | None) -> IngestTarget:
    """Resolve a user-facing library name or group ID to an ``IngestTarget``.

    ``None``, ``"user"`` and ``"My Library"`` mean the personal library. Any
    other value must match a group's name (case-insensitive) or its group ID.
    """
    wanted = (library or "").strip()
    if wanted.lower() in _PERSONAL_ALIASES:
        return IngestTarget(1, "user", user_id, "My Library")

    groups = [lib for lib in zotero.get_libraries() if lib.get("library_type") == "group"]
    match = next((g for g in groups if str(g["library_id"]) == wanted), None)
    if match is None:
        match = next((g for g in groups if g["name"].lower() == wanted.lower()), None)
    if match is None:
        choices = ", ".join(["My Library", *(f"{g['name']} ({g['library_id']})" for g in groups)])
        raise ToolError(f"Unknown Zotero library '{library}'. Available: {choices}.")

    group_id = int(match["library_id"])
    local_id = ZoteroClient.resolve_group_library_id(zotero.data_dir, group_id)
    return IngestTarget(local_id, "group", str(group_id), match["name"])
