"""Shared test fixtures for ZotPilot tests."""
# Isolate research-session persistence BEFORE any zotpilot module imports.
# Without this, tests share ``~/.local/share/zotpilot/sessions`` with the
# user's real MCP server state: any in-flight research session triggers
# Gate 2 and causes write-operation tests (create_note, manage_tags,
# manage_collections) to fail.  Point the session store at an ephemeral
# temp dir so the test run stays hermetic regardless of host state.
import os
import tempfile

os.environ["ZOTPILOT_SESSIONS_DIR"] = tempfile.mkdtemp(prefix="zotpilot-test-sessions-")
os.environ["ZOTPILOT_BATCHES_DIR"] = tempfile.mkdtemp(prefix="zotpilot-test-batches-")
os.environ["ZOTPILOT_SECRET_BACKEND"] = "local-file"
os.environ["ZOTPILOT_LOCAL_SECRETS_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="zotpilot-test-secrets-"), "secrets.json"
)
# Point the shared shell secrets file at an ephemeral path that does not
# exist. Without this, credential resolution would read the developer's real
# ~/.secrets.env and tests would pass or fail depending on whose machine they
# run on — and a test that writes a secret would edit that real file.
os.environ["ZOTPILOT_ENV_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="zotpilot-test-envfile-"), "secrets.env"
)

from unittest.mock import MagicMock

import pytest

from zotpilot.models import (
    Chunk,
    PageExtraction,
    RetrievalResult,
    SectionSpan,
    ZoteroItem,
)


def pytest_addoption(parser):
    parser.addoption(
        "--benchmark",
        action="store_true",
        default=False,
        help="run external benchmark tests",
    )


@pytest.fixture(autouse=True)
def isolated_secrets_env_file(tmp_path, monkeypatch):
    """Give every test its own (initially absent) shared secrets file.

    The module-level ZOTPILOT_ENV_FILE above is one path for the whole
    session, so a test that *writes* a secret would leak it into every later
    test's credential resolution. Re-pointing it per test keeps writes local.
    """
    monkeypatch.setenv("ZOTPILOT_ENV_FILE", str(tmp_path / "isolated-secrets.env"))


from pathlib import Path

from zotpilot.config import _default_data_dir

_REAL_DATA_DIR = _default_data_dir().expanduser().resolve()


@pytest.fixture(autouse=True)
def forbid_real_vector_store(monkeypatch):
    """Fail any test that opens a VectorStore inside the user's real data dir.

    VectorStore's startup probe moves an unopenable store aside and starts an
    empty one. On 2026-09-29 a status test with no chroma_db_path did exactly
    that to a developer's damaged 1.6M-chunk index during a test run.
    """
    from zotpilot import vector_store

    real_init = vector_store.VectorStore.__init__

    def guarded_init(self, db_path, *args, **kwargs):
        resolved = Path(db_path).expanduser().resolve()
        if resolved == _REAL_DATA_DIR or _REAL_DATA_DIR in resolved.parents:
            raise RuntimeError(f"test opened the user's real vector store at {resolved}; use tmp_path")
        real_init(self, db_path, *args, **kwargs)

    monkeypatch.setattr(vector_store.VectorStore, "__init__", guarded_init)


def _magicmock_tree(root: Path) -> set[str]:
    stray = root / "MagicMock"
    if not stray.exists():
        return set()
    return {str(p) for p in stray.rglob("*")} | {str(stray)}


@pytest.fixture(autouse=True)
def forbid_magicmock_paths_in_cwd():
    """Fail any test that creates files under ``./MagicMock``.

    Code that joins a path onto a MagicMock attribute (``config.chroma_db_path``
    left unset on a ``MagicMock()`` config) and then mkdirs it writes a relative
    ``MagicMock/mock.chroma_db_path/...`` tree into whatever the cwd is, usually
    the repo root. Give the mock a real ``tmp_path`` instead.
    """
    cwd = Path.cwd()
    before = _magicmock_tree(cwd)
    yield
    created = sorted(_magicmock_tree(cwd) - before)
    if created:
        pytest.fail(
            f"test created stray MagicMock path(s) in {cwd}: {created}; "
            "set the mocked config path (e.g. config.chroma_db_path) to a tmp_path"
        )


