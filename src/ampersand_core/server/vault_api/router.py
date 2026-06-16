"""FastAPI router exposing the markdown vault over HTTP."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

from ampersand_core.search import (
    SearchError,
    SearchIndexer,
    SearchResult,
    VectorIndexer,
    parse_sections,
    rerank_enabled,
    rerank_with_llm,
)
from ampersand_core.classifier_feedback import record_delete_as_skip
from ampersand_core.store import (
    Conflict,
    DocMeta,
    MarkdownStore,
    NotFound,
    StoreError,
)

from ampersand_core.server.vault_api.auth import require_api_key
from ampersand_core.server.vault_api.schemas import (
    AssetResponse,
    CreateDocRequest,
    DocMetaResponse,
    DocResponse,
    HybridSearchRequest,
    ListResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SemanticSearchRequest,
    UpdateDocRequest,
)
from ampersand_core.server.vault_api.store_factory import (
    get_indexer,
    get_store,
    get_vec_indexer,
)

router = APIRouter(
    prefix="/vault",
    tags=["vault"],
    dependencies=[Depends(require_api_key)],
)


def _meta_to_response(meta: DocMeta) -> DocMetaResponse:
    return DocMetaResponse(
        id=meta.id,
        path=meta.path,
        title=meta.title,
        source=meta.source,
        # Dual-emit: canonical on-disk names + legacy snake_case_at aliases.
        # See DocMetaResponse docstring — closes PKM v2 schema-drift item.
        type=meta.content_type,
        captured=meta.captured_at,
        updated=meta.updated_at,
        content_type=meta.content_type,
        captured_at=meta.captured_at,
        updated_at=meta.updated_at,
        tags=list(meta.tags),
        extra=dict(meta.extra),
        content_hash=meta.content_hash,
        body_hash=getattr(meta, "body_hash", None),
    )


def _doc_to_response(doc) -> DocResponse:
    base = _meta_to_response(doc.meta)
    return DocResponse(**base.model_dump(), body=doc.body)


def _store_dep() -> MarkdownStore:
    return get_store()


def _indexer_dep() -> SearchIndexer:
    return get_indexer()


def _vec_indexer_dep() -> VectorIndexer | None:
    return get_vec_indexer()


def _make_body_lookup(store: MarkdownStore):
    """Build a memoized (doc_id, section_path) → body fetcher for rerank.

    Re-rank passes ~30 candidates and many often share the same source
    doc — caching the parsed section list per-doc within a single request
    avoids re-reading and re-parsing the same markdown N times.

    Falls back to the empty string on NotFound or any extraction failure,
    which `rerank_with_llm` then substitutes with the candidate's snippet.
    """
    doc_cache: dict[str, dict[tuple, str]] = {}

    def lookup(c: SearchResult) -> str:
        section_map = doc_cache.get(c.doc_id)
        if section_map is None:
            try:
                doc = store.get(c.doc_id)
            except NotFound:
                doc_cache[c.doc_id] = {}
                return ""
            sections = parse_sections(doc.body, doc_title=doc.meta.title)
            section_map = {tuple(s.path): s.body for s in sections}
            doc_cache[c.doc_id] = section_map
        return section_map.get(tuple(c.section_path), "")

    return lookup


def _rrf_fuse(
    fts_hits: list[SearchResult],
    vec_hits: list[SearchResult],
    *,
    k: int = 60,
    limit: int = 20,
) -> list[SearchResult]:
    """Reciprocal Rank Fusion of two ranked result lists.

    Standard RRF: score(item) = Σ 1 / (k + rank_i). k=60 from the original
    Cormack et al. paper. Dedupes by (doc_id, section_path) so the same
    section appearing in both lists doesn't double-count.

    The returned hits keep the snippet from the FTS side when available
    (it has highlighted markers); vec hits have empty snippets.
    """
    by_key: dict[tuple, dict] = {}

    def _key(r: SearchResult) -> tuple:
        return (r.doc_id, tuple(r.section_path))

    for rank, hit in enumerate(fts_hits):
        k_ = _key(hit)
        entry = by_key.setdefault(k_, {"hit": hit, "score": 0.0})
        entry["score"] += 1.0 / (k + rank + 1)

    for rank, hit in enumerate(vec_hits):
        k_ = _key(hit)
        entry = by_key.setdefault(k_, {"hit": hit, "score": 0.0})
        entry["score"] += 1.0 / (k + rank + 1)

    fused = sorted(by_key.values(), key=lambda e: -e["score"])
    out: list[SearchResult] = []
    for entry in fused[:limit]:
        h = entry["hit"]
        # Use a normalized fusion score (negative so "lower=better" mirrors BM25)
        out.append(SearchResult(
            doc_id=h.doc_id,
            section_title=h.section_title,
            section_path=h.section_path,
            snippet=h.snippet,
            score=-entry["score"],
        ))
    return out


@router.get("", response_model=ListResponse)
def list_docs(
    store: Annotated[MarkdownStore, Depends(_store_dep)],
    since: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    order: str = Query(default="updated", pattern="^(updated|captured)$"),
) -> ListResponse:
    try:
        page = store.list(since=since, cursor=cursor, limit=limit, order=order)
    except StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ListResponse(
        items=[_meta_to_response(m) for m in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("", response_model=DocResponse, status_code=201)
def create_doc(
    payload: CreateDocRequest,
    response: Response,
    store: Annotated[MarkdownStore, Depends(_store_dep)],
) -> DocResponse:
    doc = store.create(payload.body, payload.frontmatter)
    response.headers["ETag"] = doc.meta.content_hash
    return _doc_to_response(doc)


# Upper bound on a single asset upload (bytes). Telegram photos are well
# under this; documents can be larger but we cap to avoid filling the disk.
_MAX_ASSET_BYTES = 50 * 1024 * 1024


@router.post("/{doc_id}/assets/{filename}", response_model=AssetResponse, status_code=201)
async def add_asset(
    doc_id: str,
    filename: str,
    request: Request,
    store: Annotated[MarkdownStore, Depends(_store_dep)],
) -> AssetResponse:
    """Attach a binary asset (image/file) to a doc.

    The raw bytes are the request body (no multipart — keeps the dep
    surface small). Returns the markdown-relative link to embed in the
    doc body, e.g. `./{id}-slug/photo-01.jpg`.
    """
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty asset body")
    if len(data) > _MAX_ASSET_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"asset too large ({len(data)} bytes > {_MAX_ASSET_BYTES})",
        )
    try:
        link = store.add_asset(doc_id, filename, data)
    except NotFound:
        raise HTTPException(status_code=404, detail="doc not found")
    except StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return AssetResponse(doc_id=doc_id, link=link)


@router.get("/{doc_id}/assets/{filename}")
def get_asset(
    doc_id: str,
    filename: str,
    store: Annotated[MarkdownStore, Depends(_store_dep)],
) -> FileResponse:
    """Serve a doc's asset bytes (so the web UI can render embedded images)."""
    try:
        path = store.get_asset_path(doc_id, filename)
    except NotFound:
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(path)


