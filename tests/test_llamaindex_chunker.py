# tests/test_llamaindex_chunker.py
import pytest

pytest.importorskip("llama_index.core")
pytest.importorskip("tokenizers")

from zotpilot.pdf.llamaindex_chunker import LlamaIndexChunker  # noqa: E402


def test_no_chunk_exceeds_hard_cap():
    # Dense text with long tokens to stress the tokenizer vs. chars/4 estimate.
    text = ("supercalifragilistic " * 2000) + ("∑∫∂√≈≠≤≥ " * 500)
    c = LlamaIndexChunker(chunk_size=480, overlap=60, hard_cap_tokens=512)
    chunks = c.chunk(text, pages=[], sections=[])
    tok = c._tokenizer  # the HF tokenizer instance
    for ch in chunks:
        n = len(tok.encode(ch.text).ids)
        assert n <= 512, f"chunk has {n} tokens"
    assert len(chunks) > 1


def test_chunks_carry_section_and_page_metadata():
    c = LlamaIndexChunker(chunk_size=120, overlap=20)
    chunks = c.chunk("Intro text. " * 100, pages=[], sections=[])
    assert chunks and all(hasattr(ch, "section") for ch in chunks)
