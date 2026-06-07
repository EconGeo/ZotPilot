"""Helpers for reconciling Chroma index state with the current Zotero PDF library."""

import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Always-on safety floor: refuse orphan mass-deletion when the proposed deletion
# exceeds this fraction of the current index. The empty-read and unreachable-dir
# guards are unconditional and ignore this fraction entirely.
MASS_DELETE_FRACTION_FLOOR = 0.25

# Backstop for clearing a lease whose holder PID still appears alive (i.e. the
# PID was reused by an unrelated process after the real holder died). Set well
# beyond any realistic indexing run so a genuinely-running holder is never
# stolen. PID-death is detected immediately and independently of this value.
LEASE_STALE_SECONDS = 24 * 60 * 60  # 24 hours — longer than any realistic single
# run (a large first index with vision can take many hours); only catches a stale
# lease file left by a long-dead, PID-reused process.


def current_library_pdf_doc_ids(zotero) -> set[str]:
    """Return current Zotero item keys that still have resolved PDF files."""
    doc_ids: set[str] = set()
    for item in zotero.get_all_items_with_pdfs():
        if item.pdf_path and item.pdf_path.exists():
            doc_ids.add(item.item_key)
    return doc_ids


def _stored_doc_ids_or_current(store, current_doc_ids: set[str]) -> set[str]:
    """Best-effort read of stored doc IDs.

    In production stores this should be a concrete set-like value. In tests or
    partial mocks, missing/non-iterable values fall back to the current library
    set so journal authority still works.
    """
    getter = getattr(store, "get_indexed_doc_ids", None)
    if getter is None:
        return set(current_doc_ids)
    try:
        raw = getter()
    except Exception:
        return set(current_doc_ids)
    if isinstance(raw, (set, list, tuple)):
        return set(raw)
    return set(current_doc_ids)


# ---------------------------------------------------------------------------
# Journal state management
# ---------------------------------------------------------------------------


class IndexJournal:
    """In-memory journal tracking doc indexing state with atomic disk persistence."""

    def __init__(self, journal_path: str | Path | None = None) -> None:
        self._path: Path | None = None
        if journal_path is not None:
            self._path = Path(journal_path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self.committed: dict[str, dict] = {}
        self.in_progress: dict[str, dict] = {}
        self.table_failures: dict[str, str] = {}
        self._load()

    @property
    def path(self) -> Path | None:
        return self._path

    def _load(self) -> None:
        """Load journal from disk if a path is set and the file exists.

        A corrupt file must not brick indexing — log and start from empty so the
        run can rewrite it (atomic _save makes future writes crash-safe).
        """
        if self._path is None or not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Ignoring corrupt index journal %s: %s", self._path, e)
            return
        for doc_id, entry in data.items():
            if entry.get("state") == "committed":
                self.committed[doc_id] = entry
            elif entry.get("state") == "in_progress":
                self.in_progress[doc_id] = entry
            if "table_failure" in entry:
                self.table_failures[doc_id] = entry["table_failure"]

    def _save(self) -> None:
        """Persist journal to disk using atomic write (tempfile + os.replace)."""
        if self._path is None:
            return
        data: dict[str, dict] = {}
        for doc_id, entry in self.in_progress.items():
            data[doc_id] = entry
        for doc_id, entry in self.committed.items():
            data[doc_id] = entry
            if doc_id in self.table_failures:
                data[doc_id]["table_failure"] = self.table_failures[doc_id]

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp", prefix="zotpilot_journal_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self._path)
            tmp_path = None
        except OSError as e:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise RuntimeError(f"Failed to write journal to {self._path}: {e}") from e

    def get_committed_doc_ids(self) -> set[str]:
        """Return the set of committed doc IDs from the journal."""
        return set(self.committed.keys())


class IndexLease:
    """Mutual-exclusion lease for indexing operations."""

    def __init__(self, lease_path: str | Path | None = None) -> None:
        self._path: Path | None = None
        if lease_path is not None:
            self._path = Path(lease_path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self.holder_pid: int | None = None
        self.acquired_at: float | None = None
        self._load()

    @property
    def path(self) -> Path | None:
        return self._path

    def _load(self) -> None:
        """Load lease from disk if a path is set and the file exists.

        A corrupt lease file is treated as "no lease held" so a crash mid-write
        cannot permanently block all indexing.
        """
        if self._path is None or not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Ignoring corrupt index lease %s: %s", self._path, e)
            return
        self.holder_pid = data.get("holder_pid")
        self.acquired_at = data.get("acquired_at")

    def _save(self) -> None:
        """Persist lease to disk using atomic write."""
        if self._path is None:
            return
        data = {
            "holder_pid": self.holder_pid,
            "acquired_at": self.acquired_at,
        }
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp", prefix="zotpilot_lease_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self._path)
            tmp_path = None
        except OSError as e:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise RuntimeError(f"Failed to write lease to {self._path}: {e}") from e


