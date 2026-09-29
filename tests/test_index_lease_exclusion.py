"""Cross-process exclusion for indexing (regression for the 2026-09-28 HNSW corruption).

An MCP index_library run outlived its client's 30-minute timeout and kept writing;
`zotpilot index` then started a second writer on the same Chroma store. The lease
had aged out after 60 s and the CLI never took it. These tests pin both fixes.
"""
import json
import os
import subprocess
import sys
import textwrap
import time
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.index_authority import (
    IndexLease,
    LeaseContentionError,
    acquire_lease,
    index_write_lease,
    release_lease,
)


@pytest.fixture
def holder(tmp_path):
    """A separate process that holds the lease for tmp_path until told to exit."""
    script = textwrap.dedent(f"""
        import sys
        from zotpilot.index_authority import IndexLease, acquire_lease
        lease = IndexLease({str(tmp_path / "index_lease.json")!r})
        acquire_lease(lease)
        print("held", flush=True)
        sys.stdin.readline()
    """)
    import zotpilot

    src = str(Path(zotpilot.__file__).resolve().parents[1])  # same checkout as this test run
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": src},
    )
    assert proc.stdout.readline().strip() == "held"
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def test_lease_held_by_other_process_blocks(tmp_path, holder):
    lease = IndexLease(tmp_path / "index_lease.json")
    with pytest.raises(LeaseContentionError, match=f"PID {holder.pid}"):
        acquire_lease(lease)


def test_long_running_holder_is_not_treated_as_stale(tmp_path, holder):
    """The holder's timestamp being hours old must not let a second writer in."""
    path = tmp_path / "index_lease.json"
    path.write_text(json.dumps({"holder_pid": holder.pid, "acquired_at": time.time() - 4 * 3600}))
    with pytest.raises(LeaseContentionError):
        acquire_lease(IndexLease(path))


def test_lease_freed_when_holder_dies(tmp_path, holder):
    holder.kill()
    holder.wait()
    lease = IndexLease(tmp_path / "index_lease.json")
    acquire_lease(lease)  # kernel released the lock with the dead process
    assert lease.held
    release_lease(lease)


def test_second_lease_in_same_process_blocks(tmp_path):
    first = IndexLease(tmp_path / "index_lease.json")
    acquire_lease(first)
    try:
        with pytest.raises(LeaseContentionError):
            acquire_lease(IndexLease(tmp_path / "index_lease.json"))
    finally:
        release_lease(first)
    acquire_lease(second := IndexLease(tmp_path / "index_lease.json"))
    release_lease(second)


def test_release_by_non_holder_leaves_holder_record(tmp_path, holder):
    """A contending caller's cleanup must not wipe the real holder's record."""
    lease = IndexLease(tmp_path / "index_lease.json")
    with pytest.raises(LeaseContentionError):
        acquire_lease(lease)
    release_lease(lease)
    data = json.loads((tmp_path / "index_lease.json").read_text())
    assert data["holder_pid"] == holder.pid


def test_index_all_libraries_refuses_while_lease_held(tmp_path, holder):
    from zotpilot.indexer import index_all_libraries

    config = MagicMock()
    config.chroma_db_path = tmp_path / "chroma"
    with patch("zotpilot.indexer._index_all_libraries_locked") as locked:
        with pytest.raises(LeaseContentionError):
            index_all_libraries(config)
    locked.assert_not_called()


def test_index_all_libraries_holds_lease_during_run(tmp_path):
    from zotpilot.indexer import index_all_libraries

    config = MagicMock()
    config.chroma_db_path = tmp_path / "chroma"
    seen = {}

    def body(*_args, **_kwargs):
        with pytest.raises(LeaseContentionError):
            acquire_lease(IndexLease(tmp_path / "index_lease.json"))
        seen["ran"] = True
        return {"results": []}

    with patch("zotpilot.indexer._index_all_libraries_locked", side_effect=body):
        index_all_libraries(config)
    assert seen["ran"]
    with index_write_lease(tmp_path):  # released afterwards
        pass


def test_cli_index_exits_nonzero_when_lease_held(tmp_path, holder, capsys):
    from zotpilot import cli

    config = MagicMock()
    config.validate.return_value = []
    config.chroma_db_path = tmp_path / "chroma"
    config.max_pages = 40
    args = Namespace(
        verbose=False, config=None, no_vision=False, max_pages=0, batch_size=0,
        force=False, limit=None, item_key=None, title=None,
    )
    with (
        patch.object(cli, "resolve_runtime_config", return_value=config),
        patch("zotpilot.indexer._index_all_libraries_locked") as locked,
    ):
        assert cli.cmd_index(args) == 1
    locked.assert_not_called()
    assert "Indexing lease held by PID" in capsys.readouterr().err


def test_mcp_index_library_reports_contention_as_tool_error(tmp_path, holder):
    from zotpilot.state import ToolError
    from zotpilot.tools.indexing import index_library

    config = MagicMock()
    config.validate.return_value = []
    config.chroma_db_path = tmp_path / "chroma"
    config.max_pages = 40
    with (
        patch("zotpilot.tools.indexing._get_config", return_value=config),
        patch("zotpilot.indexer._index_all_libraries_locked") as locked,
        patch("dataclasses.replace", side_effect=lambda obj, **kwargs: obj),
    ):
        with pytest.raises(ToolError, match="Indexing lease held by PID"):
            index_library()
    locked.assert_not_called()
    assert json.loads((tmp_path / "index_lease.json").read_text())["holder_pid"] == holder.pid
