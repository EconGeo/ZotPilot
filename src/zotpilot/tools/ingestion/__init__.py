"""Ingestion MCP tools: search_academic_databases + ingest_by_identifiers."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import httpx
from pydantic import BeforeValidator, Field

from ...bridge import DEFAULT_PORT, BridgeServer
from ...state import (
    ToolError,
    _get_config,
    _get_writer,
    _get_zotero,
    mcp,
    register_reset_callback,
)
from ..profiles import tool_tags
from . import connector, search
from .models import IngestCandidate
from .target import IngestTarget, resolve_ingest_target

logger = logging.getLogger(__name__)
_writer_lock = threading.Lock()


def _parse_json_string_list(value: Any) -> Any:
    """Accept list params even when an MCP client wraps them as JSON strings.

    Some MCP client wrappers (e.g. Qwen-based 'Sisyphus' runtimes, older
    Claude Code builds) serialize list[T] params as JSON strings instead
    of real arrays before they reach the server. Decode transparently so
    Pydantic can validate the inner structure.

    Passes through lists, None, and other non-string values unchanged.
    On malformed JSON, returns the original value so Pydantic surfaces
    a type error the caller can act on instead of silently swallowing.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
        if isinstance(parsed, list):
            return parsed
    return value

# ---------------------------------------------------------------------------
# Target library: resolution, per-library clients and writers
# ---------------------------------------------------------------------------
_PERSONAL_TARGET = IngestTarget(1, "user", None, "My Library")
_group_writers: dict[str, Any] = {}
_group_clients: dict[int, Any] = {}


def _clear_target_caches() -> None:
    _group_writers.clear()
    _group_clients.clear()


register_reset_callback(_clear_target_caches)


def _resolve_target(library: str | None) -> IngestTarget:
    if not (library or "").strip():
        return _PERSONAL_TARGET
    return resolve_ingest_target(_get_zotero(), library, user_id=_get_config().zotero_user_id)


def _zotero_for_target(target: IngestTarget):
    """SQLite client scoped to the target library."""
    if target.is_personal:
        return _get_zotero()
    client = _group_clients.get(target.local_library_id)
    if client is None:
        from ...zotero_client import ZoteroClient

        client = ZoteroClient(_get_config().zotero_data_dir, library_id=target.local_library_id)
        _group_clients[target.local_library_id] = client
    return client


def _writer_for_target(target: IngestTarget):
    """Web API writer for the target library."""
    if target.is_personal:
        return _get_writer()
    writer = _group_writers.get(str(target.remote_id))
    if writer is None:
        _get_writer()  # same credential checks as the personal writer
        from ...zotero_writer import ZoteroWriter

        writer = ZoteroWriter(_get_config().zotero_api_key, str(target.remote_id), "group")
        _group_writers[str(target.remote_id)] = writer
    return writer


