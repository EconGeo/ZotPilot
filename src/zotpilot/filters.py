"""ChromaDB filter builders and post-retrieval text filters."""

VALID_CHUNK_TYPES = {"text", "figure", "table"}


_SCOPE_LIMIT = 1_000_000


def _resolve_scope_doc_ids(
    zotero,
    author: str | None = None,
    tag: str | None = None,
    collection: str | None = None,
) -> set[str] | None:
    """Resolve author/tag/collection substring filters to live Zotero item keys.

    ChromaDB cannot do substring matching, and post-filtering the top-K of a
    whole-library search drops nearly every hit for a small scope. Resolving the
    filters against the live Zotero database first lets the scope go into the
    ChromaDB where clause as ``doc_id $in``, and reflects current collection and
    tag membership rather than the metadata frozen at index time.

    Returns:
        None when no text filter is set; otherwise the matching item keys
        (possibly empty).
    """
    if not _has_text_filters(author, tag, collection):
        return None
    conditions = [
        {"field": field, "op": "contains", "value": value}
        for field, value in (("author", author), ("tag", tag), ("collection", collection))
        if value
    ]
    rows = zotero.advanced_search(conditions=conditions, match="all", limit=_SCOPE_LIMIT)
    return {row["doc_id"] for row in rows if row.get("doc_id")}


def _build_chromadb_filters(
    year_min: int | None = None,
    year_max: int | None = None,
    chunk_types: list[str] | None = None,
    doc_ids: set[str] | None = None,
) -> dict | None:
    """Build ChromaDB where clause for year range, chunk_type and doc_id scope.

    IMPORTANT: ChromaDB only supports: $eq, $ne, $gt, $gte, $lt, $lte, $in, $nin
    It does NOT support substring/contains operations on metadata.
    Text-based filters (author, tag, collection) are resolved to doc_ids first
    with _resolve_scope_doc_ids() and passed here as ``doc_ids``.

    Args:
        year_min: Minimum publication year
        year_max: Maximum publication year
        chunk_types: Filter to specific chunk types (text, figure, table)
        doc_ids: Restrict to these document IDs (None = no restriction)

    Returns:
        ChromaDB where clause dict, or None if no filters
    """
    conditions = []
    if doc_ids is not None:
        conditions.append({"doc_id": {"$in": sorted(doc_ids)}})
    if year_min:
        conditions.append({"year": {"$gte": year_min}})
    if year_max:
        conditions.append({"year": {"$lte": year_max}})
    if chunk_types:
        if len(chunk_types) == 1:
            conditions.append({"chunk_type": {"$eq": chunk_types[0]}})
        else:
            conditions.append({"chunk_type": {"$in": chunk_types}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _meta_get(r, key: str, default: str = "") -> str:
    """Get a metadata field from StoredChunk (.metadata dict) or RetrievalResult (attrs)."""
    if hasattr(r, "metadata") and isinstance(r.metadata, dict):
        return r.metadata.get(key, default)
    return getattr(r, key, default)


def _apply_text_filters(
    results: list,
    author: str | None = None,
    tag: str | None = None,
    collection: str | None = None,
) -> list:
    """Apply substring-based filters in Python (post-retrieval).

    ChromaDB doesn't support substring matching, so we filter after retrieval.
    All matches are case-insensitive substrings.

    Works with both StoredChunk (metadata dict) and RetrievalResult (dataclass attrs).

    Args:
        results: List of StoredChunk or RetrievalResult objects
        author: Author name substring (case-insensitive)
        tag: Tag substring (case-insensitive)
        collection: Collection name substring (case-insensitive)

    Returns:
        Filtered list
    """
    if not author and not tag and not collection:
        return results

    author_lower = author.lower() if author else None
    tag_lower = tag.lower() if tag else None
    collection_lower = collection.lower() if collection else None

    filtered = []
    for r in results:
        if author_lower:
            authors = _meta_get(r, "authors", "").lower()
            if author_lower not in authors:
                continue

        if tag_lower:
            tags = _meta_get(r, "tags", "").lower()
            if tag_lower not in tags:
                continue

        if collection_lower:
            colls = _meta_get(r, "collections", "").lower()
            if collection_lower not in colls:
                continue

        filtered.append(r)

    return filtered


def _has_text_filters(author: str | None, tag: str | None, collection: str | None) -> bool:
    """Check if any text-based filters are active."""
    return bool(author or tag or collection)


def _apply_required_terms(results: list, terms: list[str]) -> list:
    """Filter results to only those containing all required terms as whole words.

    Case-insensitive. Checks the passage text (and full_context if available).
    """
    import re
    patterns = [re.compile(r'\b' + re.escape(t) + r'\b', re.IGNORECASE) for t in terms]

    filtered = []
    for r in results:
        text = getattr(r, 'text', '') or ''
        full_ctx = r.full_context() if hasattr(r, 'full_context') and callable(getattr(r, 'full_context', None)) else ''
        combined = text + ' ' + full_ctx
        if all(p.search(combined) for p in patterns):
            filtered.append(r)
    return filtered
