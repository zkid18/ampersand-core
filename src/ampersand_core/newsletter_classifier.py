"""Lazy-loaded TF-IDF + LogReg classifier for the newsletter filter's
tiebreaker bucket.

Used by `newsletter_filter.is_newsletter` only for ambiguous emails (has
List-Id but doesn't match a known newsletter platform domain). The heuristic
handles obvious cases for free; the classifier handles the gray zone.

Model resolution (first hit wins):
  1. {AMPERSAND_DATA_DIR}/.classifier/model.joblib  — user-retrained, takes
     precedence so `ampersand-admin classifier retrain` can override the
     bundled default without code redeploys.
  2. src/ampersand_core/models/newsletter_classifier.joblib  — bootstrap
     model that ships with the package. New installs get this for free.

If neither path works (or sklearn isn't installed), callers fall back to the
word-count heuristic — the system stays useful out-of-box.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

BUNDLED_MODEL = Path(__file__).resolve().parent / "models" / "newsletter_classifier.joblib"
USER_MODEL_RELPATH = Path(".classifier") / "model.joblib"
DEFAULT_DATA_DIR = Path.home() / ".ampersand" / "vault"
BODY_CHARS = 2000  # mirror scripts/train_classifier.py


def _user_model_path() -> Path:
    raw = os.environ.get("AMPERSAND_DATA_DIR")
    root = Path(raw).expanduser() if raw else DEFAULT_DATA_DIR
    return root / USER_MODEL_RELPATH


@lru_cache(maxsize=1)
def _load_model():
    """Load joblib pipeline lazily, exactly once per process.

    Returns None if sklearn isn't installed or no model file is reachable —
    callers should treat that as "classifier unavailable, fall back".
    """
    try:
        import joblib  # type: ignore[import-not-found]
    except ImportError:
        logger.info("classifier: joblib not installed, falling back to heuristic")
        return None

    for path in (_user_model_path(), BUNDLED_MODEL):
        if not path.exists():
            continue
        try:
            model = joblib.load(path)
            logger.info(
                "classifier: loaded %s (%.1f KB)",
                path, path.stat().st_size / 1024,
            )
            return model
        except Exception as e:
            logger.warning("classifier: load failed for %s (%s), trying next", path, e)

    logger.info("classifier: no model file found, falling back to heuristic")
    return None


def reload_model() -> None:
    """Drop the cached model so the next predict_keep() picks up a new file.

    Called by `ampersand-admin classifier retrain` after writing a new model;
    saves a watcher restart in test/dev. Production still restarts the watcher
    to free the old model's memory from the long-running process.
    """
    _load_model.cache_clear()


def _build_features(sender: str, subject: str, body: str) -> str:
    """Must match scripts/train_classifier.py:build_features exactly.

    Mirroring is not pretty but it's the only way to guarantee training and
    serving see identical inputs. If you change one, change both.
    """
    sender = (sender or "").lower()
    subject = subject or ""
    body = (body or "")[:BODY_CHARS]
    domain = sender.partition("@")[2]
    return f"{sender} {sender} {domain} {domain} {subject} {body}"


def predict_keep(sender: str, subject: str, body: str) -> bool | None:
    """Predict whether an email should be kept (True) or skipped (False).

    Returns None if the classifier isn't available so the caller can fall
    back to a heuristic instead of guessing.
    """
    model = _load_model()
    if model is None:
        return None
    feat = _build_features(sender, subject, body)
    try:
        label = model.predict([feat])[0]
    except Exception as e:
        logger.warning("classifier: predict failed (%s), falling back", e)
        return None
    return label == "KEEP"
