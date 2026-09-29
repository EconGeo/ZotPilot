"""Index stats must agree with the vector store (regression for 2026-09-29).

An MCP run left a scanned PDF "empty" and its journal entry stuck at in_progress.
After OCR the CLI indexed it, but the CLI never wrote the journal, so the stuck
entry kept get_index_stats reporting the doc as unindexed. The stats totals also
counted only the user library, so group-library docs never showed up in them.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from zotpilot.index_authority import IndexJournal, is_doc_committed, mark_in_progress


def _make_indexer():
    from zotpilot.indexer import Indexer

    config = MagicMock()
    config.zotero_data_dir = Path("/fake")
    config.chroma_db_path = Path("/fake/chroma")
    config.chunk_size = 1000
    config.chunk_overlap = 200
    config.vision_enabled = False
    config.anthropic_api_key = None
    with patch("zotpilot.indexer.ZoteroClient"), \
         patch("zotpilot.indexer.create_embedder"), \
         patch("zotpilot.indexer.VectorStore"), \
         patch("zotpilot.indexer.JournalRanker"):
        return Indexer(config)


def _empty_extraction():
    return SimpleNamespace(
        pages=[SimpleNamespace(markdown="")] * 9,
        stats={"total_pages": 9, "text_pages": 0, "ocr_pages": 0, "empty_pages": 9},
        quality_grade="F",
    )


def test_empty_extraction_leaves_no_in_progress_entry(tmp_path):
    indexer = _make_indexer()
    journal = IndexJournal(tmp_path / "index_journal.json")
    item = SimpleNamespace(item_key="SCAN0001")

    n_chunks, *_ = indexer._index_extraction(item, _empty_extraction(), journal)

    assert n_chunks == 0
    assert "SCAN0001" not in journal.in_progress
    assert "SCAN0001" not in IndexJournal(journal.path).in_progress  # persisted too
    indexer.store.add_chunks.assert_not_called()


def test_empty_reindex_clears_entry_marked_before_extraction(tmp_path):
    """A changed PDF is marked in_progress before extraction; an empty result must clear it."""
    indexer = _make_indexer()
    journal = IndexJournal(tmp_path / "index_journal.json")
    mark_in_progress(journal, "SCAN0001")

    indexer._index_extraction(SimpleNamespace(item_key="SCAN0001"), _empty_extraction(), journal)

    assert "SCAN0001" not in IndexJournal(journal.path).in_progress


def test_cli_index_writes_the_same_journal_as_mcp(tmp_path):
    from zotpilot import cli

    config = MagicMock()
    config.validate.return_value = []
    config.chroma_db_path = tmp_path / "chroma"
    config.max_pages = 40
    args = SimpleNamespace(
        verbose=False, config=None, no_vision=False, max_pages=0, batch_size=0,
        force=False, limit=None, item_key=None, title=None,
    )
    result = {"results": [], "indexed": 0, "already_indexed": 0, "skipped": 0, "failed": 0, "empty": 0}
    with (
        patch.object(cli, "resolve_runtime_config", return_value=config),
        patch("zotpilot.indexer.index_all_libraries", return_value=result) as run,
    ):
        assert cli.cmd_index(args) == 0

    journal = run.call_args.kwargs.get("journal")
    assert isinstance(journal, IndexJournal)
    assert journal.path == tmp_path / "index_journal.json"


def test_cli_indexed_doc_clears_stale_in_progress_entry(tmp_path):
    """The path the incident took: stale entry + successful re-index must end committed."""
    indexer = _make_indexer()
    indexer._pdf_hash = MagicMock(return_value="h")
    journal = IndexJournal(tmp_path / "index_journal.json")
    mark_in_progress(journal, "SCAN0001")
    extraction = SimpleNamespace(
        pages=[SimpleNamespace(markdown="Some OCR text.")],
        stats={"total_pages": 1, "text_pages": 1, "ocr_pages": 0, "empty_pages": 0},
        quality_grade="A",
        full_markdown="Some OCR text.",
        sections=[],
        tables=[],
        figures=[],
    )
    indexer.chunker = MagicMock()
    indexer.chunker.chunk.return_value = [MagicMock()]
    item = SimpleNamespace(
        item_key="SCAN0001", title="t", authors="a", year=1994, citation_key="", publication="",
        doi="", tags="", collections="", pdf_path=tmp_path / "x.pdf",
    )
    with patch("zotpilot.pdf.reference_matcher.match_references", return_value={}):
        indexer._index_extraction(item, extraction, journal)

    assert is_doc_committed(IndexJournal(journal.path), "SCAN0001")


class _StatsStore:
    db_path = None  # no journal: authority is the raw store∩library intersection

    def __init__(self, doc_ids):
        self._ids = set(doc_ids)
        self.collection = MagicMock()
        self.collection.get.return_value = {"metadatas": []}

    def get_indexed_doc_ids(self):
        return set(self._ids)

    def count_chunks_for_doc_ids(self, doc_ids):
        return 10 * len(doc_ids & self._ids)


def test_index_stats_totals_include_group_libraries(tmp_path, monkeypatch):
    import zotpilot.tools.indexing as indexing_mod
    from tests.test_multi_library_indexing import _make_db

    data_dir = _make_db(tmp_path)
    cfg = SimpleNamespace(
        zotero_data_dir=data_dir, embedding_provider="ollama", stats_sample_limit=100,
    )
    store = _StatsStore({"USERAAAA", "GRPBBBBB"})
    monkeypatch.setattr(indexing_mod, "_get_config", lambda: cfg)
    monkeypatch.setattr(indexing_mod, "_get_store", lambda: store)
    monkeypatch.setattr(indexing_mod, "_get_retriever", lambda: None)

    stats = indexing_mod.get_index_stats(limit=10)

    assert stats["total_documents"] == 2
    assert stats["total_chunks"] == 20
    assert stats["unindexed_count"] == 0
