"""Ingest targets one library and never misreports what the connector saved.

Regression tests for the 2026-09-02 group-library ingest:
  1. results mapped to the wrong item (an earlier save's key and title echoed)
  2. has_pdf false on items that did get a PDF
  3. a connector timeout treated as a failure, creating a DOI-API duplicate
     in My Library
  4. the recent-saves cache returning a key deleted minutes earlier
"""
from __future__ import annotations

import sqlite3
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import zotpilot.tools.ingestion as ingestion_tool
from zotpilot.state import ToolError
from zotpilot.tools.ingestion import connector
from zotpilot.tools.ingestion.models import IngestCandidate
from zotpilot.tools.ingestion.target import IngestTarget, resolve_ingest_target
from zotpilot.zotero_client import ZoteroClient

USER = IngestTarget(local_library_id=1, library_type="user", remote_id="42", name="My Library")
GROUP = IngestTarget(local_library_id=8, library_type="group", remote_id="6075488", name="NAR_settlement")


# ---------------------------------------------------------------------------
# Fixture database: My Library (1) and one group (8)
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY, itemTypeID INTEGER,
            dateAdded TEXT DEFAULT '2024-01-01 00:00:00', key TEXT UNIQUE,
            libraryID INTEGER DEFAULT 1
        );
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        INSERT INTO fields VALUES (1, 'title'), (4, 'DOI'), (6, 'extra');
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemAttachments (
            itemID INTEGER PRIMARY KEY, parentItemID INTEGER,
            contentType TEXT, linkMode INTEGER, path TEXT
        );
        CREATE TABLE groups (groupID INTEGER PRIMARY KEY, libraryID INT NOT NULL,
                             name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                             version INT NOT NULL DEFAULT 0);
        INSERT INTO groups VALUES (6075488, 8, 'NAR_settlement', '', 0);
        INSERT INTO groups VALUES (123, 9, 'Other Group', '', 0);

        -- Old copy of the paper in My Library
        INSERT INTO items VALUES (1, 2, '2020-05-01 10:00:00', 'OLDUSER', 1);
        INSERT INTO itemDataValues VALUES (1, '10.1/x');
        INSERT INTO itemData VALUES (1, 4, 1);

        -- Fresh save of the same paper in the group, with a PDF
        INSERT INTO items VALUES (2, 2, '2026-09-29 12:00:05', 'NEWGRP', 8);
        INSERT INTO itemData VALUES (2, 4, 1);
        INSERT INTO items VALUES (3, 14, '2026-09-29 12:00:06', 'ATTGRP', 8);
        INSERT INTO itemAttachments VALUES (3, 2, 'application/pdf', 1, 'storage:a.pdf');

        -- A group item that was deleted
        INSERT INTO items VALUES (4, 2, '2026-09-29 12:00:07', 'GONE', 8);
        INSERT INTO deletedItems VALUES (4);

        -- An arXiv preprint identified only through extra
        INSERT INTO items VALUES (5, 2, '2026-09-29 12:00:08', 'ARXGRP', 8);
        INSERT INTO itemDataValues VALUES (2, 'arXiv:2301.00001 [cs.CV]');
        INSERT INTO itemData VALUES (5, 6, 2);
    """)
    conn.commit()
    conn.close()
    return tmp_path


class TestZoteroClientLookups:
    def test_find_items_by_doi_spans_libraries_newest_first(self, tmp_path):
        client = ZoteroClient(_make_db(tmp_path))
        rows = client.find_items_by_doi("https://doi.org/10.1/X", all_libraries=True)
        assert [(r["key"], r["library_id"]) for r in rows] == [("NEWGRP", 8), ("OLDUSER", 1)]
        assert rows[0]["date_added"] == "2026-09-29 12:00:05"

    def test_find_items_by_doi_defaults_to_own_library(self, tmp_path):
        client = ZoteroClient(_make_db(tmp_path), library_id=8)
        assert [r["key"] for r in client.find_items_by_doi("10.1/x")] == ["NEWGRP"]

    def test_find_items_by_arxiv_id(self, tmp_path):
        client = ZoteroClient(_make_db(tmp_path))
        rows = client.find_items_by_arxiv_id("2301.00001v2", all_libraries=True)
        assert [r["key"] for r in rows] == ["ARXGRP"]

    def test_item_key_exists_excludes_deleted_and_other_libraries(self, tmp_path):
        client = ZoteroClient(_make_db(tmp_path), library_id=8)
        assert client.item_key_exists("NEWGRP") is True
        assert client.item_key_exists("GONE") is False
        assert client.item_key_exists("OLDUSER") is False

    def test_item_has_pdf_attachment(self, tmp_path):
        client = ZoteroClient(_make_db(tmp_path), library_id=8)
        assert client.item_has_pdf_attachment("NEWGRP") is True
        assert client.item_has_pdf_attachment("ARXGRP") is False


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

class TestResolveIngestTarget:
    @pytest.mark.parametrize("value", [None, "", "user", "My Library", "personal"])
    def test_personal_library(self, tmp_path, value):
        client = ZoteroClient(_make_db(tmp_path))
        target = resolve_ingest_target(client, value, user_id="42")
        assert target == USER
        assert target.api_prefix == "users/0"

    @pytest.mark.parametrize("value", ["NAR_settlement", "nar_settlement", "6075488"])
    def test_group_by_name_or_id(self, tmp_path, value):
        client = ZoteroClient(_make_db(tmp_path))
        target = resolve_ingest_target(client, value, user_id="42")
        assert target == GROUP
        assert target.api_prefix == "groups/6075488"

    def test_unknown_library_lists_choices(self, tmp_path):
        client = ZoteroClient(_make_db(tmp_path))
        with pytest.raises(ToolError, match="NAR_settlement"):
            resolve_ingest_target(client, "No Such Group", user_id="42")


# ---------------------------------------------------------------------------
# Identity check (bug 1)
# ---------------------------------------------------------------------------

class TestIdentityCheck:
    def test_doi_mismatch_rejects(self):
        item = {"DOI": "10.1/prev", "title": "Previous Paper"}
        assert connector.identity_check(
            item, expected_dois={"10.1/cur"}, arxiv_id=None, title="Current Paper",
        ) is False

    def test_doi_match_accepts_case_and_prefix(self):
        item = {"DOI": "https://doi.org/10.1/CUR", "title": "Whatever"}
        assert connector.identity_check(
            item, expected_dois={"10.1/cur"}, arxiv_id=None, title=None,
        ) is True

    def test_arxiv_extra_accepts(self):
        item = {"DOI": "", "extra": "arXiv:2301.00001", "title": "T"}
        assert connector.identity_check(
            item, expected_dois=set(), arxiv_id="2301.00001", title=None,
        ) is True

    def test_title_fallback_when_item_has_no_doi(self):
        item = {"DOI": "", "title": "Previous Paper"}
        assert connector.identity_check(
            item, expected_dois={"10.1/cur"}, arxiv_id=None, title="A Completely Different Study",
        ) is False
        item = {"DOI": "", "title": "Housing Supply and Affordability"}
        assert connector.identity_check(
            item, expected_dois={"10.1/cur"}, arxiv_id=None, title="Housing supply and affordability.",
        ) is True

    def test_unknown_when_nothing_to_compare(self):
        assert connector.identity_check(
            {"DOI": "", "title": "X"}, expected_dois=set(), arxiv_id=None, title=None,
        ) is None


# ---------------------------------------------------------------------------
# PDF status (bug 2)
# ---------------------------------------------------------------------------

class TestPdfStatus:
    def test_local_api_404_is_unknown_not_false(self):
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with patch.object(connector.urllib.request, "urlopen", side_effect=err):
            assert connector._check_has_pdf_via_local_api("GRPKEY", api_prefix="groups/1") is None

    def test_local_api_uses_library_prefix(self):
        seen = []

        def _urlopen(req, timeout=None):
            seen.append(req.full_url)
            raise urllib.error.URLError("down")

        with patch.object(connector.urllib.request, "urlopen", side_effect=_urlopen):
            connector._check_has_pdf_via_local_api("K", api_prefix="groups/6075488")
        assert seen == ["http://127.0.0.1:23119/api/groups/6075488/items/K/children"]

    def test_sqlite_check_wins_without_network(self):
        with patch.object(connector.urllib.request, "urlopen") as urlopen:
            status = connector.check_pdf_status(
                "K", get_writer=MagicMock, timeout_s=5.0,
                has_pdf_locally=lambda key: True,
            )
        assert status == "attached"
        urlopen.assert_not_called()

    def test_404_falls_back_to_web_api(self):
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        writer = MagicMock()
        writer.check_has_pdf.return_value = True
        with patch.object(connector.urllib.request, "urlopen", side_effect=err):
            status = connector.check_pdf_status("K", get_writer=lambda: writer, timeout_s=0.1)
        assert status == "attached"


# ---------------------------------------------------------------------------
# save_single_and_verify: never duplicate, never echo another item (bugs 1, 3)
# ---------------------------------------------------------------------------

def _save(poll_result, *, find_recent_saves=None, item_data=None, doi="10.1/cur",
          title="Current Paper", risk_class="normal", delete_ok=True, fallback=None):
    fallback = fallback or MagicMock(return_value={
        "status": "saved_metadata_only", "method": "api_fallback", "item_key": "APIKEY",
        "has_pdf": False, "title": title, "action_required": None, "warning": "w",
    })
    with patch.object(connector, "enqueue_save_request", return_value=("r1", None)) as enqueue, \
         patch.object(connector, "poll_single_save_result", return_value=poll_result), \
         patch.object(connector, "_fetch_item_via_local_api",
                      side_effect=lambda key, **kw: (item_data or {}).get(key)), \
         patch.object(connector, "discover_item_via_local_api", return_value=None), \
         patch.object(connector, "delete_item_safe", return_value=delete_ok), \
         patch.object(connector, "_cleanup_publisher_tags"), \
         patch.object(connector, "apply_collection_tag_routing", return_value=None), \
         patch.object(connector, "check_pdf_status", return_value="attached"), \
         patch.object(connector, "_doi_api_fallback", fallback), \
         patch.object(connector.time, "sleep"):
        result = connector.save_single_and_verify(
            "https://doi.org/" + doi if doi else "https://example.org/p", doi, title,
            collection_key="INBOX", tags=None, bridge_url="b",
            get_writer=MagicMock, writer_lock=MagicMock(), risk_class=risk_class,
            api_prefix="groups/6075488",
            find_recent_saves=find_recent_saves,
        )
    return result, fallback, enqueue


class TestSaveSingleAndVerify:
    def test_enqueue_names_target_library(self):
        _, _, enqueue = _save(
            {"success": True, "item_key": "CUR"},
            item_data={"CUR": {"itemType": "journalArticle", "title": "Current Paper", "DOI": "10.1/cur"}},
        )
        assert enqueue.call_args.kwargs["library"] == "groups/6075488"

    def test_timeout_never_falls_back_to_api(self):
        result, fallback, _ = _save({"success": False, "status": "timeout_likely_saved", "error": "t"})
        fallback.assert_not_called()
        assert result["status"] == "saved_unconfirmed"
        assert result["item_key"] is None
        assert "do not" in result["warning"].lower()

    def test_timeout_recovers_item_found_in_target_library(self):
        result, fallback, _ = _save(
            {"success": False, "status": "timeout_likely_saved", "error": "t"},
            find_recent_saves=lambda **kw: [{"key": "CUR", "in_target": True, "library": "NAR_settlement"}],
            item_data={"CUR": {"itemType": "journalArticle", "title": "Current Paper", "DOI": "10.1/cur"}},
        )
        fallback.assert_not_called()
        assert result["status"] == "saved_with_pdf"
        assert result["item_key"] == "CUR"

    def test_item_found_in_other_library_is_reported_not_duplicated(self):
        result, fallback, _ = _save(
            {"success": False, "status": "timeout_likely_saved", "error": "t"},
            find_recent_saves=lambda **kw: [{"key": "MISFILED", "in_target": False, "library": "My Library"}],
        )
        fallback.assert_not_called()
        assert result["status"] == "saved_unconfirmed"
        assert result["item_key"] == "MISFILED"
        assert "My Library" in result["warning"]

    def test_success_without_key_never_falls_back(self):
        result, fallback, _ = _save({"success": True, "item_key": None})
        fallback.assert_not_called()
        assert result["status"] == "saved_unconfirmed"

    def test_wrong_item_key_is_not_reported(self):
        result, fallback, _ = _save(
            {"success": True, "item_key": "PREVKEY"},
            item_data={"PREVKEY": {"itemType": "journalArticle", "title": "Previous Paper", "DOI": "10.1/prev"}},
        )
        fallback.assert_not_called()
        assert result["item_key"] != "PREVKEY"
        assert result["title"] != "Previous Paper"
        assert result["status"] == "saved_unconfirmed"

    def test_wrong_item_key_replaced_by_recent_save_of_right_paper(self):
        result, _, _ = _save(
            {"success": True, "item_key": "PREVKEY"},
            find_recent_saves=lambda **kw: [{"key": "CUR", "in_target": True, "library": "NAR_settlement"}],
            item_data={
                "PREVKEY": {"itemType": "journalArticle", "title": "Previous Paper", "DOI": "10.1/prev"},
                "CUR": {"itemType": "journalArticle", "title": "Current Paper", "DOI": "10.1/cur"},
            },
        )
        assert result["item_key"] == "CUR"
        assert result["title"] == "Current Paper"

    def test_explicit_failure_uses_item_already_saved(self):
        result, fallback, _ = _save(
            {"success": False, "error_code": "save_trigger_failed", "error": "boom"},
            find_recent_saves=lambda **kw: [{"key": "CUR", "in_target": True, "library": "NAR_settlement"}],
            item_data={"CUR": {"itemType": "journalArticle", "title": "Current Paper", "DOI": "10.1/cur"}},
        )
        fallback.assert_not_called()
        assert result["item_key"] == "CUR"

    def test_explicit_failure_with_nothing_saved_falls_back_with_reason(self):
        result, fallback, _ = _save(
            {"success": False, "error_code": "save_trigger_failed", "error": "boom"},
            find_recent_saves=lambda **kw: [],
        )
        fallback.assert_called_once()
        assert "save_trigger_failed" in fallback.call_args.kwargs["reason"]

    def test_invalid_item_not_deleted_blocks_fallback(self):
        result, fallback, _ = _save(
            {"success": True, "item_key": "JUNK"},
            item_data={"JUNK": {"itemType": "journalArticle", "title": "Access Denied", "DOI": "",
                                "dateAdded": "2099-01-01T00:00:00Z"}},
            delete_ok=False,
        )
        fallback.assert_not_called()
        assert result["status"] == "failed"


    def test_invalid_item_from_before_the_save_is_left_alone(self):
        """An error-page item that predates this save is someone else's — never deleted."""
        result, fallback, _ = _save(
            {"success": True, "item_key": "OLDJUNK"},
            item_data={"OLDJUNK": {"itemType": "journalArticle", "title": "Access Denied",
                                   "DOI": "", "dateAdded": "2001-01-01T00:00:00Z"}},
        )
        assert result["status"] == "saved_unconfirmed"  # not the delete-and-replace path
        fallback.assert_not_called()


    def test_unreadable_item_is_never_deleted_or_replaced(self):
        """Local API down and Web API not synced: the found item is reported, not replaced."""
        with patch.object(connector, "validate_saved_item", return_value={
            "valid": False, "item_type": "unknown", "title": "", "reason": "validation_error:404",
        }):
            result, fallback, _ = _save(
                {"success": False, "status": "timeout_likely_saved", "error": "t"},
                find_recent_saves=lambda **kw: [{"key": "CUR", "in_target": True, "library": "G"}],
            )
        fallback.assert_not_called()
        assert result["status"] == "saved_unconfirmed"
        assert result["item_key"] == "CUR"

    def test_old_invalid_item_is_never_deleted_even_without_identity_data(self):
        """No DOI or title to compare: an invalid item still must be new to be deleted."""
        result, fallback, _ = _save(
            {"success": True, "item_key": "OLDWEB"},
            doi=None, title=None,
            item_data={"OLDWEB": {"itemType": "webpage", "title": "Some Page",
                                  "dateAdded": "2020-01-01T00:00:00Z"}},
        )
        fallback.assert_not_called()
        assert result["status"] == "saved_unconfirmed"

    def test_every_risk_class_waits_for_the_extension_deadline(self):
        with patch.object(connector, "enqueue_save_request", return_value=("r1", None)), \
             patch.object(connector, "poll_single_save_result",
                          return_value={"success": False, "status": "timeout_likely_saved"}) as poll, \
             patch.object(connector, "discover_item_via_local_api", return_value=None), \
             patch.object(connector.time, "sleep"):
            connector.save_single_and_verify(
                "https://doi.org/10.1016/j.x", "10.1016/j.x", "T",
                collection_key=None, tags=None, bridge_url="b",
                get_writer=MagicMock, writer_lock=MagicMock(), risk_class="manual_verification",
            )
        assert poll.call_args.kwargs["timeout_s"] == connector.CONNECTOR_SAVE_DEADLINE_S


