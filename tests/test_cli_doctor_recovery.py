"""Tests for `zotpilot doctor` recovery/reconcile CLI glue + arg parsing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from zotpilot.cli import cmd_doctor, main
from zotpilot.index_recovery import RecoveryReport


def _doctor_args(**overrides) -> SimpleNamespace:
    base = dict(
        config=None,
        json=False,
        full=False,
        recover_index=False,
        reconcile=False,
        source=None,
        dry_run=False,
        force=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestDoctorArgParsing:
    def test_recover_flags_parse_via_main(self):
        # The real doctor subparser exposes the new flags with argparse dest mangling.
        with patch("zotpilot.cli.cmd_doctor", return_value=0) as mock_cmd:
            main(["doctor", "--recover-index", "--dry-run", "--source", "/tmp/x"])
        ns = mock_cmd.call_args.args[0]
        assert ns.recover_index is True
        assert ns.dry_run is True
        assert ns.source == "/tmp/x"

    def test_reconcile_force_flag_parse_via_main(self):
        with patch("zotpilot.cli.cmd_doctor", return_value=0) as mock_cmd:
            main(["doctor", "--reconcile", "--force"])
        ns = mock_cmd.call_args.args[0]
        assert ns.reconcile is True
        assert ns.force is True


class TestRecoverIndexGlue:
    @patch("zotpilot.index_recovery.recover_index")
    @patch("zotpilot.embeddings.create_embedder")
    @patch("zotpilot.cli.resolve_runtime_config")
    def test_dry_run_reports_and_exits_zero(self, mock_cfg, _mock_embedder, mock_recover, capsys):
        cfg = MagicMock()
        cfg.chroma_db_path = Path("/tmp/chroma")
        cfg.embedding_dimensions = 768
        cfg.embedding_provider = "gemini"
        mock_cfg.return_value = cfg
        report = RecoveryReport(source=Path("/tmp/chroma.corrupt-1"), dry_run=True)
        report.recovered_count = 42
        report.doc_count = 5
        mock_recover.return_value = report

        rc = cmd_doctor(_doctor_args(recover_index=True, dry_run=True))
        out = capsys.readouterr().out
        assert rc == 0
        assert "DRY RUN" in out
        assert "42" in out
        # dry_run forwarded to the engine.
        assert mock_recover.call_args.kwargs["dry_run"] is True

    @patch("zotpilot.index_recovery.recover_index")
    @patch("zotpilot.embeddings.create_embedder")
    @patch("zotpilot.cli.resolve_runtime_config")
    def test_success_swap_reports_and_exits_zero(self, mock_cfg, _mock_embedder, mock_recover, capsys):
        cfg = MagicMock()
        cfg.chroma_db_path = Path("/tmp/chroma")
        cfg.embedding_dimensions = 768
        mock_cfg.return_value = cfg
        report = RecoveryReport(source=Path("/tmp/chroma.corrupt-1"))
        report.recovered_count = 100
        report.doc_count = 9
        report.swapped = True
        mock_recover.return_value = report

        rc = cmd_doctor(_doctor_args(recover_index=True))
        out = capsys.readouterr().out
        assert rc == 0
        assert "Recovered 100 chunks" in out

    @patch("zotpilot.index_recovery.recover_index")
    @patch("zotpilot.embeddings.create_embedder")
    @patch("zotpilot.cli.resolve_runtime_config")
    def test_missing_hnswlib_prints_install_hint(self, mock_cfg, _mock_embedder, mock_recover, capsys):
        from zotpilot.index_recovery import HnswlibUnavailableError

        cfg = MagicMock()
        cfg.chroma_db_path = Path("/tmp/chroma")
        cfg.embedding_dimensions = 768
        mock_cfg.return_value = cfg
        mock_recover.side_effect = HnswlibUnavailableError("chroma-hnswlib not installed")

        rc = cmd_doctor(_doctor_args(recover_index=True, dry_run=True))
        out = capsys.readouterr().out
        assert rc == 1
        assert "uv sync --extra recover" in out


class TestReconcileGlue:
    @patch("zotpilot.zotero_client.ZoteroClient")
    @patch("zotpilot.index_authority.current_library_pdf_doc_ids")
    @patch("zotpilot.index_authority.orphaned_index_doc_ids")
    @patch("zotpilot.vector_store.VectorStore")
    @patch("zotpilot.embeddings.create_embedder")
    @patch("zotpilot.cli.resolve_runtime_config")
    def test_reconcile_dry_run_preview(self, mock_cfg, _emb, _vs, mock_orphans, mock_current, _zot, capsys):
        cfg = MagicMock()
        cfg.chroma_db_path = Path("/tmp/chroma")
        cfg.zotero_data_dir = Path("/tmp/zot")
        mock_cfg.return_value = cfg
        mock_current.return_value = {"a", "b", "c"}
        mock_orphans.return_value = {"orphan1", "orphan2"}

        rc = cmd_doctor(_doctor_args(reconcile=True, dry_run=True))
        out = capsys.readouterr().out
        assert rc == 0
        assert "Orphaned indexed documents: 2" in out
        assert "No changes made" in out

    @patch("zotpilot.index_authority.reconcile_orphaned_index_docs")
    @patch("zotpilot.zotero_client.ZoteroClient")
    @patch("zotpilot.index_authority.current_library_pdf_doc_ids")
    @patch("zotpilot.vector_store.VectorStore")
    @patch("zotpilot.embeddings.create_embedder")
    @patch("zotpilot.cli.resolve_runtime_config")
    def test_reconcile_refusal_surfaces_reason(self, mock_cfg, _emb, _vs, mock_current, _zot, mock_reconcile, capsys):
        cfg = MagicMock()
        cfg.chroma_db_path = Path("/tmp/chroma")
        cfg.zotero_data_dir = Path("/tmp/zot")
        mock_cfg.return_value = cfg
        mock_current.return_value = {"a"}
        mock_reconcile.return_value = {
            "orphaned_doc_ids": ["x"],
            "deleted_count": 0,
            "refused_mass_delete": True,
            "skipped_reason": "deletion exceeds 25% floor",
        }

        rc = cmd_doctor(_doctor_args(reconcile=True))
        out = capsys.readouterr().out
        assert rc == 1
        assert "refused" in out.lower()
        assert "25%" in out
        # Non-force pass-through: allow_mass_delete defaults False.
        assert mock_reconcile.call_args.kwargs["allow_mass_delete"] is False

    @patch("zotpilot.index_authority.reconcile_orphaned_index_docs")
    @patch("zotpilot.zotero_client.ZoteroClient")
    @patch("zotpilot.index_authority.current_library_pdf_doc_ids")
    @patch("zotpilot.vector_store.VectorStore")
    @patch("zotpilot.embeddings.create_embedder")
    @patch("zotpilot.cli.resolve_runtime_config")
    def test_reconcile_force_passes_override(self, mock_cfg, _emb, _vs, mock_current, _zot, mock_reconcile, capsys):
        cfg = MagicMock()
        cfg.chroma_db_path = Path("/tmp/chroma")
        cfg.zotero_data_dir = Path("/tmp/zot")
        mock_cfg.return_value = cfg
        mock_current.return_value = {"a"}
        mock_reconcile.return_value = {
            "orphaned_doc_ids": ["x", "y"],
            "deleted_count": 2,
            "refused_mass_delete": False,
        }

        rc = cmd_doctor(_doctor_args(reconcile=True, force=True))
        out = capsys.readouterr().out
        assert rc == 0
        assert "deleted 2" in out
        assert mock_reconcile.call_args.kwargs["allow_mass_delete"] is True

    @patch("zotpilot.index_authority.reconcile_orphaned_index_docs")
    @patch("zotpilot.zotero_client.ZoteroClient")
    @patch("zotpilot.index_authority.current_library_pdf_doc_ids")
    @patch("zotpilot.vector_store.VectorStore")
    @patch("zotpilot.embeddings.create_embedder")
    @patch("zotpilot.cli.resolve_runtime_config")
    def test_reconcile_passes_library_unreachable(
        self, mock_cfg, _emb, _vs, mock_current, _zot, mock_reconcile, tmp_path, capsys
    ):
        # Parity with the auto-callers: an unmounted data dir must surface as
        # library_unreachable even under --force (closes the bypass gap).
        cfg = MagicMock()
        cfg.chroma_db_path = Path("/tmp/chroma")
        cfg.zotero_data_dir = tmp_path / "definitely-not-mounted"  # does not exist
        mock_cfg.return_value = cfg
        mock_current.return_value = {"a"}
        mock_reconcile.return_value = {"orphaned_doc_ids": [], "deleted_count": 0, "refused_mass_delete": False}

        cmd_doctor(_doctor_args(reconcile=True, force=True))
        assert mock_reconcile.call_args.kwargs["library_unreachable"] is True

        # A reachable data dir → library_unreachable False.
        cfg.zotero_data_dir = tmp_path
        cmd_doctor(_doctor_args(reconcile=True, force=True))
        assert mock_reconcile.call_args.kwargs["library_unreachable"] is False


class TestMainParserWiring:
    def test_main_doctor_recover_dispatches(self):
        # End-to-end: `zotpilot doctor --recover-index --dry-run` routes through cmd_doctor.
        with patch("zotpilot.cli.cmd_doctor", return_value=0) as mock_cmd:
            rc = main(["doctor", "--recover-index", "--dry-run"])
        assert rc == 0
        ns = mock_cmd.call_args.args[0]
        assert ns.recover_index is True
        assert ns.dry_run is True
