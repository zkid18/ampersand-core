"""LLM-based re-ranking for the final stage of hybrid search.

After BM25 + vector + RRF give us ~30 candidate sections, we send them to a
small LLM with the original query and ask "rate how well each passage
answers this query, 0-10". The top-K by re-rank score is what we return.

Why bother: BM25 and vector are great at *recall* (finding plausible
candidates) but mediocre at *precision* (ordering them). A cross-encoder
or LLM judge that sees the query and the passage together can reorder
the top-30 much better than either single-vector retrieval can.

Cost model: ~1500 tokens in + ~200 tokens out per re-rank call. At
gpt-4o-mini pricing that's ~$0.001/query. Fits a personal vault budget.

Disabled by default to keep `/vault/search` responses fast for the
hybrid mode — opt in via the `rerank` flag on the request body. Also
honors `AMPERSAND_RERANK_ENABLED=0` as a kill switch.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Sequence

from ampersand_core.search.models import SearchResult

logger = logging.getLogger(__name__)

# gpt-4o-mini is the sweet spot for re-ranking: cheap enough at the
# personal-vault query rate, smart enough to grade short passages on a
# numeric scale, and stable across runs at temperature=0.
RERANK_MODEL = os.environ.get("AMPERSAND_RERANK_MODEL", "gpt-4o-mini")

# Per-candidate body cap. ~600 chars ≈ 150 tokens — long enough to be
# graded fairly, short enough that 30 candidates fit in one request.
RERANK_MAX_CHARS = 600


def rerank_enabled() -> bool:
    """Server-wide off switch. Operations can disable rerank via env var."""
    return os.environ.get("AMPERSAND_RERANK_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def rerank_with_llm(
    query: str,
    candidates: Sequence[SearchResult],
    body_lookup: Callable[[SearchResult], str],
    *,
    limit: int = 10,
    api_key: str | None = None,
    model: str | None = None,
) -> list[SearchResult]:
    """Re-rank `candidates` by LLM-judged relevance to `query`.

    `body_lookup(candidate)` returns the full text the LLM should grade
    (typically the section body fetched from the store). Errors in lookup
    fall back to the candidate's snippet so a single missing doc doesn't
    poison the whole batch.

    On any LLM error returns the first `limit` candidates in their original
    (RRF) order — never raises. The point of re-rank is to improve quality
    when it works, not to be a single point of failure.
    """
    if not candidates:
        return []
    candidates = list(candidates)

    api_key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("rerank: OPENAI_API_KEY not set; falling back to RRF order")
        return candidates[:limit]

    items = []
    for i, c in enumerate(candidates):
        try:
            body = body_lookup(c)
        except Exception:  # noqa: BLE001
            body = ""
        if not body:
            body = c.snippet or ""
        items.append({
            "id": i,
            "title": c.section_title or "",
            "path": " > ".join(c.section_path) if c.section_path else "",
            "text": body[:RERANK_MAX_CHARS],
        })

    prompt = (
        f"You are a search relevance judge. Rate each passage on how well "
        f"it answers the query, 0-10 (10 = directly answers, 5 = related, "
        f"0 = unrelated). Be strict: most passages should score below 5.\n\n"
        f"Query: {query}\n\n"
        f"Passages:\n{json.dumps(items, ensure_ascii=False)}\n\n"
        f"Return JSON: {{\"scores\": [{{\"id\": <int>, \"score\": <int>}}, ...]}}"
    )

    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("rerank: openai package not installed; returning RRF order")
        return candidates[:limit]

    try:
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model or RERANK_MODEL,
            response_format={"type": "json_object"},
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("rerank LLM call failed (%s); returning RRF order", exc)
        return candidates[:limit]

    scores_by_id = {
        int(s["id"]): float(s["score"])
        for s in parsed.get("scores", [])
        if isinstance(s, dict) and "id" in s and "score" in s
    }
    if not scores_by_id:
        logger.warning("rerank: empty scores from LLM; returning RRF order")
        return candidates[:limit]

    # Re-order. Candidates without a score keep their RRF position via the
    # secondary key (-i sorts smaller indexes first when scores tie).
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda p: (-scores_by_id.get(p[0], 0.0), p[0]))
    reranked = [
        # Surface the rerank score on the SearchResult so the UI can show
        # "why this came back first" — negative so lower=better matches
        # the rest of the codebase's score convention.
        SearchResult(
            doc_id=c.doc_id,
            section_title=c.section_title,
            section_path=c.section_path,
            snippet=c.snippet,
            score=-scores_by_id.get(i, 0.0),
        )
        for i, c in indexed[:limit]
    ]
    return reranked