class TestDoiApiFallback:
    def test_status_follows_has_pdf_and_warning_states_reason(self):
        with patch.object(connector, "save_via_api", return_value={
            "success": True, "item_key": "K", "title": "T", "pdf": True,
        }):
            result = connector._doi_api_fallback(
                "10.1/x", "T", collection_key=None, tags=None,
                get_writer=MagicMock, writer_lock=None, reason="connector offline",
            )
        assert result["status"] == "saved_with_pdf"
        assert "connector offline" in result["warning"]


class TestPublisherTagCleanup:
    def test_keeps_manual_tags(self):
        writer = MagicMock()
        writer._zot.item.return_value = {"data": {"tags": [
            {"tag": "Economics", "type": 1},
            {"tag": "my-topic"},
            {"tag": "reviewed", "type": 0},
        ]}}
        connector._cleanup_publisher_tags("K", "u", writer, MagicMock())
        writer.set_item_tags.assert_called_once_with("K", ["my-topic", "reviewed"])

    def test_no_automatic_tags_no_write(self):
        writer = MagicMock()
        writer._zot.item.return_value = {"data": {"tags": [{"tag": "mine"}]}}
        connector._cleanup_publisher_tags("K", "u", writer, MagicMock())
        writer.set_item_tags.assert_not_called()


