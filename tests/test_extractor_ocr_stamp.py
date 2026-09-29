"""A per-page download stamp must not stop OCR of a scanned PDF (regression for 2026-09-29).

A ProQuest scan carried "Reproduced with permission of the copyright owner..." as
real text on every page. That ~100 characters per page lifted every page above the
near-empty threshold, so the full-document OCR fallback never ran and the paper
was indexed as "9 pages but no text".
"""
import logging

import pymupdf

from zotpilot.pdf.extractor import _effective_native_lengths, extract_document

STAMP = (
    "Reproduced with permission of the copyright owner.  "
    "Further reproduction prohibited without permission."
)


def test_stamp_repeated_on_every_page_counts_as_no_text():
    assert _effective_native_lengths([STAMP] * 9) == [0] * 9


def test_stamp_with_page_numbers_counts_as_no_text():
    texts = [f"{STAMP}\nDownloaded page {n}" for n in range(1, 6)]
    assert _effective_native_lengths(texts) == [0] * 5


def test_running_header_does_not_hide_body_text():
    body = "Real sentence of body text. " * 40
    texts = [f"Journal of Things, Vol. 11, p. {n}\n{body}{chr(65 + n)}" for n in range(6)]
    lengths = _effective_native_lengths(texts)
    assert all(n > len(body.strip()) for n in lengths)


def test_single_page_text_is_never_treated_as_boilerplate():
    assert _effective_native_lengths([STAMP]) == [len(STAMP)]


def test_line_on_only_some_pages_is_kept():
    texts = ["Chapter heading"] + ["x" * 30] * 4
    assert _effective_native_lengths(texts)[0] == len("Chapter heading")


def test_stamped_scan_triggers_full_document_ocr(tmp_path, caplog):
    pdf = tmp_path / "stamped_scan.pdf"
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page(width=200, height=200)
        page.insert_text((10, 190), STAMP, fontsize=3)
    doc.save(pdf)
    doc.close()

    with caplog.at_level(logging.INFO, logger="zotpilot.pdf.extractor"):
        extract_document(pdf)

    assert "ocr_fallback=yes" in caplog.text
