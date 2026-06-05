"""Pydantic request/response models for the vault API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateDocRequest(BaseModel):
    body: str
    frontmatter: dict[str, Any] | None = None


class UpdateDocRequest(BaseModel):
    body: str
    frontmatter: dict[str, Any] | None = None


class DocMetaResponse(BaseModel):
    id: str
    path: str
    title: str | None = None
    source: str | None = None
    content_type: str | None = None
    captured_at: datetime
    updated_at: datetime
    tags: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    content_hash: str


class DocResponse(DocMetaResponse):
    body: str


class AssetResponse(BaseModel):
    """Returned after attaching an asset to a doc."""

    doc_id: str
    link: str  # markdown-relative link to embed in the doc body


class ListResponse(BaseModel):
    items: list[DocMetaResponse]
    next_cursor: str | None = None


class SearchRequest(BaseModel):
    q: str
    limit: int = Field(default=20, ge=1, le=100)
    # `any`        — OR-joins every word, BM25 ranks. Friendliest UX.
    # `fts`        — power-user, passes the query verbatim (AND default,
    #                supports `foo OR bar`, `"phrase"`, `prefix*`).
    # `substring`  — every token treated as a literal phrase (no operators).
    # `semantic`   — embed the query, KNN over section vectors. Catches
    #                conceptual matches keywords miss; needs OPENAI_API_KEY
    #                + a populated vec_index (run `ampersand-admin vec-rebuild`).
    # `hybrid`     — combine FTS BM25 and semantic via reciprocal rank
    #                fusion. Best general-purpose mode when both indexes
    #                are available.
    mode: str = Field(default="any", pattern=r"^(any|fts|substring|semantic|hybrid)$")


class SearchHit(BaseModel):
    doc_id: str
    section_title: str | None = None
    section_path: list[str] = Field(default_factory=list)
    snippet: str
    score: float


class SearchResponse(BaseModel):
    results: list[SearchHit] = Field(default_factory=list)


class SemanticSearchRequest(BaseModel):
    """Dedicated body for POST /vault/search/semantic — same as a SearchRequest
    with mode pinned to semantic (no mode field, fewer ways to misuse)."""

    q: str
    limit: int = Field(default=20, ge=1, le=100)


class HybridSearchRequest(BaseModel):
    """Dedicated body for POST /vault/search/hybrid."""

    q: str
    limit: int = Field(default=20, ge=1, le=100)
    rerank: bool = Field(
        default=False,
        description=(
            "Adds an LLM re-rank stage after RRF fusion. Expands the candidate "
            "pool, asks gpt-4o-mini to score relevance, returns top `limit` by "
            "LLM judgement. +1-2s latency, ~$0.001/query. Server-wide kill "
            "switch: AMPERSAND_RERANK_ENABLED=0."
        ),
    )