# ---------------------------------------------------------------------------
# ingest_by_identifiers
# ---------------------------------------------------------------------------

@pytest.fixture
def ingest_env(monkeypatch):
    ingestion_tool._RECENT_SAVES.clear()
    ingestion_tool._PREFLIGHT_PASSES.clear()
    zotero = MagicMock()
    zotero.get_item_key_by_doi.return_value = None
    zotero.get_item_key_by_arxiv_id.return_value = None
    zotero.item_key_exists.return_value = True
    monkeypatch.setattr(ingestion_tool, "_ensure_inbox_collection", lambda *a, **k: "INBOX")
    monkeypatch.setattr(ingestion_tool, "_get_zotero", lambda: zotero)
    monkeypatch.setattr(ingestion_tool, "_get_writer", lambda: MagicMock())
    monkeypatch.setattr(ingestion_tool, "_resolve_target", lambda library: USER)
    monkeypatch.setattr(
        ingestion_tool.connector, "check_connector_availability",
        lambda *a, **k: (True, None, None),
    )
    monkeypatch.setattr(
        ingestion_tool.connector, "run_preflight_check",
        lambda candidates, *a, **k: (list(candidates), [], None, []),
    )
    monkeypatch.setattr(
        ingestion_tool.connector, "get_selected_zotero_library",
        lambda: {"libraryID": 1, "libraryName": "My Library", "editable": True},
    )
    return zotero


