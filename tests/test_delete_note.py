"""Tests for ZoteroWriter.delete_note and the delete_note MCP tool."""
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.zotero_writer import ZoteroWriter


@pytest.fixture
def writer():
    # Bypass __init__ (which needs real API creds); inject a mock pyzotero client.
    w = ZoteroWriter.__new__(ZoteroWriter)
    w._zot = MagicMock()
    return w


def _note_item(item_type="note",
               note_html="<h1>[ZotPilot] Data (auto-extracted)</h1><p>...</p>",
               parent="PARENT1"):
    return {"key": "NOTE123",
            "data": {"itemType": item_type, "note": note_html, "parentItem": parent}}


def test_delete_note_happy(writer):
    writer._zot.item.return_value = _note_item()
    result = writer.delete_note("NOTE123")
    assert result == {"deleted": True, "note_key": "NOTE123", "parent_key": "PARENT1"}
    writer._zot.delete_item.assert_called_once()


def test_delete_note_refuses_non_note(writer):
    writer._zot.item.return_value = _note_item(item_type="journalArticle")
    result = writer.delete_note("PAPER1")
    assert result["deleted"] is False
    assert result["reason"] == "not_a_note"
    writer._zot.delete_item.assert_not_called()


def test_delete_note_requires_zotpilot_marker_by_default(writer):
    writer._zot.item.return_value = _note_item(note_html="<h1>My own note</h1><p>hi</p>")
    result = writer.delete_note("NOTE123", require_zotpilot=True)
    assert result["deleted"] is False
    assert result["reason"] == "not_a_zotpilot_note"
    writer._zot.delete_item.assert_not_called()


def test_delete_note_override_allows_any_note(writer):
    writer._zot.item.return_value = _note_item(note_html="<h1>My own note</h1><p>hi</p>")
    result = writer.delete_note("NOTE123", require_zotpilot=False)
    assert result["deleted"] is True
    writer._zot.delete_item.assert_called_once()


def test_delete_note_not_found(writer):
    writer._zot.item.side_effect = Exception("404 not found")
    result = writer.delete_note("MISSING")
    assert result["deleted"] is False
    assert result["reason"] == "not_found"
    writer._zot.delete_item.assert_not_called()


def test_delete_note_tool_delegates_to_writer():
    from zotpilot.tools import write_ops
    mock_writer = MagicMock()
    mock_writer.delete_note.return_value = {"deleted": True, "note_key": "N1", "parent_key": "P1"}
    with patch("zotpilot.tools.write_ops._get_writer", return_value=mock_writer):
        result = write_ops.delete_note("N1")
    assert result == {"deleted": True, "note_key": "N1", "parent_key": "P1"}
    mock_writer.delete_note.assert_called_once_with("N1", require_zotpilot=True)