def _find_recent_saves_in(
    zotero, library_names: dict[int, str], *, dois: list[str], arxiv_id: str | None, since: datetime,
) -> list[dict]:
    """Items with these identifiers added to any library at or after ``since``, newest first.

    Rows are ``{"key", "in_target", "library"}``, ``in_target`` meaning the
    item is in ``zotero``'s library.
    """
    rows: list[dict] = []
    for doi in dict.fromkeys(d for d in dois if d):
        rows.extend(zotero.find_items_by_doi(doi, all_libraries=True))
    if arxiv_id:
        rows.extend(zotero.find_items_by_arxiv_id(arxiv_id, all_libraries=True))
    found: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r["date_added"] or "", reverse=True):
        try:
            added = datetime.strptime(row["date_added"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if added < since or row["key"] in found:
            continue
        found[row["key"]] = {
            "key": row["key"],
            "in_target": row["library_id"] == zotero.library_id,
            "library": library_names.get(row["library_id"], f"library {row['library_id']}"),
        }
    return list(found.values())


# ---------------------------------------------------------------------------
# INBOX collection cache (one INBOX per library)
# ---------------------------------------------------------------------------
_inbox_collection_keys: dict[str, str] = {}
_inbox_lock = threading.Lock()
_INBOX_COLLECTION_NAME = "INBOX"


def _clear_inbox_cache() -> None:
    _inbox_collection_keys.clear()


register_reset_callback(_clear_inbox_cache)


def _ensure_inbox_collection(target: IngestTarget | None = None) -> str | None:
    """Return the target library's INBOX collection key, creating it if absent when possible."""
    target = target or _PERSONAL_TARGET
    cache_key = "user" if target.is_personal else f"group:{target.remote_id}"
    if cache_key in _inbox_collection_keys:
        return _inbox_collection_keys[cache_key]
    with _inbox_lock:
        if cache_key in _inbox_collection_keys:
            return _inbox_collection_keys[cache_key]
        try:
            writer = _writer_for_target(target)
        except Exception:
            return None
        if not _get_config().zotero_api_key:
            return None

        def _remember(key: str | None) -> str | None:
            if key:
                _inbox_collection_keys[cache_key] = key
            return key

        try:
            with _writer_lock:
                collections = writer._zot.collections()
            for coll in collections:
                data = coll.get("data", {})
                if data.get("name") == _INBOX_COLLECTION_NAME:
                    return _remember(data.get("key") or coll.get("key"))
            with _writer_lock:
                response = writer._zot.create_collections([{"name": _INBOX_COLLECTION_NAME}])
            if response and "successful" in response:
                for value in response["successful"].values():
                    key = _remember(value.get("key") or value.get("data", {}).get("key"))
                    if key:
                        return key
            with _writer_lock:
                collections = writer._zot.collections()
            for coll in collections:
                data = coll.get("data", {})
                if data.get("name") == _INBOX_COLLECTION_NAME:
                    return _remember(data.get("key") or coll.get("key"))
        except Exception as exc:
            logger.warning("_ensure_inbox_collection failed: %s", exc)
    return None


_RECENT_SAVES_TTL_S = 900.0  # 15 min
_RECENT_SAVES: dict[tuple[Any, str], tuple[str, float]] = {}
_PREFLIGHT_PASS_TTL_S = 900.0  # 15 min
_PREFLIGHT_PASSES: dict[str, float] = {}


def _recent_save_key(zotero, key: str) -> tuple[Any, str]:
    return (getattr(zotero, "library_id", 1), key.lower().strip())


def _remember_recent_save(key: str | None, item_key: str | None, zotero=None) -> None:
    """Remember a just-saved (DOI or arXiv) → item_key for short-term dedup.

    Covers the moment between a Connector save and the item becoming visible
    to the SQLite dedup lookup, so an agent-side retry of the same ingest does
    not create a duplicate item. Keyed by library: a save into one library is
    not a duplicate of the same paper in another.
    """
    if not (key and item_key):
        return
    zotero = zotero if zotero is not None else _get_zotero()
    _RECENT_SAVES[_recent_save_key(zotero, key)] = (item_key, time.monotonic())


def _lookup_recent_save(key: str | None, zotero) -> str | None:
    """A remembered save, if it is recent and the item still exists."""
    if not key:
        return None
    cache_key = _recent_save_key(zotero, key)
    entry = _RECENT_SAVES.get(cache_key)
    if not entry:
        return None
    item_key, saved_at = entry
    if time.monotonic() - saved_at > _RECENT_SAVES_TTL_S:
        _RECENT_SAVES.pop(cache_key, None)
        return None
    try:
        exists = bool(zotero.item_key_exists(item_key))
    except Exception:
        exists = True  # cannot check; the cache is the only guard left
    if not exists:
        # Deleted (or moved) since it was saved — never report it as a duplicate
        _RECENT_SAVES.pop(cache_key, None)
        return None
    return item_key


def _remember_preflight_pass(url: str | None) -> None:
    if not url:
        return
    _PREFLIGHT_PASSES[url] = time.monotonic()


def _has_recent_preflight_pass(url: str | None) -> bool:
    if not url:
        return False
    saved_at = _PREFLIGHT_PASSES.get(url)
    if saved_at is None:
        return False
    if time.monotonic() - saved_at > _PREFLIGHT_PASS_TTL_S:
        _PREFLIGHT_PASSES.pop(url, None)
        return False
    return True


def _lookup_local_item_key_by_doi(doi: str | None, zotero=None) -> str | None:
    """Check if a DOI already exists in a local Zotero library (default: personal).

    Falls back to the in-process recent-saves cache for a save that SQLite
    does not show yet (see _remember_recent_save).
    """
    if not doi:
        return None
    try:
        zotero = zotero if zotero is not None else _get_zotero()
        found = zotero.get_item_key_by_doi(doi)
        if found:
            return found
    except Exception:
        if zotero is None:
            return None
    return _lookup_recent_save(doi, zotero)


def _lookup_local_item_key_by_arxiv_extra(arxiv_id: str | None, zotero=None) -> str | None:
    """Check whether an arXiv ID appears in a local Zotero item's extra field.

    Falls back to the in-process recent-saves cache (see _lookup_recent_save)
    to cover the window between Connector save and Zotero SQLite commit.
    """
    if not arxiv_id:
        return None
    try:
        zotero = zotero if zotero is not None else _get_zotero()
        found = zotero.get_item_key_by_arxiv_id(arxiv_id)
        if found:
            return found
    except Exception:
        if zotero is None:
            return None
    return _lookup_recent_save(arxiv_id, zotero)


def _normalize_arxiv_id(arxiv_id: str | None) -> str | None:
    """Normalize an arXiv ID by trimming prefixes and version suffixes."""
    if not arxiv_id:
        return None
    cleaned = arxiv_id.strip()
    if cleaned.lower().startswith("arxiv:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    cleaned = re.sub(r"v\d+$", "", cleaned, flags=re.IGNORECASE)
    return cleaned or None


def _arxiv_doi(arxiv_id: str) -> str:
    return f"10.48550/arxiv.{arxiv_id}"


def _doi_to_landing_url(doi: str) -> str:
    return f"https://doi.org/{doi}"


def _candidates_to_internal(input_candidates: list[IngestCandidate]) -> list[dict]:
    """Convert structured candidates into the internal ingestion representation."""
    internal: list[dict] = []
    for index, candidate in enumerate(input_candidates):
        source_doi = search.normalize_doi(candidate.doi)
        arxiv_id = _normalize_arxiv_id(candidate.arxiv_id)
        entry: dict = {
            "identifier": source_doi or arxiv_id or candidate.landing_page_url or "",
            "title": candidate.title,
            "arxiv_id": arxiv_id,
            "source_doi": source_doi,
            "landing_page_url": candidate.landing_page_url,
            "publisher": candidate.publisher,
            "needs_manual_verification": candidate.needs_manual_verification,
            "existing_item_key": candidate.existing_item_key,
            "resume_action": candidate.resume_action,
            "_index": index,
            "url": None,
            "doi": None,
        }

        doi_is_arxiv = bool(source_doi and source_doi.startswith("10.48550/arxiv."))
        if candidate.is_oa_published and source_doi and not doi_is_arxiv:
            entry["doi"] = source_doi
            entry["url"] = _doi_to_landing_url(source_doi)
        elif arxiv_id:
            entry["doi"] = _arxiv_doi(arxiv_id)
            entry["url"] = f"https://arxiv.org/abs/{arxiv_id}"
        elif source_doi:
            entry["doi"] = source_doi
            entry["url"] = _doi_to_landing_url(source_doi)
        elif candidate.landing_page_url:
            entry["url"] = candidate.landing_page_url
        else:
            entry["status"] = "failed"
            entry["error"] = "no_usable_identifier"

        internal.append(entry)
    return internal


def _identifiers_to_internal(identifiers: list[str]) -> list[dict]:
    """Convert deprecated identifier strings into the internal ingestion representation."""
    internal: list[dict] = []
    for index, raw_identifier in enumerate(identifiers):
        identifier = raw_identifier.strip()
        arxiv_id: str | None = None
        source_doi: str | None = None
        candidate: dict = {
            "identifier": identifier,
            "title": None,
            "landing_page_url": None,
            "publisher": None,
            "needs_manual_verification": None,
            "existing_item_key": None,
            "resume_action": None,
            "url": None,
            "doi": None,
            "arxiv_id": None,
            "source_doi": None,
            "_index": index,
        }

        source_doi = search.normalize_doi(identifier)
        if source_doi:
            candidate["source_doi"] = source_doi
            candidate["doi"] = source_doi
            candidate["url"] = connector.resolve_doi_to_landing_url(source_doi)
            if source_doi.startswith("10.48550/arxiv."):
                arxiv_id = _normalize_arxiv_id(source_doi[len("10.48550/arxiv."):])
                candidate["arxiv_id"] = arxiv_id
        elif identifier.lower().startswith("arxiv:") or _looks_like_arxiv_id(identifier):
            arxiv_id = _normalize_arxiv_id(identifier)
            candidate["arxiv_id"] = arxiv_id
            if arxiv_id:
                candidate["source_doi"] = _arxiv_doi(arxiv_id)
                candidate["doi"] = _arxiv_doi(arxiv_id)
                candidate["url"] = f"https://arxiv.org/abs/{arxiv_id}"
        elif identifier.startswith(("http://", "https://")):
            candidate["url"] = identifier
            doi_from_url = search.is_doi_query(identifier)
            if doi_from_url:
                source_doi = search.normalize_doi(doi_from_url)
                candidate["source_doi"] = source_doi
                candidate["doi"] = source_doi
                if source_doi and source_doi.startswith("10.48550/arxiv."):
                    candidate["arxiv_id"] = _normalize_arxiv_id(
                        source_doi[len("10.48550/arxiv."):]
                    )
        else:
            candidate["status"] = "failed"
            candidate["error"] = "unrecognized_identifier"

        internal.append(candidate)
    return internal


def _refresh_duplicate_pdf(candidate: dict, *, logger, target: IngestTarget | None = None) -> dict:
    """Return a result for a duplicate-detected candidate.

    If the existing item already has a PDF, report `has_pdf=True`. Otherwise
    try the OA fallback path (Unpaywall / arXiv) against the existing
    item_key so a retry after a partial earlier save has a chance to finish
    the PDF attachment. Falls back to the plain duplicate result if anything
    in the refresh path raises.
    """
    item_key = candidate.get("item_key")
    if not item_key:
        return _result_from_candidate(candidate)
    target = target or _PERSONAL_TARGET

    try:
        from ...state import _get_resolver
        from . import connector as _conn

        zotero = _zotero_for_target(target)
        pdf_status = _conn.check_pdf_status(
            item_key, get_writer=lambda: _writer_for_target(target), timeout_s=5.0,
            api_prefix=target.api_prefix, has_pdf_locally=zotero.item_has_pdf_attachment,
            _logger=logger,
        )
        has_pdf = pdf_status == "attached"
        if has_pdf:
            return _result_from_candidate(candidate, has_pdf=True)

        doi = candidate.get("doi")
        arxiv_id = candidate.get("arxiv_id")
        if not (doi or arxiv_id):
            return _result_from_candidate(candidate, warning="No PDF and no DOI/arxiv to fetch OA fallback.")

        resolve_id = f"arxiv:{arxiv_id}" if arxiv_id else doi
        metadata = _get_resolver().resolve(resolve_id)
        effective_arxiv = metadata.arxiv_id or arxiv_id
        with _writer_lock:
            attach_status = _writer_for_target(target).try_attach_oa_pdf(
                item_key,
                doi=metadata.doi or doi,
                oa_url=metadata.oa_url,
                arxiv_id=effective_arxiv,
            )
        if attach_status == "attached":
            logger.info("Duplicate refresh attached PDF to %s", item_key)
            return _result_from_candidate(candidate, has_pdf=True)
        if attach_status == "quota_exceeded":
            return _result_from_candidate(
                candidate, has_pdf=False,
                warning=(
                    "Duplicate found with no PDF; Zotero Web quota is full so "
                    "the OA fallback aborted. Free cloud quota or right-click "
                    "the item in Zotero Desktop → 'Find Available PDF'."
                ),
            )
        return _result_from_candidate(
            candidate, has_pdf=False,
            warning="Duplicate found with no PDF; OA fallback did not attach one.",
        )
    except Exception as exc:
        logger.debug("Duplicate PDF refresh failed for %s: %s", item_key, exc)
        return _result_from_candidate(candidate)


def _result_from_candidate(
    candidate: dict,
    *,
    status: str | None = None,
    error: str | None = None,
    item_key: str | None = None,
    has_pdf: bool = False,
    title: str | None = None,
    action_required=None,
    warning: str | None = None,
    **extra,
) -> dict:
    """Format a public result row from an internal candidate + save result fields."""
    return {
        "identifier": candidate.get("identifier", ""),
        "candidate_index": candidate.get("_index"),
        "status": status or candidate.get("status", "failed"),
        "item_key": item_key if item_key is not None else candidate.get("item_key"),
        "has_pdf": has_pdf,
        "title": title if title is not None else candidate.get("title", "") or "",
        "error": error if error is not None else candidate.get("error"),
        "action_required": action_required,
        "warning": warning,
        **extra,
    }


def _lookup_existing_item_key(candidate: dict, zotero=None) -> str | None:
    doi = candidate.get("source_doi") or candidate.get("doi")
    item_key = _lookup_local_item_key_by_doi(doi, zotero=zotero)
    if item_key:
        return item_key
    arxiv_id = candidate.get("arxiv_id")
    if arxiv_id:
        return _lookup_local_item_key_by_arxiv_extra(arxiv_id, zotero=zotero)
    return None


def _build_manual_completion_action(
    *,
    pending_candidate: dict,
    current_result: dict | None,
    retry_payload: list[dict],
    completed_count: int,
    completed_indexes: list[int],
    message: str,
) -> dict:
    item_key = None
    if current_result is not None:
        item_key = current_result.get("item_key")
    item_key = item_key or pending_candidate.get("existing_item_key")
    return {
        "type": "manual_completion_required",
        "pending_candidate": _candidate_retry_payload(
            pending_candidate,
            resume_action=(current_result or {}).get("resume_action"),
            existing_item_key=item_key,
        ),
        "retry_payload": retry_payload,
        "timeout_stage": (current_result or {}).get("timeout_stage", "manual_queue"),
        "completed_count": completed_count,
        "completed_indexes": completed_indexes,
        "item_key": item_key,
        "message": message,
    }


# ---------------------------------------------------------------------------
# MCP Tool: search_academic_databases
# ---------------------------------------------------------------------------

@mcp.tool(tags=tool_tags("core", "ingestion"))
def search_academic_databases(
    query: Annotated[
        str,
        Field(description=(
            "Search query. MUST be structured (not fuzzy bag-of-words) UNLESS "
            "you also pass concepts/venue/institutions. Allowed forms: DOI "
            "('10.xxx/yyy'), author-anchored ('author:Radford CLIP'), "
            "quoted phrase ('\"visual instruction tuning\"'), boolean "
            "('\"LLaVA\" OR \"Flamingo\"'). Pass '' when filtering purely by "
            "concept/venue."
        )),
    ],
    limit: Annotated[int, Field(ge=1, le=100, description="Max results per page")] = 20,
    year_min: Annotated[int | None, Field(description="Minimum publication year")] = None,
    year_max: Annotated[int | None, Field(description="Maximum publication year")] = None,
    min_citations: Annotated[
        int | None,
        Field(description="Minimum citation count (use to cut long tail)"),
    ] = None,
    sort_by: Annotated[
        Literal["relevance", "citations", "date"],
        Field(description="Sort order"),
    ] = "relevance",
    oa_only: Annotated[
        bool,
        Field(description="Restrict to open access papers"),
    ] = False,
    concepts: Annotated[
        list[str] | None,
        BeforeValidator(_parse_json_string_list),
        Field(description=(
            "Concept/topic names (human-readable, resolved server-side). "
            "Examples: ['Computer vision', 'Natural language processing']. "
            "Use to anchor topic searches and escape bag-of-words rejection."
        )),
    ] = None,
    venue: Annotated[
        str | None,
        Field(description=(
            "Venue/journal/conference name (resolved server-side). "
            "Examples: 'CVPR', 'NeurIPS', 'IEEE TPAMI', 'ICLR'."
        )),
    ] = None,
    institutions: Annotated[
        list[str] | None,
        BeforeValidator(_parse_json_string_list),
        Field(description=(
            "Institution names (resolved server-side). "
            "Examples: ['Stanford University', 'MIT', 'Google DeepMind']."
        )),
    ] = None,
    cursor: Annotated[
        str | None,
        Field(description="Pagination cursor from previous call's next_cursor"),
    ] = None,
) -> dict:
    """Search OpenAlex with full filter suite (concepts/venue/institutions).

    Returns: {"results": [...], "next_cursor": str|None, "total_count": int,
    "unresolved_filters": [...]}. Fuzzy queries are rejected unless a
    structured filter narrows the space. Use concepts+venue+year_min for
    precise topic discovery; use DOI or quoted phrase for known papers.
    """
    # MCP client compatibility: some clients wrap list[str] params as JSON
    # strings. Coerce defensively — mirrors the same pattern used by
    # ingest_by_identifiers. See _parse_json_string_list for rationale.
    concepts = _parse_json_string_list(concepts)
    institutions = _parse_json_string_list(institutions)
    if isinstance(concepts, str):
        raise ToolError(
            "`concepts` must be a list of concept names, e.g. "
            '["Computer vision", "Natural language processing"].'
        )
    if isinstance(institutions, str):
        raise ToolError(
            "`institutions` must be a list of institution names, e.g. "
            '["Stanford University", "Google DeepMind"].'
        )

    config = _get_config()
    sort_map = {"relevance": "relevance", "citations": "citationCount", "date": "publicationDate"}
    return search.search_academic_databases_impl(
        config, query, limit=limit,
        year_min=year_min, year_max=year_max,
        sort_by=sort_map.get(sort_by, "relevance"),
        httpx_module=httpx, tool_error_cls=ToolError, logger=logger,
        min_citations=min_citations,
        oa_only=oa_only,
        concepts=concepts,
        institutions=institutions,
        venue=venue,
        cursor=cursor,
        lookup_by_doi=_lookup_local_item_key_by_doi,
        lookup_by_arxiv_extra=_lookup_local_item_key_by_arxiv_extra,
    )


# ---------------------------------------------------------------------------
# MCP Tool: ingest_by_identifiers
# ---------------------------------------------------------------------------

@mcp.tool(tags=tool_tags("core", "ingestion"))
def ingest_by_identifiers(
    candidates: Annotated[
        list[IngestCandidate] | None,
        BeforeValidator(_parse_json_string_list),
        Field(description=(
            "**Preferred path.** Structured candidates from "
            "search_academic_databases. Pass each selected search result dict "
            "directly. Extra fields are ignored. Do not reconstruct identifier "
            "strings from memory."
        )),
    ] = None,
    identifiers: Annotated[
        list[str] | None,
        BeforeValidator(_parse_json_string_list),
        Field(description=(
            "_Deprecated._ Direct DOI/arXiv/URL string list kept only for "
            "backward compatibility. Use candidates= instead."
        )),
    ] = None,
    library: Annotated[
        str | None,
        Field(description=(
            "Zotero library to save into: omit (or 'user') for My Library, or a "
            "group library's name or group ID. The browser connector saves into "
            "the library selected in Zotero Desktop, so that library (or a "
            "collection in it) must be selected there; otherwise nothing is "
            "saved and action_required says what to select."
        )),
    ] = None,
) -> dict:
    """Ingest papers into a Zotero library's INBOX collection. Per-paper status, synchronous.

    Destination and tagging are **not** caller-controlled beyond `library`:
      - All new items land in that library's INBOX collection (auto-created on first use).
      - Tags are NEVER applied at save time. Topic tagging and reclassification
        happen in Phase 3 via `manage_tags` / `manage_collections` through the
        plan-then-execute workflow in ztp-research — this prevents drive-by
        tagging that bypasses user vocabulary review.

    Internal flow: normalize → dedup → connector check → preflight →
    sequential save+verify → API fallback on failure → PDF check.

    Statuses: saved_with_pdf, saved_metadata_only, saved_unconfirmed, blocked,
    duplicate, failed. saved_unconfirmed means the browser may have saved the
    paper but ZotPilot could not identify the item — check Zotero, never
    re-ingest it. Results are in input order (candidate_index).
    When action_required is non-empty, surface to user and wait.
    """
    # MCP client compatibility: some clients (Qwen-based 'Sisyphus' runtimes,
    # older Claude Code builds) wrap list[T] params as JSON strings on the
    # wire. The signature has a BeforeValidator that catches this during
    # FastMCP dispatch, but we also coerce defensively here so direct Python
    # calls (tests, in-process callers) get the same behavior and so the
    # string → list[IngestCandidate] conversion is explicit at the body level.
    candidates = _parse_json_string_list(candidates)
    identifiers = _parse_json_string_list(identifiers)
    if isinstance(candidates, str):
        raise ToolError(
            "`candidates` could not be parsed as a JSON list of candidate "
            "dicts. Pass the search result object directly without "
            "modification."
        )
    if isinstance(identifiers, str):
        raise ToolError(
            "`identifiers` could not be parsed as a JSON list of strings. "
            'Expected form: ["10.1234/abc", "2301.00001"].'
        )
    if isinstance(candidates, list):
        candidates = [
            c if isinstance(c, IngestCandidate) else IngestCandidate.model_validate(c)
            for c in candidates
        ]

    # Empty lists count as "absent": calling ingest with zero candidates is
    # almost always an upstream filter bug (e.g. `local_duplicate` wiped the
    # selection). Fail loudly instead of silently returning total=0.
    has_candidates = bool(candidates)
    has_identifiers = bool(identifiers)
    if not has_candidates and not has_identifiers:
        raise ToolError(
            "ingest_by_identifiers requires at least one candidate. Pass "
            "`candidates` (preferred — structured dicts from "
            "search_academic_databases results) or `identifiers` "
            "(deprecated — raw DOI/URL strings). Empty or missing inputs "
            "are not a valid call — this usually means an upstream filter "
            "removed every selection. Pass the search result object "
            "directly without modification."
        )
    if has_candidates and has_identifiers:
        raise ToolError(
            "ingest_by_identifiers accepts only one of `candidates` or "
            "`identifiers`, not both. Prefer `candidates`."
        )

    bridge_url = f"http://127.0.0.1:{DEFAULT_PORT}"
    _get_zotero()
    target = _resolve_target(library)
    zotero = _zotero_for_target(target)

    def get_writer():
        return _writer_for_target(target)

    total_inputs = len(candidates) if has_candidates else len(identifiers or [])

    # Destination is the target library's INBOX. _ensure_inbox_collection
    # auto-creates it on first use; returns None only when ZOTERO_API_KEY is
    # missing or the writer init fails — then the tool cannot function at all.
    collection_key = _ensure_inbox_collection(target)
    if not collection_key:
        raise ToolError(
            "INBOX collection unavailable. ingest_by_identifiers requires "
            "ZOTERO_API_KEY and ZOTERO_USER_ID so it can create and route "
            "items into the INBOX collection. Configure credentials and retry."
        )

    # Step 1: Normalize inputs → internal candidates
    if candidates is not None:
        candidates_internal = _candidates_to_internal(candidates)
    else:
        logger.warning(
            "ingest_by_identifiers called with deprecated identifiers=<list[str]>. "
            "Prefer candidates=<list[dict]> forwarded from "
            "search_academic_databases results. The str branch will be removed "
            "in 0.6.0."
        )
        candidates_internal = _identifiers_to_internal(identifiers or [])

    # Step 2: Local dedup — check journal DOI, arXiv DOI variant, and extra field
    seen_batch_keys: dict[str, dict] = {}
    for candidate in candidates_internal:
        if candidate.get("status"):
            continue

        canonical_key = _canonical_candidate_key(candidate)
        if canonical_key in seen_batch_keys:
            candidate["status"] = "duplicate"
            candidate["item_key"] = seen_batch_keys[canonical_key].get("item_key")
            continue
        seen_batch_keys[canonical_key] = candidate

        doi_candidates: list[str] = []
        for value in (candidate.get("source_doi"), candidate.get("doi")):
            normalized = search.normalize_doi(value)
            if normalized and normalized not in doi_candidates:
                doi_candidates.append(normalized)

        arxiv_id = _normalize_arxiv_id(candidate.get("arxiv_id"))
        existing_key: str | None = None
        for doi_value in doi_candidates:
            existing_key = _lookup_local_item_key_by_doi(doi_value, zotero=zotero)
            if existing_key:
                break

        if not existing_key and arxiv_id:
            arxiv_doi_variant = _arxiv_doi(arxiv_id)
            if arxiv_doi_variant not in doi_candidates:
                existing_key = _lookup_local_item_key_by_doi(arxiv_doi_variant, zotero=zotero)

        if not existing_key and arxiv_id:
            existing_key = _lookup_local_item_key_by_arxiv_extra(arxiv_id, zotero=zotero)

        if existing_key:
            candidate["status"] = "duplicate"
            candidate["item_key"] = existing_key
            seen_batch_keys[canonical_key]["item_key"] = existing_key

    # Step 3: Check Connector availability
    active_candidates = [c for c in candidates_internal if not c.get("status") and c.get("url")]
    ext_ok = False
    if active_candidates:
        ext_ok, _ext_error, _ = connector.check_connector_availability(
            active_candidates, DEFAULT_PORT, BridgeServer,
        )

    # action_required declared early — needed by both Step 4 blocking and Step 5 anti-bot
    action_required: list[dict] = []

    # Step 3.5: The connector saves into the library selected in Zotero
    # Desktop. If that is not the target, saving would file the papers in the
    # wrong library, so stop and say what to select.
    if ext_ok and active_candidates:
        selected = connector.get_selected_zotero_library()
        if selected is None:
            logger.info("Could not read Zotero's selected library; post-save checks will catch misfiling")
        elif selected["libraryID"] != target.local_library_id or not selected["editable"]:
            selected_name = selected.get("libraryName") or f"library {selected['libraryID']}"
            for candidate in active_candidates:
                candidate["status"] = "blocked"
                candidate["error"] = "zotero_library_mismatch"
            action_required.append({
                "type": "select_zotero_library",
                "target_library": target.name,
                "selected_library": selected_name,
                "message": (
                    f"Zotero Desktop is set to save into '{selected_name}'"
                    + ("" if selected["editable"] else " (read-only)")
                    + f", but this ingest targets '{target.name}'. In Zotero, click the "
                    f"'{target.name}' library (or a collection inside it), then run the "
                    "ingest again. Nothing was saved."
                ),
            })
            active_candidates = []

    # Step 4: Preflight (if Connector online)
    if ext_ok and active_candidates:
        preflight_ready = [c for c in active_candidates if _has_recent_preflight_pass(c.get("url"))]
        preflight_targets = [c for c in active_candidates if not _has_recent_preflight_pass(c.get("url"))]

        remaining: list[dict] = list(preflight_ready)
        preflight_failures: list[dict] = []
        blocked_publishers: list[dict] = []
        blocking: dict | None = None

        if preflight_targets:
            remaining_fresh, preflight_failures, blocking, blocked_publishers = connector.run_preflight_check(
                preflight_targets, DEFAULT_PORT, BridgeServer, logger,
            )
            remaining.extend(remaining_fresh)
            for candidate in remaining_fresh:
                _remember_preflight_pass(candidate.get("url"))

        if blocking:
            failure_by_url: dict[str, dict] = {
                f["url"]: f for f in preflight_failures if f.get("url")
            }
            blocked_count = 0
            for candidate in candidates_internal:
                if candidate.get("status"):
                    continue
                failure = failure_by_url.get(candidate.get("url", ""))
                if failure is None:
                    continue
                candidate["status"] = "preflight_blocked"
                candidate["error"] = failure.get("error_code") or "preflight_failed"
                blocked_count += 1

            publisher_details = blocked_publishers or []
            publisher_names = [d.get("publisher", "") for d in publisher_details]
            if publisher_details:
                parts = [
                    f"{d['publisher']}（{d.get('error_code', 'preflight_failed')}）"
                    for d in publisher_details
                ]
                details_str = "、".join(parts)
            else:
                details_str = "、".join(publisher_names)

            urls_to_verify: list[str] = []
            for d in publisher_details:
                urls_to_verify.extend(d.get("sample_urls") or [])

            action_required.append({
                "type": "preflight_blocked",
                "publishers": publisher_names,
                "details": publisher_details,
                "urls_to_verify": urls_to_verify,
                "blocked_count": blocked_count,
                "message": (
                    f"{blocked_count} 篇需要你处理：{details_str}。\n"
                    "以下条目已通过预检，将继续入库；未通过的请在浏览器中打开这些链接确认页面可访问"
                    "（若有 Cloudflare / CAPTCHA / 登录，请完成它）：\n  - "
                    + "\n  - ".join(urls_to_verify)
                ),
            })

        remaining_ids = {id(c) for c in remaining}
        candidates_internal = [
            c for c in candidates_internal
            if c.get("status") or id(c) in remaining_ids
        ]



    manual_candidates = [
        c for c in candidates_internal
        if not c.get("status") and _classify_execution_group(c) == "manual_verification"
    ]
    access_candidates = [
        c for c in candidates_internal
        if not c.get("status") and _classify_execution_group(c) == "access_sensitive"
    ]
    normal_candidates = [
        c for c in candidates_internal
        if not c.get("status") and _classify_execution_group(c) == "normal"
    ]
    execution_plan = manual_candidates + access_candidates + normal_candidates

    # Step 5: Sequential save + verify
    results: list[dict] = []
    manual_completion: dict | None = None
    for candidate in candidates_internal:
        if candidate.get("status"):
            if candidate.get("status") == "duplicate":
                results.append(_refresh_duplicate_pdf(candidate, logger=logger, target=target))
            else:
                results.append(_result_from_candidate(candidate))

    library_names: dict[int, str] | None = None

    def find_recent_saves(*, dois: list[str], arxiv_id: str | None, since: datetime) -> list[dict]:
        nonlocal library_names
        if library_names is None:
            library_names = zotero.get_library_names()
        return _find_recent_saves_in(zotero, library_names, dois=dois, arxiv_id=arxiv_id, since=since)

    for position, candidate in enumerate(execution_plan):
        url = candidate.get("url")
        doi = candidate.get("doi")
        title = candidate.get("title")
        risk_class = _classify_execution_group(candidate)

        if ext_ok and url:
            result = connector.save_single_and_verify(
                url, doi, title,
                arxiv_id=candidate.get("arxiv_id"),
                collection_key=collection_key, tags=None,
                bridge_url=bridge_url, get_writer=get_writer,
                writer_lock=_writer_lock, _logger=logger,
                risk_class=risk_class,
                api_prefix=target.api_prefix,
                expected_dois=[candidate.get("source_doi")],
                find_recent_saves=find_recent_saves,
                has_pdf_locally=zotero.item_has_pdf_attachment,
            )
        elif doi:
            result = connector._doi_api_fallback(
                doi, title,
                arxiv_id=candidate.get("arxiv_id"),
                oa_url=None,
                collection_key=collection_key, tags=None,
                get_writer=get_writer, writer_lock=_writer_lock, _logger=logger,
                reason="browser connector was not connected",
            )
        else:
            result = {
                "status": "failed", "error": "no_usable_identifier",
                "item_key": None, "has_pdf": False, "title": title or "",
                "action_required": None, "warning": None,
            }

        if result.get("status") == "__manual_completion_required__":
            existing_item_key = result.get("item_key") or _lookup_existing_item_key(candidate, zotero)
            pending_candidate = dict(candidate)
            pending_candidate["existing_item_key"] = existing_item_key
            pending_candidate["item_key"] = existing_item_key
            deferred = execution_plan[position + 1:]
            retry_payload = sorted(
                [
                    _candidate_retry_payload(
                        pending_candidate,
                        resume_action=result.get("resume_action"),
                        existing_item_key=existing_item_key,
                    ),
                    *[
                        _candidate_retry_payload(
                            c,
                            resume_action=c.get("resume_action"),
                            existing_item_key=c.get("existing_item_key"),
                        )
                        for c in deferred
                    ],
                ],
                key=lambda row: row.get("candidate_index", 0),
            )
            completed_count = sum(
                1 for row in results if row["status"] in {"saved_with_pdf", "saved_metadata_only", "duplicate"}
            )
            completed_indexes = sorted(
                row["candidate_index"] for row in results if row.get("candidate_index") is not None
            )
            manual_completion = _build_manual_completion_action(
                pending_candidate=pending_candidate,
                current_result=result,
                retry_payload=retry_payload,
                completed_count=completed_count,
                completed_indexes=completed_indexes,
                message=(
                    "部分高风险出版社条目需要你先在 Zotero Desktop 中完成 translator 对话框 / PDF 挂载。"
                    "请检查 Zotero 是否已有条目，若仍有 Continue/确认弹窗请先点击，然后回复 Y 继续；不要整批重试。"
                ),
            )
            break

        row = {
            **result,
            "identifier": candidate.get("identifier", ""),
            "candidate_index": candidate.get("_index"),
        }
        results.append(row)

        if result.get("item_key") and result.get("status") not in {"failed", "blocked", "saved_unconfirmed"}:
            _remember_recent_save(candidate.get("doi"), result.get("item_key"), zotero=zotero)
            _remember_recent_save(candidate.get("arxiv_id"), result.get("item_key"), zotero=zotero)

        if result.get("status") == "saved_unconfirmed":
            action_required.append({
                "type": "save_unconfirmed",
                "message": result.get("warning", ""),
                "identifier": candidate.get("identifier", ""),
                "item_key": result.get("item_key"),
            })

        if result.get("status") == "blocked":
            action_required.append({
                "type": "anti_bot_detected",
                "message": result.get("action_required", ""),
                "identifier": candidate.get("identifier", ""),
            })

    if manual_completion is not None:
        action_required.append(manual_completion)

    # Saves run in risk order; report them in input order.
    results.sort(key=lambda row: (row.get("candidate_index") is None, row.get("candidate_index") or 0))

    return {
        "total": total_inputs,
        "library": target.name,
        "results": results,
        "action_required": action_required,
        "completed_count": sum(
            1 for row in results if row["status"] in {"saved_with_pdf", "saved_metadata_only", "duplicate"}
        ),
        "completed_indexes": sorted(
            row["candidate_index"] for row in results if row.get("candidate_index") is not None
        ),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$", re.IGNORECASE)
_ACCESS_SENSITIVE_PUBLISHER_KEYWORDS = (
    "ieee",
    "wiley",
    "springer",
)
_ACCESS_SENSITIVE_HOST_MARKERS = (
    "ieeexplore.ieee.org",
    "wiley.com",
    "onlinelibrary.wiley.com",
    "springer.com",
    "springerlink.com",
    "link.springer.com",
)
_MANUAL_VERIFICATION_HOST_MARKERS = (
    "sciencedirect.com",
    "linkinghub.elsevier.com",
    "elsevier.com",
)


def _looks_like_arxiv_id(s: str) -> bool:
    return bool(_ARXIV_ID_RE.match(s.strip()))


def _candidate_host(candidate: dict, *, prefer_landing: bool = False) -> str:
    source = candidate.get("landing_page_url") if prefer_landing else None
    if not source:
        source = candidate.get("landing_page_url") or candidate.get("url") or ""
    return connector.extract_publisher_domain(source)


def _classify_execution_group(candidate: dict) -> str:
    publisher = (candidate.get("publisher") or "").lower()
    landing_host = _candidate_host(candidate, prefer_landing=True)
    execution_host = _candidate_host(candidate)
    doi = (candidate.get("doi") or candidate.get("source_doi") or "").lower()
    manual_hint = candidate.get("needs_manual_verification")

    if manual_hint is True:
        return "manual_verification"
    if doi.startswith("10.1016/"):
        return "manual_verification"
    if any(marker in landing_host or marker in execution_host for marker in _MANUAL_VERIFICATION_HOST_MARKERS):
        return "manual_verification"
    if "elsevier" in publisher:
        return "manual_verification"

    if any(keyword in publisher for keyword in _ACCESS_SENSITIVE_PUBLISHER_KEYWORDS):
        return "access_sensitive"
    if any(marker in landing_host or marker in execution_host for marker in _ACCESS_SENSITIVE_HOST_MARKERS):
        return "access_sensitive"

    return "normal"


def _candidate_retry_payload(
    candidate: dict,
    *,
    resume_action: str | None = None,
    existing_item_key: str | None = None,
) -> dict:
    return {
        "candidate_index": candidate.get("_index"),
        "identifier": candidate.get("identifier"),
        "doi": candidate.get("source_doi") or candidate.get("doi"),
        "arxiv_id": candidate.get("arxiv_id"),
        "landing_page_url": candidate.get("landing_page_url"),
        "url": candidate.get("url"),
        "title": candidate.get("title"),
        "publisher": candidate.get("publisher"),
        "needs_manual_verification": candidate.get("needs_manual_verification"),
        "existing_item_key": existing_item_key,
        "resume_action": resume_action,
    }


def _canonical_candidate_key(candidate: dict) -> str:
    for key in (
        candidate.get("source_doi"),
        candidate.get("doi"),
        candidate.get("arxiv_id"),
        candidate.get("landing_page_url"),
        candidate.get("url"),
        candidate.get("identifier"),
    ):
        if key:
            return str(key).lower().strip()
    return f"candidate-index:{candidate.get('_index')}"