@router.post("/search", response_model=SearchResponse)
def search(
    payload: SearchRequest,
    indexer: Annotated[SearchIndexer, Depends(_indexer_dep)],
    vec: Annotated[VectorIndexer | None, Depends(_vec_indexer_dep)],
) -> SearchResponse:
    mode = payload.mode
    try:
        if mode in ("any", "fts", "substring"):
            results = indexer.index.search(
                payload.q, limit=payload.limit, mode=mode
            )
        elif mode == "semantic":
            if vec is None:
                raise HTTPException(
                    status_code=503,
                    detail="semantic search disabled — set OPENAI_API_KEY and run "
                           "`ampersand-admin vec-rebuild` to populate the vector index.",
                )
            qvec = vec._embedder.embed(payload.q)
            results = vec.index.search(qvec, limit=payload.limit)
        elif mode == "hybrid":
            if vec is None:
                # Graceful degradation: hybrid silently falls back to FTS
                # rather than 503-ing. Logged elsewhere.
                results = indexer.index.search(
                    payload.q, limit=payload.limit, mode="any"
                )
            else:
                fts_hits = indexer.index.search(
                    payload.q, limit=max(payload.limit * 3, 30), mode="any"
                )
                qvec = vec._embedder.embed(payload.q)
                vec_hits = vec.index.search(
                    qvec, limit=max(payload.limit * 3, 30)
                )
                results = _rrf_fuse(fts_hits, vec_hits, limit=payload.limit)
        else:
            raise HTTPException(status_code=400, detail=f"unknown mode: {mode!r}")
    except SearchError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"search failed: {exc}")

    return SearchResponse(
        results=[
            SearchHit(
                doc_id=r.doc_id,
                section_title=r.section_title,
                section_path=r.section_path,
                snippet=r.snippet,
                score=r.score,
            )
            for r in results
        ]
    )