class LeaseContentionError(Exception):
    """Raised when a lease cannot be acquired due to an active holder."""

    pass


# ---------------------------------------------------------------------------
# Journal helper functions
# ---------------------------------------------------------------------------


def mark_in_progress(journal: IndexJournal, doc_id: str) -> None:
    """Mark a document as currently being indexed."""
    entry = {"state": "in_progress", "timestamp": time.time()}
    journal.in_progress[doc_id] = entry
    journal.committed.pop(doc_id, None)
    journal._save()


def mark_committed(journal: IndexJournal, doc_id: str) -> None:
    """Mark a document as successfully indexed."""
    entry = {"state": "committed", "timestamp": time.time()}
    journal.committed[doc_id] = entry
    journal.in_progress.pop(doc_id, None)
    journal._save()


def get_committed_doc_ids(journal: IndexJournal) -> set[str]:
    """Return the set of committed doc IDs from the journal."""
    return set(journal.committed.keys())


def get_touched_doc_ids(journal: IndexJournal) -> set[str]:
    """Return all journal-tracked doc IDs (committed + in_progress)."""
    return set(journal.committed.keys()) | set(journal.in_progress.keys())


def is_doc_committed(journal: IndexJournal, doc_id: str) -> bool:
    """Check if a specific document is committed."""
    return doc_id in journal.committed


def record_table_failure(journal: IndexJournal, doc_id: str, reason: str) -> None:
    """Record a table/vision extraction failure for a committed doc (warning only)."""
    journal.table_failures[doc_id] = reason
    if doc_id in journal.committed:
        journal.committed[doc_id]["table_failure"] = reason
        journal._save()


def clear_table_failure(journal: IndexJournal, doc_id: str) -> None:
    """Clear a previously recorded table/figure failure.

    Called when a doc is reprocessed so a stale marker from an earlier run does
    not linger forever (markers were previously add-only — a doc that failed once
    and later reindexed cleanly kept the marker indefinitely). If this run fails
    again, ``record_table_failure`` re-adds it.
    """
    had_marker = journal.table_failures.pop(doc_id, None) is not None
    committed_entry = journal.committed.get(doc_id)
    if committed_entry is not None and "table_failure" in committed_entry:
        committed_entry.pop("table_failure", None)
        had_marker = True
    if had_marker:
        journal._save()


def acquire_lease(lease: IndexLease) -> str | None:
    """Attempt to acquire an indexing lease. Returns lease ID on success.

    Staleness is decided primarily by PROCESS LIVENESS, not wall-clock age. A
    lease whose holder PID is still alive is treated as held for the entire run
    — real indexing runs take many minutes (per-paper extraction plus vision
    Batch waves of "10-30min" each, up to 3 waves), and stealing a live holder
    mid-run lets two processes write the same Chroma collection concurrently and
    corrupt it (the exact P0 data-loss class this lease exists to prevent).

    The age check is only a backstop for a stale lease *file* left behind by a
    long-dead process whose PID was later reused by an unrelated process (so
    ``_is_pid_alive`` reports True forever). ``LEASE_STALE_SECONDS`` is therefore
    set well beyond any realistic single run.
    """
    now = time.time()
    if lease.holder_pid is not None and lease.acquired_at is not None:
        pid_alive = _is_pid_alive(lease.holder_pid)
        age = now - lease.acquired_at
        if not pid_alive or age > LEASE_STALE_SECONDS:
            # Holder crashed (dead PID) or the lease file is stale beyond the
            # backstop (likely a reused PID) — safe to clear and take over.
            lease.holder_pid = None
            lease.acquired_at = None
            lease._save()
        else:
            raise LeaseContentionError(f"Indexing lease held by PID {lease.holder_pid} (acquired {age:.0f}s ago)")

    lease.holder_pid = os.getpid()
    lease.acquired_at = now
    lease._save()
    return "active"


def release_lease(lease: IndexLease) -> None:
    """Release the current indexing lease."""
    lease.holder_pid = None
    lease.acquired_at = None
    lease._save()


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Authority functions (existing, updated)
# ---------------------------------------------------------------------------


def authoritative_indexed_doc_ids(store, current_doc_ids: set[str]) -> set[str]:
    """Return authoritative indexed doc IDs for the current library.

    Rules:
    - Start from docs that are both in the current library and in the store
    - If no journal exists, return that raw intersection
    - If a journal exists, committed journal docs are authoritative for touched docs
    - Legacy raw docs not represented in the journal are preserved
    - In-progress journal docs are excluded
    """
    current = set(current_doc_ids)
    stored = _stored_doc_ids_or_current(store, current)
    raw_indexed = current & stored

    db_path = getattr(store, "db_path", None)
    if db_path is None:
        return raw_indexed

    journal_path = Path(db_path).parent / "index_journal.json"
    if not journal_path.exists():
        return raw_indexed

    journal = IndexJournal(journal_path)
    touched = get_touched_doc_ids(journal)
    committed = get_committed_doc_ids(journal) & raw_indexed
    legacy_raw = raw_indexed - touched
    return committed | legacy_raw


