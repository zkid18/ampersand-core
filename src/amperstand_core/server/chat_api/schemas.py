"""Request models for the chat API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str = Field(pattern=r"^(user|assistant|system)$")
    content: str


class ChatRequest(BaseModel):
    """A grounded chat turn.

    `doc_ids` is the working set — the user's selection of vault docs that
    should bound the answer. An empty working set is rejected (200 with no
    context would just be a vanilla OpenAI passthrough, which the user
    didn't ask for).
    """

    messages: list[ChatMessage]
    doc_ids: list[str] = Field(min_length=1, max_length=30)
    model: str | None = Field(
        default=None,
        description="OpenAI model (defaults to AMPERSTAND_CHAT_MODEL env or gpt-4o-mini)",
    )
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