def test_results_come_back_in_input_order(ingest_env, monkeypatch):
    monkeypatch.setattr(ingestion_tool.connector, "save_single_and_verify", lambda url, doi, title, **kw: {
        "status": "saved_metadata_only", "method": "connector", "item_key": f"K-{doi}",
        "has_pdf": False, "title": title, "action_required": None, "warning": None,
    })
    ingest_env.get_item_key_by_doi.side_effect = lambda doi: "DUP" if doi == "10.1000/c" else None
    result = ingestion_tool.ingest_by_identifiers(candidates=[
        IngestCandidate(doi="10.1016/a", title="A", publisher="Elsevier BV"),  # runs first (manual)
        IngestCandidate(doi="10.1000/b", title="B", is_oa_published=True),
        IngestCandidate(doi="10.1000/c", title="C", is_oa_published=True),        # duplicate
    ])
    assert [row["candidate_index"] for row in result["results"]] == [0, 1, 2]


def test_selected_library_mismatch_blocks_connector_saves(ingest_env, monkeypatch):
    save = MagicMock()
    monkeypatch.setattr(ingestion_tool.connector, "save_single_and_verify", save)
    monkeypatch.setattr(ingestion_tool, "_resolve_target", lambda library: GROUP)
    group_client = MagicMock()
    group_client.get_item_key_by_doi.return_value = None
    group_client.get_item_key_by_arxiv_id.return_value = None
    monkeypatch.setattr(ingestion_tool, "_zotero_for_target", lambda target: group_client)
    monkeypatch.setattr(ingestion_tool, "_writer_for_target", lambda target: MagicMock())

    result = ingestion_tool.ingest_by_identifiers(
        candidates=[IngestCandidate(doi="10.1000/b", title="B", is_oa_published=True)],
        library="NAR_settlement",
    )
    save.assert_not_called()
    assert result["results"][0]["status"] == "blocked"
    assert result["results"][0]["error"] == "zotero_library_mismatch"
    action = result["action_required"][0]
    assert action["type"] == "select_zotero_library"
    assert "NAR_settlement" in action["message"] and "My Library" in action["message"]