def authoritative_indexed_doc_ids_with_journal(store, current_doc_ids: set[str], journal: IndexJournal) -> set[str]:
    """Return indexed doc IDs based on journal authority that still exist in the current library."""
    current = set(current_doc_ids)
    stored = _stored_doc_ids_or_current(store, current)
    raw_indexed = current & stored
    touched = get_touched_doc_ids(journal)
    committed = get_committed_doc_ids(journal) & raw_indexed
    legacy_raw = raw_indexed - touched
    return committed | legacy_raw


def orphaned_index_doc_ids(store, current_doc_ids: set[str]) -> set[str]:
    """Return indexed doc IDs that are no longer present in the current Zotero PDF library."""
    current = set(current_doc_ids)
    return set(store.get_indexed_doc_ids()) - current


def _mass_delete_refusal_reason(
    *,
    current_doc_ids: set[str],
    library_unreachable: bool,
    orphan_count: int,
    index_size: int,
    allow_mass_delete: bool,
) -> str | None:
    """Return a human-readable refusal reason if mass deletion must be blocked, else None.

    Breach conditions (any one):
      (a) the current-library read is empty (an unmounted drive or partial read is
          indistinguishable from a truly empty library);
      (b) the library/data directory is unreachable;
      (c) the proposed deletion exceeds ``MASS_DELETE_FRACTION_FLOOR`` of the index.

    ``allow_mass_delete`` bypasses ONLY (c); (a) and (b) are never legitimate signals
    to wipe the index, so they refuse even under the override.
    """
    if library_unreachable:
        return (
            f"Zotero library/data directory is unreachable — refusing to delete "
            f"{orphan_count} indexed document(s). Verify the data directory/drive is "
            f"mounted, then re-run (override is intentionally disabled for unreachable reads)."
        )
    if len(current_doc_ids) == 0:
        return (
            f"current Zotero library read returned 0 items — refusing to delete "
            f"{orphan_count} indexed document(s) (an unmounted drive or partial read looks "
            f"identical to an empty library). Verify the library, then re-run (override is "
            f"intentionally disabled for empty reads)."
        )
    if not allow_mass_delete and index_size > 0:
        fraction = orphan_count / index_size
        if fraction > MASS_DELETE_FRACTION_FLOOR:
            return (
                f"proposed deletion of {orphan_count}/{index_size} indexed document(s) "
                f"({fraction:.0%}) exceeds the {MASS_DELETE_FRACTION_FLOOR:.0%} safety floor. "
                f"If you really removed this many papers in Zotero, re-run with "
                f"`zotpilot doctor reconcile --force` or `index_library(..., allow_mass_delete=True)`."
            )
    return None


def reconcile_orphaned_index_docs(
    store,
    current_doc_ids: set[str],
    *,
    allow_mass_delete: bool = False,
    library_unreachable: bool = False,
) -> dict:
    """Delete orphaned indexed docs from Chroma and return a summary.

    An always-on safety floor refuses mass deletion when the current-library read
    looks untrustworthy (empty read, unreachable data dir, or a deletion exceeding
    ``MASS_DELETE_FRACTION_FLOOR`` of the index), so a transient signal can never
    silently wipe the index.

    Return contract: ``orphaned_doc_ids`` (the would-be orphans) and ``deleted_count``
    are ALWAYS present with unchanged semantics. On refusal nothing is deleted,
    ``deleted_count == 0``, and the summary ADDS ``refused_mass_delete: True`` plus
    ``skipped_reason``; otherwise ``refused_mass_delete: False``.
    """
    indexed = set(store.get_indexed_doc_ids())
    orphaned = sorted(indexed - set(current_doc_ids))

    if orphaned:
        reason = _mass_delete_refusal_reason(
            current_doc_ids=current_doc_ids,
            library_unreachable=library_unreachable,
            orphan_count=len(orphaned),
            index_size=len(indexed),
            allow_mass_delete=allow_mass_delete,
        )
        if reason is not None:
            db_path = getattr(store, "db_path", None)
            logger.warning(
                "Refusing orphan reconciliation: %s (index path: %s)",
                reason,
                db_path if db_path is not None else "<unknown>",
            )
            return {
                "orphaned_doc_ids": orphaned,
                "deleted_count": 0,
                "refused_mass_delete": True,
                "skipped_reason": reason,
            }

    for doc_id in orphaned:
        store.delete_document(doc_id)
    return {
        "orphaned_doc_ids": orphaned,
        "deleted_count": len(orphaned),
        "refused_mass_delete": False,
    }
