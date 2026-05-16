"""Chat API — RAG-style streaming chat grounded in a user-selected subset of vault docs.

Used by ampersand-notebook (the local-Mac chat UI). The notebook builds a
"working set" of doc IDs from vault search, sends them with the conversation
history, and gets back a streamed answer that cites those docs.
"""

from ampersand_core.server.chat_api.router import router

__all__ = ["router"]