def test_group_target_passes_library_through_to_save(ingest_env, monkeypatch):
    save = MagicMock(return_value={
        "status": "saved_with_pdf", "method": "connector", "item_key": "G1",
        "has_pdf": True, "title": "B", "action_required": None, "warning": None,
    })
    monkeypatch.setattr(ingestion_tool.connector, "save_single_and_verify", save)
    monkeypatch.setattr(ingestion_tool, "_resolve_target", lambda library: GROUP)
    group_client = MagicMock()
    group_client.get_item_key_by_doi.return_value = None
    group_client.get_item_key_by_arxiv_id.return_value = None
    monkeypatch.setattr(ingestion_tool, "_zotero_for_target", lambda target: group_client)
    group_writer = MagicMock()
    monkeypatch.setattr(ingestion_tool, "_writer_for_target", lambda target: group_writer)
    monkeypatch.setattr(
        ingestion_tool.connector, "get_selected_zotero_library",
        lambda: {"libraryID": 8, "libraryName": "NAR_settlement", "editable": True},
    )

    result = ingestion_tool.ingest_by_identifiers(
        candidates=[IngestCandidate(doi="10.1000/b", title="B", is_oa_published=True)],
        library="NAR_settlement",
    )
    assert result["results"][0]["status"] == "saved_with_pdf"
    kwargs = save.call_args.kwargs
    assert kwargs["api_prefix"] == "groups/6075488"
    assert kwargs["get_writer"]() is group_writer
    group_client.get_item_key_by_doi.assert_called()  # dedup ran against the group
    ingest_env.get_item_key_by_doi.assert_not_called()


