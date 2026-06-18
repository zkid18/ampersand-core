"""User-feedback capture for the newsletter classifier.

When the user deletes an email-sourced doc from the vault, we log it as a
SKIP example before the file is removed. The `amperstand-admin classifier
retrain` command later folds these into the training set so the next model
incorporates the user's judgment.

Storage: append-only JSONL at {data_dir}/.classifier/feedback.jsonl.
Each line is one example, same shape as scripts/data/labeled.jsonl.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

USER_DIR_RELPATH = Path(".classifier")
FEEDBACK_FILENAME = "feedback.jsonl"
DEFAULT_DATA_DIR = Path.home() / ".amperstand" / "vault"


def feedback_path() -> Path:
    raw = os.environ.get("AMPERSTAND_DATA_DIR")
    root = Path(raw).expanduser() if raw else DEFAULT_DATA_DIR
    return root / USER_DIR_RELPATH / FEEDBACK_FILENAME


def record_delete_as_skip(
    *,
    doc_id: str,
    source: str,
    sender: str,
    subject: str,
    body: str,
) -> None:
    """Append a SKIP example for a user-deleted email. Best-effort —
    failures here must never break the user's delete request.
    """
    if not source.startswith("email://"):
        return  # not an email; nothing to learn from this delete

    entry = {
        "id": doc_id,
        "sender": sender or "",
        "subject": subject or "",
        "body": (body or "")[:4000],
        "label": "SKIP",
        "source": "user_delete",
        "ts": int(time.time()),
    }

    try:
        path = feedback_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("classifier feedback: SKIP %s (%s)", doc_id, sender)
    except Exception as e:  # pragma: no cover — defensive, deletes must not break
        logger.warning("classifier feedback: failed to log %s: %s", doc_id, e)