@pytest.fixture
def single_library_indexing(monkeypatch):
    """Let index_all_libraries run against a mocked config and a mocked Indexer.

    The orchestrator opens the real Zotero database to enumerate libraries and
    takes an OS lock beside config.chroma_db_path. With a MagicMock config both
    resolve to junk paths (a stray MagicMock/ directory, FileNotFoundError), so
    tests that only care about what reaches Indexer.index_all stub them out.
    """
    from contextlib import nullcontext

    from zotpilot import indexer

    monkeypatch.setattr(indexer, "enumerate_indexable_libraries", lambda config: [(1, "My Library")])
    monkeypatch.setattr(indexer, "global_pdf_doc_ids", lambda config: set())
    monkeypatch.setattr(indexer, "index_write_lease", lambda data_root: nullcontext())


def pytest_collection_modifyitems(config, items):
    if config.getoption("--benchmark"):
        return
    skip_benchmark = pytest.mark.skip(reason="need --benchmark to run")
    for item in items:
        if "benchmark" in item.keywords:
            item.add_marker(skip_benchmark)


@pytest.fixture
def sample_chunks():
    """Create sample chunks for testing."""
    return [
        Chunk(
            text="This is the introduction to our study on neural networks.",
            chunk_index=0,
            page_num=1,
            char_start=0,
            char_end=56,
            section="introduction",
            section_confidence=1.0,
        ),
        Chunk(
            text="We used a transformer architecture with 12 attention heads.",
            chunk_index=1,
            page_num=2,
            char_start=56,
            char_end=114,
            section="methods",
            section_confidence=0.85,
        ),
        Chunk(
            text="Our results show a 15% improvement over the baseline.",
            chunk_index=2,
            page_num=3,
            char_start=114,
            char_end=167,
            section="results",
            section_confidence=1.0,
        ),
    ]


@pytest.fixture
def sample_pages():
    """Create sample page extractions."""
    return [
        PageExtraction(page_num=1, markdown="Introduction text...", char_start=0),
        PageExtraction(page_num=2, markdown="Methods text...", char_start=56),
        PageExtraction(page_num=3, markdown="Results text...", char_start=114),
    ]


@pytest.fixture
def sample_sections():
    """Create sample section spans."""
    return [
        SectionSpan(label="introduction", char_start=0, char_end=56, heading_text="Introduction", confidence=1.0),
        SectionSpan(label="methods", char_start=56, char_end=114, heading_text="Methods", confidence=0.85),
        SectionSpan(label="results", char_start=114, char_end=167, heading_text="Results", confidence=1.0),
    ]


@pytest.fixture
def sample_retrieval_result():
    """Create a sample retrieval result."""
    return RetrievalResult(
        chunk_id="TEST123_chunk_0001",
        text="Transformer models achieve state-of-the-art results.",
        score=0.85,
        doc_id="TEST123",
        doc_title="Attention Is All You Need",
        authors="Vaswani et al.",
        year=2017,
        page_num=5,
        chunk_index=1,
        citation_key="vaswani2017",
        publication="NeurIPS",
        section="results",
        section_confidence=1.0,
        journal_quartile="Q1",
    )


@pytest.fixture
def sample_zotero_item():
    """Create a sample Zotero item."""
    return ZoteroItem(
        item_key="TEST123",
        title="Attention Is All You Need",
        authors="Vaswani et al.",
        year=2017,
        pdf_path=None,
        citation_key="vaswani2017",
        publication="NeurIPS",
        doi="10.1234/test",
        tags="deep-learning; transformers",
        collections="Machine Learning",
    )


@pytest.fixture
def mock_embedder():
    """Create a mock embedder."""
    embedder = MagicMock()
    embedder.dimensions = 768
    embedder.embed.side_effect = lambda texts, **kwargs: [[0.1] * 768 for _ in texts]
    embedder.embed_query.return_value = [0.1] * 768
    return embedder