def test_saved_unconfirmed_raises_action_and_is_not_completed(ingest_env, monkeypatch):
    monkeypatch.setattr(ingestion_tool.connector, "save_single_and_verify", lambda url, doi, title, **kw: {
        "status": "saved_unconfirmed", "method": "connector", "item_key": None,
        "has_pdf": False, "title": title, "action_required": None,
        "warning": "Check Zotero; do not re-ingest.",
    })
    result = ingestion_tool.ingest_by_identifiers(
        candidates=[IngestCandidate(doi="10.1000/b", title="B", is_oa_published=True)],
    )
    assert result["completed_count"] == 0
    assert result["action_required"][0]["type"] == "save_unconfirmed"


# ---------------------------------------------------------------------------
# Recent-saves cache (bug 4)
# ---------------------------------------------------------------------------

def test_recent_save_of_deleted_item_is_forgotten():
    ingestion_tool._RECENT_SAVES.clear()
    zotero = MagicMock(library_id=8)
    ingestion_tool._remember_recent_save("10.1/x", "DELETEDKEY", zotero=zotero)
    zotero.get_item_key_by_doi.return_value = None
    zotero.item_key_exists.return_value = False
    assert ingestion_tool._lookup_local_item_key_by_doi("10.1/x", zotero=zotero) is None
    assert ingestion_tool._RECENT_SAVES == {}


