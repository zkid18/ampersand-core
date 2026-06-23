"""Streaming chat-with-vault-context endpoint.

Each request carries a working set of `doc_ids` plus the conversation history.
The router fetches each doc from the MarkdownStore, builds a system prompt
that wraps the doc bodies as `<doc id="...">` blocks, and streams the
OpenAI response back to the client as Server-Sent Events.

The system prompt instructs the model to:
- only use the provided docs as evidence
- cite as `[doc_id]` (parsed client-side into clickable links)
- say "the docs don't say" rather than invent facts

Disabled when OPENAI_API_KEY isn't set (returns 503 with a helpful message,
like the semantic-search endpoint).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from amperstand_core.server.chat_api.schemas import ChatRequest
from amperstand_core.server.vault_api.auth import require_api_key
from amperstand_core.server.vault_api.store_factory import get_store
from amperstand_core.store import MarkdownStore, NotFound

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"

# How much body text to send per doc. ~10k chars per doc, 30 docs cap → ~300k
# chars total, well under 128k tokens for gpt-4o-mini after tokenization.
PER_DOC_CHAR_BUDGET = 10_000

SYSTEM_PROMPT_TEMPLATE = """You are a research assistant answering questions \
strictly grounded in the user's personal vault docs listed below.

RULES:
- Use ONLY the docs below as evidence. If they don't answer the question, say so directly.
- When stating a fact from a doc, cite it as `[<doc_id>]` (just the bare doc_id in square brackets).
- Quote short excerpts (1-2 sentences) when they directly support a claim.
- If the docs disagree, surface the disagreement instead of picking one.
- Do not invent doc IDs. Only cite IDs that appear below.
- The docs may be in mixed languages (English, Russian, Portuguese, etc). \
Answer in whatever language the user used in their question.

DOCS IN WORKING SET ({n_docs}):

{docs_block}
"""

router = APIRouter(
    prefix="/chat",
    tags=["chat"],
    dependencies=[Depends(require_api_key)],
)


def _store_dep() -> MarkdownStore:
    return get_store()


def _require_openai() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "feature_requires_openai_key",
                "feature": "chat",
                "detail": (
                    "Chat is disabled because OPENAI_API_KEY is not set on "
                    "the server. Add it to /etc/amperstand/env and restart "
                    "amperstand-server."
                ),
            },
        )
    return key


@router.post("")
def chat(
    payload: ChatRequest,
    store: Annotated[MarkdownStore, Depends(_store_dep)],
) -> StreamingResponse:
    """Stream a chat completion grounded in `payload.doc_ids`.

    Returns Server-Sent Events: each event is `data: {"delta": "<text>"}\\n\\n`.
    Final event is `data: {"done": true}\\n\\n`. Errors stream as
    `data: {"error": "<message>"}\\n\\n` then a done event so the client
    can show them inline rather than swallowing the connection.
    """
    api_key = _require_openai()

    # Fetch each doc body. Skip silently on NotFound so a stale working set
    # in the browser doesn't 4xx the whole request. If nothing is fetchable,
    # return 400 — chatting over zero context is the user's bug, not ours.
    docs: list[tuple[str, str | None, str]] = []
    for doc_id in payload.doc_ids:
        try:
            doc = store.get(doc_id)
        except NotFound:
            logger.info("chat: doc_id %s not in vault, skipping", doc_id)
            continue
        title = doc.meta.title or doc_id
        body = (doc.body or "")[:PER_DOC_CHAR_BUDGET]
        docs.append((doc_id, title, body))

    if not docs:
        raise HTTPException(
            status_code=400,
            detail="no valid doc_ids in working set (all 404 from the vault)",
        )

    docs_block = "\n\n".join(
        f'<doc id="{doc_id}" title="{title or "(untitled)"}">\n{body}\n</doc>'
        for doc_id, title, body in docs
    )
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        n_docs=len(docs), docs_block=docs_block,
    )

    messages = [{"role": "system", "content": system_prompt}]
    for m in payload.messages:
        messages.append({"role": m.role, "content": m.content})

    model = payload.model or os.environ.get("AMPERSTAND_CHAT_MODEL") or DEFAULT_MODEL

    def event_stream():
        try:
            from openai import OpenAI
        except ImportError:
            yield 'data: {"error": "openai package not installed on the server"}\n\n'
            yield 'data: {"done": true}\n\n'
            return

        try:
            client = OpenAI(api_key=api_key)
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=payload.temperature,
                stream=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("chat: OpenAI request failed")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            yield 'data: {"done": true}\n\n'
            return

        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    yield f"data: {json.dumps({'delta': delta})}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("chat: stream interrupted")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            yield 'data: {"done": true}\n\n'

    return StreamingResponse(event_stream(), media_type="text/event-stream")
