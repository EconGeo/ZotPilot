# tests/test_embedding_truncation.py
"""Embedding inputs over the model token budget are truncated, not failed."""
import httpx
import pytest

from zotpilot.embeddings.base import truncate_to_token_budget
from zotpilot.embeddings.ollama import OllamaEmbedder


def test_truncate_to_token_budget_caps_length():
    text = "word " * 10_000  # ~50k chars
    out = truncate_to_token_budget(text, max_tokens=512, est_chars_per_token=3)
    assert len(out) <= 512 * 3
    assert out  # non-empty


def _make_response(status_code, json_body, url="http://localhost:11434/api/embed"):
    """Build an httpx.Response with a bound Request (required by httpx >= 0.27)."""
    req = httpx.Request("POST", url)
    resp = httpx.Response(status_code, json=json_body, request=req)
    return resp


def test_ollama_embed_truncates_oversized_input(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["inputs"] = json["input"]
        n = len(json["input"])
        return _make_response(200, {"embeddings": [[0.0] * 1024 for _ in range(n)]}, url=url)

    monkeypatch.setattr("zotpilot.embeddings.ollama.httpx.post", fake_post)
    emb = OllamaEmbedder(model="bge-large", dimensions=1024)
    huge = "token " * 20_000
    vecs = emb.embed([huge])
    assert len(vecs) == 1 and len(vecs[0]) == 1024
    # the text actually sent must be within the conservative char budget
    assert all(len(t) <= emb.max_input_tokens * 3 for t in captured["inputs"])


def test_ollama_embed_subbatches(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(len(json["input"]))
        n = len(json["input"])
        return _make_response(200, {"embeddings": [[0.0] * 1024 for _ in range(n)]}, url=url)

    monkeypatch.setattr("zotpilot.embeddings.ollama.httpx.post", fake_post)
    emb = OllamaEmbedder(model="bge-large", dimensions=1024)
    emb.embed_batch_size = 4
    vecs = emb.embed(["x"] * 10)
    assert len(vecs) == 10
    assert calls == [4, 4, 2]  # sub-batched, not one giant request