def _require_vec(vec: VectorIndexer | None) -> VectorIndexer:
    if vec is None:
        raise HTTPException(
            status_code=503,
            detail="semantic search disabled — set OPENAI_API_KEY and run "
                   "`ampersand-admin vec-rebuild` to populate the vector index.",
        )
    return vec


def _to_hits(results: list) -> list[SearchHit]:
    return [
        SearchHit(
            doc_id=r.doc_id,
            section_title=r.section_title,
            section_path=r.section_path,
            snippet=r.snippet,
            score=r.score,
        )
        for r in results
    ]


@router.post("/search/semantic", response_model=SearchResponse)
def search_semantic(
    payload: SemanticSearchRequest,
    vec: Annotated[VectorIndexer | None, Depends(_vec_indexer_dep)],
) -> SearchResponse:
    """Embed the query, run KNN over section vectors. Pure semantic — no
    keyword fallback. Returns 503 when the vector index isn't available."""
    v = _require_vec(vec)
    qvec = v._embedder.embed(payload.q)
    return SearchResponse(results=_to_hits(v.index.search(qvec, limit=payload.limit)))


@router.post("/search/hybrid", response_model=SearchResponse)
def search_hybrid(
    payload: HybridSearchRequest,
    store: Annotated[MarkdownStore, Depends(_store_dep)],
    indexer: Annotated[SearchIndexer, Depends(_indexer_dep)],
    vec: Annotated[VectorIndexer | None, Depends(_vec_indexer_dep)],
) -> SearchResponse:
    """Reciprocal rank fusion of FTS BM25 + vector KNN, with optional LLM
    re-rank as a final precision stage.

    Returns 503 when the vector index isn't available (no OPENAI_API_KEY,
    or the index hasn't been populated) — clients that want a BM25 fallback
    should explicitly call POST /vault/search with mode="any". Silent
    fallback was found to deceive callers about which retrieval mode they
    were actually getting.

    When `rerank=True` the candidate pool is widened to ~4× `limit`, fused,
    then graded by gpt-4o-mini and re-sorted by relevance. If rerank was
    requested but isn't available (no OpenAI key), responds 503 rather than
    silently dropping the rerank stage.
    """
    v = _require_vec(vec)
    if bool(payload.rerank) and not rerank_enabled():
        raise HTTPException(
            status_code=503,
            detail="rerank requested but disabled — set OPENAI_API_KEY to enable.",
        )
    use_rerank = bool(payload.rerank)
    # When rerank is on, fetch a fatter candidate pool so the LLM has more
    # options to reorder. ~4× limit (capped to 60) is a reasonable trade
    # between coverage and prompt size.
    pool_size = (
        max(payload.limit * 4, 30) if use_rerank else max(payload.limit * 3, 30)
    )
    pool_size = min(pool_size, 60)

    fts_hits = indexer.index.search(payload.q, limit=pool_size, mode="any")
    qvec = vec._embedder.embed(payload.q)
    vec_hits = vec.index.search(qvec, limit=pool_size)
    fused = _rrf_fuse(fts_hits, vec_hits, limit=pool_size)

    if use_rerank:
        results = rerank_with_llm(
            payload.q,
            fused,
            body_lookup=_make_body_lookup(store),
            limit=payload.limit,
        )
    else:
        results = fused[:payload.limit]
    return SearchResponse(results=_to_hits(results))


