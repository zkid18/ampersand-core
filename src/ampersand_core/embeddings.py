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
EMBED_DIM = 1536
MAX_CHARS = 32_000  # ~8000 tokens at ~4 chars/token; cheap proxy
BATCH_SIZE = 100
MAX_RETRIES = 4


def _truncate(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS]


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
                resp = self._client.embeddings.create(input=texts, model=self.model)
                return [d.embedding for d in resp.data]
            except Exception as exc:  # noqa: BLE001
                # OpenAI client raises a typed RateLimitError / APIError; we
                # back off and retry transient cases. Give up on the last try.
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