def test_recent_save_still_present_is_returned():
    ingestion_tool._RECENT_SAVES.clear()
    zotero = MagicMock(library_id=8)
    ingestion_tool._remember_recent_save("10.1/x", "LIVEKEY", zotero=zotero)
    zotero.get_item_key_by_doi.return_value = None
    zotero.item_key_exists.return_value = True
    assert ingestion_tool._lookup_local_item_key_by_doi("10.1/x", zotero=zotero) == "LIVEKEY"
    ingestion_tool._RECENT_SAVES.clear()


def test_recent_saves_are_per_library():
    ingestion_tool._RECENT_SAVES.clear()
    user_client = MagicMock(library_id=1)
    group_client = MagicMock(library_id=8)
    for client in (user_client, group_client):
        client.get_item_key_by_doi.return_value = None
        client.item_key_exists.return_value = True
    ingestion_tool._remember_recent_save("10.1/x", "USERKEY", zotero=user_client)
    assert ingestion_tool._lookup_local_item_key_by_doi("10.1/x", zotero=group_client) is None
    ingestion_tool._RECENT_SAVES.clear()


def test_find_recent_saves_marks_target_library(tmp_path):
    client = ZoteroClient(_make_db(tmp_path), library_id=8)
    names = {1: "My Library", 8: "NAR_settlement"}
    since = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    rows = ingestion_tool._find_recent_saves_in(
        client, names, dois=["10.1/x"], arxiv_id=None, since=since,
    )
    assert rows == [{"key": "NEWGRP", "in_target": True, "library": "NAR_settlement"}]


def test_personal_target_ignores_a_group_override(monkeypatch, tmp_path):
    """library=None means My Library even when the shared client/writer point at a group."""
    group_client = ZoteroClient(_make_db(tmp_path), library_id=8)
    group_writer = MagicMock()
    group_writer._zot.library_type = "groups"
    monkeypatch.setattr(ingestion_tool, "_get_zotero", lambda: group_client)
    monkeypatch.setattr(ingestion_tool, "_get_writer", lambda: group_writer)
    monkeypatch.setattr(ingestion_tool, "_get_config", lambda: MagicMock(
        zotero_data_dir=tmp_path, zotero_user_id="42", zotero_api_key="k"))
    ingestion_tool._clear_target_caches()
    try:
        target = ingestion_tool._resolve_target(None)
        assert ingestion_tool._zotero_for_target(target).library_id == 1
        writer = ingestion_tool._writer_for_target(target)
        assert writer is not group_writer
        assert writer._zot.library_type == "users" and writer._zot.library_id == "42"
    finally:
        ingestion_tool._clear_target_caches()
