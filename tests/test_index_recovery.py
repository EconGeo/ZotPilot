"""Tests for the zero-cost ChromaDB index recovery engine (P2 / AC4, AC14)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.index_recovery import (
    HnswlibUnavailableError,
    RecoverySourceError,
    RecoveryVerificationError,
    discover_corrupt_backups,
    load_sqlite_records,
    rebuild_collection,
    recover_index,
    resolve_source,
    verify_recovery,
)

VEC_SEG = "65b44def-vec-segment"
META_SEG = "820ae23a-meta-segment"


def _make_chroma_sqlite(path: Path, rows: list[tuple[str, str, dict]]) -> None:
    """Create a minimal but faithful chroma.sqlite3 (embeddings + metadata + segments)."""
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE embeddings (id INTEGER PRIMARY KEY, segment_id TEXT NOT NULL, "
        "embedding_id TEXT NOT NULL, seq_id BLOB, created_at TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE embedding_metadata (id INTEGER, key TEXT NOT NULL, string_value TEXT, "
        "int_value INTEGER, float_value REAL, bool_value INTEGER, PRIMARY KEY (id, key))"
    )
    con.execute(
        "CREATE TABLE segments (id TEXT PRIMARY KEY, type TEXT NOT NULL, scope TEXT NOT NULL, collection TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO segments VALUES (?,?,?,?)",
        (VEC_SEG, "urn:chroma:segment/vector/hnsw-local-persisted", "VECTOR", "col"),
    )
    con.execute(
        "INSERT INTO segments VALUES (?,?,?,?)",
        (META_SEG, "urn:chroma:segment/metadata/sqlite", "METADATA", "col"),
    )
    for i, (chroma_id, doc, meta) in enumerate(rows, start=1):
        con.execute(
            "INSERT INTO embeddings (id, segment_id, embedding_id, seq_id) VALUES (?,?,?,?)",
            (i, META_SEG, chroma_id, b""),
        )
        con.execute(
            "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?,?,?)",
            (i, "chroma:document", doc),
        )
        for key, value in meta.items():
            if isinstance(value, bool):
                con.execute(
                    "INSERT INTO embedding_metadata (id, key, bool_value) VALUES (?,?,?)",
                    (i, key, int(value)),
                )
            elif isinstance(value, int):
                con.execute(
                    "INSERT INTO embedding_metadata (id, key, int_value) VALUES (?,?,?)",
                    (i, key, value),
                )
            elif isinstance(value, float):
                con.execute(
                    "INSERT INTO embedding_metadata (id, key, float_value) VALUES (?,?,?)",
                    (i, key, value),
                )
            else:
                con.execute(
                    "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?,?,?)",
                    (i, key, value),
                )
    con.commit()
    con.close()


def _make_backup(parent: Path, rows: list[tuple[str, str, dict]], *, with_segment: bool = True) -> Path:
    """Build a `chroma.corrupt-<ns>` backup dir with SQLite (+ optional HNSW segment)."""
    backup = parent / "chroma.corrupt-1700000000000000000"
    backup.mkdir(parents=True)
    _make_chroma_sqlite(backup / "chroma.sqlite3", rows)
    if with_segment:
        seg = backup / VEC_SEG
        seg.mkdir()
        # Real HNSW bytes are irrelevant here — load_hnsw_vectors is patched; we only
        # need find_vector_segment_dir to locate a dir containing data_level0.bin.
        (seg / "data_level0.bin").write_bytes(b"\x00" * 16)
        (seg / "index_metadata.pickle").write_bytes(b"")
    return backup


_ROWS = [
    ("paperA_chunk_0000", "alpha text", {"doc_id": "paperA", "page_num": 1}),
    ("paperA_chunk_0001", "beta text", {"doc_id": "paperA", "page_num": 2}),
    ("paperB_chunk_0000", "gamma text", {"doc_id": "paperB", "page_num": 1}),
]
_VECTORS = {
    "paperA_chunk_0000": [1.0, 0.0, 0.0, 0.0],
    "paperA_chunk_0001": [0.0, 1.0, 0.0, 0.0],
    "paperB_chunk_0000": [0.0, 0.0, 1.0, 0.0],
}


class TestSourceDiscovery:
    def test_discover_corrupt_backups_glob(self, tmp_path):
        # AC14: autodiscovery uses the real {name}.corrupt-* glob.
        db_path = tmp_path / "chroma"
        backup = _make_backup(tmp_path, _ROWS)
        found = discover_corrupt_backups(db_path)
        assert found == [backup]

    def test_resolve_source_autodiscovers(self, tmp_path):
        db_path = tmp_path / "chroma"
        backup = _make_backup(tmp_path, _ROWS)
        assert resolve_source(db_path, None) == backup

    def test_resolve_source_explicit(self, tmp_path):
        db_path = tmp_path / "chroma"
        backup = _make_backup(tmp_path, _ROWS)
        assert resolve_source(db_path, backup) == backup

    def test_resolve_source_none_found(self, tmp_path):
        db_path = tmp_path / "chroma"
        with pytest.raises(RecoverySourceError):
            resolve_source(db_path, None)

    def test_resolve_source_explicit_not_a_backup(self, tmp_path):
        db_path = tmp_path / "chroma"
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(RecoverySourceError):
            resolve_source(db_path, empty)


class TestSqliteReconstruction:
    def test_load_sqlite_records(self, tmp_path):
        sqlite_path = tmp_path / "chroma.sqlite3"
        _make_chroma_sqlite(sqlite_path, _ROWS)
        records = load_sqlite_records(sqlite_path)
        assert len(records) == 3
        by_id = {r.chroma_id: r for r in records}
        assert by_id["paperA_chunk_0000"].document == "alpha text"
        assert by_id["paperA_chunk_0000"].metadata["doc_id"] == "paperA"
        assert by_id["paperA_chunk_0000"].metadata["page_num"] == 1


class TestRecoverHnswZeroCost:
    def test_recover_hnsw_zero_embed_calls(self, tmp_path):
        # AC4: HNSW path supplies explicit vectors => embedder.embed is NEVER called.
        db_path = tmp_path / "chroma"
        _make_backup(tmp_path, _ROWS)
        embedder = MagicMock()
        embedder.embed = MagicMock(side_effect=AssertionError("embedder must not be called"))

        with patch("zotpilot.index_recovery.load_hnsw_vectors", return_value=dict(_VECTORS)):
            report = recover_index(db_path, dim=4, embedder=embedder)

        embedder.embed.assert_not_called()
        assert report.method == "hnsw"
        assert report.recovered_count == 3
        assert report.doc_count == 2
        assert report.verified is True
        assert report.swapped is True
        # Swapped into place: db_path now holds the rebuilt collection.
        assert (db_path / "chroma.sqlite3").exists()
        records = load_sqlite_records(db_path / "chroma.sqlite3")
        assert len(records) == 3

    def test_dry_run_writes_nothing(self, tmp_path):
        db_path = tmp_path / "chroma"
        _make_backup(tmp_path, _ROWS)
        with patch("zotpilot.index_recovery.load_hnsw_vectors", return_value=dict(_VECTORS)):
            report = recover_index(db_path, dim=4, dry_run=True)
        assert report.dry_run is True
        assert report.recovered_count == 3
        assert report.swapped is False
        assert not db_path.exists()
        # No .recovered-* dir left behind on a dry run.
        assert not list(tmp_path.glob("chroma.recovered-*"))


class TestReembedFallback:
    def test_reembed_fallback_costs_embed_calls(self, tmp_path):
        # P2.4: HNSW unreadable -> re-embed stored text via configured embedder (paid).
        db_path = tmp_path / "chroma"
        _make_backup(tmp_path, _ROWS, with_segment=False)
        embedder = MagicMock()
        embedder.embed = MagicMock(return_value=[_VECTORS[r[0]] for r in _ROWS])

        report = recover_index(
            db_path,
            dim=4,
            embedder=embedder,
            allow_reembed=True,
            confirm=lambda _report: True,
        )
        embedder.embed.assert_called_once()
        assert report.method == "reembed"
        assert report.recovered_count == 3
        assert report.swapped is True

    def test_reembed_blocked_without_permission(self, tmp_path):
        db_path = tmp_path / "chroma"
        _make_backup(tmp_path, _ROWS, with_segment=False)
        embedder = MagicMock()
        with pytest.raises(HnswlibUnavailableError):
            recover_index(db_path, dim=4, embedder=embedder, allow_reembed=False)

    def test_hnswlib_missing_triggers_reembed_path(self, tmp_path):
        db_path = tmp_path / "chroma"
        _make_backup(tmp_path, _ROWS)
        embedder = MagicMock()
        embedder.embed = MagicMock(return_value=[_VECTORS[r[0]] for r in _ROWS])
        with patch(
            "zotpilot.index_recovery.load_hnsw_vectors",
            side_effect=HnswlibUnavailableError("missing"),
        ):
            report = recover_index(db_path, dim=4, embedder=embedder, allow_reembed=True, confirm=lambda _r: True)
        assert report.method == "reembed"
        embedder.embed.assert_called_once()


class TestVerificationGate:
    def test_verify_count_mismatch_raises(self, tmp_path):
        out_dir = tmp_path / "out"
        rebuild_collection(
            out_dir,
            ["x_chunk_0000"],
            [[1.0, 0.0, 0.0, 0.0]],
            ["doc"],
            [{"doc_id": "x"}],
            4,
        )
        with pytest.raises(RecoveryVerificationError):
            verify_recovery(out_dir, expected_count=99, dim=4, sample_id=None, sample_vector=None)

    def test_verify_dim_mismatch_raises(self, tmp_path):
        out_dir = tmp_path / "out"
        rebuild_collection(out_dir, ["x_chunk_0000"], [[1.0, 0.0, 0.0, 0.0]], ["doc"], [{"doc_id": "x"}], 4)
        with pytest.raises(RecoveryVerificationError):
            verify_recovery(out_dir, expected_count=1, dim=8, sample_id=None, sample_vector=None)

    def test_verification_failure_aborts_swap_original_untouched(self, tmp_path):
        # Negative: verification failure keeps the new dir aside and leaves the original intact.
        db_path = tmp_path / "chroma"
        db_path.mkdir()
        (db_path / "sentinel.txt").write_text("ORIGINAL")  # marker proving no swap happened
        _make_backup(tmp_path, _ROWS)

        with (
            patch("zotpilot.index_recovery.load_hnsw_vectors", return_value=dict(_VECTORS)),
            patch(
                "zotpilot.index_recovery.verify_recovery",
                side_effect=RecoveryVerificationError("forced"),
            ),
        ):
            with pytest.raises(RecoveryVerificationError):
                recover_index(db_path, dim=4, embedder=MagicMock())

        # Original untouched.
        assert (db_path / "sentinel.txt").read_text() == "ORIGINAL"
        # The unverified rebuild is kept aside for inspection, not swapped in.
        assert list(tmp_path.glob("chroma.recovered-*"))
