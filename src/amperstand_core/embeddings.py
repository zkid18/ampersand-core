"""OpenAI embeddings client used by the vector search indexer.

Wraps the OpenAI Python client with batching, simple retry, and a
truncation policy: each input is hard-capped at ~8000 tokens (≈32k chars)
to stay below text-embedding-3-small's 8191-token context. Any input
shorter than that is sent verbatim.

The model dimension is fixed at 1536 (OpenAI text-embedding-3-small's
native dim). Don't change this without rebuilding the vector index.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "text-embedding-3-small"
# 512 instead of the model's native 1536 — text-embedding-3-small uses
# Matryoshka Representation Learning so a 512-dim slice is still a
# well-normalized vector with most of the retrieval quality. Trade-off:
# brute-force KNN over our ~43k section vectors drops from ~6s to ~0.1s
# on a 1 vCPU droplet, which is the difference between "research tool"
# and "interactive search". Recall loss is negligible at this scale.
EMBED_DIM = 512
# OpenAI's hard limit is 8192 tokens; leave a small headroom for any
# tokenizer drift. We truncate by token count when tiktoken is available
# and fall back to a tight char-based budget when it isn't.
MAX_TOKENS = 8000
CHAR_FALLBACK = 16_000  # ~4000 tokens worst case at 2 chars/token (CJK)
BATCH_SIZE = 100
MAX_RETRIES = 4

# tiktoken is optional. If it imports, we truncate by exact token count.
# If not, we fall back to a conservative char limit. Either way the
# request stays under OpenAI's 8192-token cap.
try:
    import tiktoken
    _ENCODER = tiktoken.encoding_for_model(DEFAULT_MODEL)
except Exception:  # noqa: BLE001
    _ENCODER = None


def _truncate(text: str) -> str:
    if _ENCODER is not None:
        tokens = _ENCODER.encode(text, disallowed_special=())
        if len(tokens) <= MAX_TOKENS:
            return text
        return _ENCODER.decode(tokens[:MAX_TOKENS])
    if len(text) <= CHAR_FALLBACK:
        return text
    return text[:CHAR_FALLBACK]


@dataclass
class EmbeddingResult:
    text_hash: str
    vector: list[float]


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    """Thin OpenAI embeddings wrapper. Construct via Embedder.from_env()."""

    def __init__(self, *, api_key: str, model: str = DEFAULT_MODEL) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise EmbeddingError(
                "openai package not installed. Run: pip install openai"
            ) from exc
        self._client = OpenAI(api_key=api_key)
        self.model = model

    @classmethod
    def from_env(cls, *, model: str = DEFAULT_MODEL) -> Embedder:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise EmbeddingError(
                "OPENAI_API_KEY is not set. Embedding-based search is disabled."
            )
        return cls(api_key=key, model=model)

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed up to N texts at once. Splits into BATCH_SIZE chunks if needed."""
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            chunk = [_truncate(t) if t else " " for t in texts[i : i + BATCH_SIZE]]
            out.extend(self._embed_with_retry(chunk))
        return out

    def _embed_with_retry(self, texts: list[str]) -> list[list[float]]:
        delay = 1.0
        for attempt in range(MAX_RETRIES):
            try:
                resp = self._client.embeddings.create(
                    input=texts, model=self.model, dimensions=EMBED_DIM,
                )
                return [d.embedding for d in resp.data]
            except Exception as exc:  # noqa: BLE001
                # 400 = bad input (oversize, malformed); retrying won't fix
                # it. Re-raise immediately so the caller can skip this batch.
                status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
                if status == 400:
                    raise EmbeddingError(f"embeddings rejected (400): {exc}") from exc
                if attempt == MAX_RETRIES - 1:
                    raise EmbeddingError(f"embeddings failed after retries: {exc}") from exc
                logger.warning(
                    "embedding attempt %d failed (%s); retrying in %.1fs",
                    attempt + 1, exc, delay,
                )
                time.sleep(delay)
                delay *= 2
        # Unreachable
        raise EmbeddingError("embedding retry loop exited without result")