@router.get("/{doc_id}/related", response_model=SearchResponse)
def get_related(
    doc_id: str,
    vec: Annotated[VectorIndexer | None, Depends(_vec_indexer_dep)],
    limit: int = Query(default=20, ge=1, le=100),
) -> SearchResponse:
    """Return docs semantically similar to `doc_id` — KNN around the
    centroid of all of doc_id's section vectors. Same doc is excluded
    from the returned hits.

    Useful for "I just read this, find me 10 like it" — the natural
    workflow when you're triaging a research vault.
    """
    v = _require_vec(vec)
    centroid = v.index.doc_centroid(doc_id)
    if centroid is None:
        raise HTTPException(
            status_code=404,
            detail=f"doc {doc_id} has no vectors in the index "
                   f"(unknown id, or never embedded)",
        )
    # Pull extra hits so we still have ~limit after dropping the doc itself
    raw = v.index.search(centroid, limit=limit + 8)
    filtered = [h for h in raw if h.doc_id != doc_id][:limit]
    return SearchResponse(results=_to_hits(filtered))


@router.get("/{doc_id}", response_model=DocResponse)
def get_doc(
    doc_id: str,
    response: Response,
    store: Annotated[MarkdownStore, Depends(_store_dep)],
) -> DocResponse:
    try:
        doc = store.get(doc_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="doc not found")
    response.headers["ETag"] = doc.meta.content_hash
    return _doc_to_response(doc)


@router.put("/{doc_id}", response_model=DocResponse)
def upsert_doc(
    doc_id: str,
    payload: UpdateDocRequest,
    response: Response,
    store: Annotated[MarkdownStore, Depends(_store_dep)],
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> DocResponse:
    try:
        doc = store.upsert(
            doc_id,
            payload.body,
            payload.frontmatter,
            if_match=if_match,
        )
    except Conflict as exc:
        raise HTTPException(status_code=412, detail=str(exc))
    except StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    response.headers["ETag"] = doc.meta.content_hash
    return _doc_to_response(doc)


@router.delete("/{doc_id}", status_code=204, response_class=Response)
def delete_doc(
    doc_id: str,
    store: Annotated[MarkdownStore, Depends(_store_dep)],
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    # Read first so we can record the delete as classifier feedback before
    # the file is gone. Best-effort: a missing doc still 404s normally, and
    # feedback failures must never block the delete.
    feedback_payload: dict | None = None
    try:
        doc = store.get(doc_id)
        feedback_payload = {
            "doc_id": doc.meta.id,
            "source": doc.meta.source or "",
            "sender": (doc.meta.extra or {}).get("sender_email", ""),
            "subject": doc.meta.title or "",
            "body": doc.body,
        }
    except NotFound:
        pass  # delete will 404 below; nothing to record

    try:
        store.delete(doc_id, if_match=if_match)
    except NotFound:
        raise HTTPException(status_code=404, detail="doc not found")
    except Conflict as exc:
        raise HTTPException(status_code=412, detail=str(exc))

    if feedback_payload is not None:
        record_delete_as_skip(**feedback_payload)
    return Response(status_code=204)


@router.get("/{doc_id}/raw", response_class=PlainTextResponse)
def get_doc_raw(
    doc_id: str,
    response: Response,
    store: Annotated[MarkdownStore, Depends(_store_dep)],
) -> PlainTextResponse:
    try:
        doc = store.get(doc_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="doc not found")
    raw_path = store.root / doc.meta.path
    text = raw_path.read_text(encoding="utf-8")
    return PlainTextResponse(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={"ETag": doc.meta.content_hash},
    )
