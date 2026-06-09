"""Domain-list config loader for the newsletter filter.

Lists live in YAML (data/newsletter_domains.yaml in the package) so the
user can edit them without touching code. User overrides at
`{AMPERSAND_DATA_DIR}/.classifier/newsletter_domains.yaml` are merged
ADDITIVELY into the bundled defaults — you can add domains, not remove.
To drop a bundled default, edit the bundled file and redeploy.

The loader is cached per-process. Edits require a restart to take effect.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

BUNDLED_YAML = Path(__file__).resolve().parent / "data" / "newsletter_domains.yaml"
USER_YAML_RELPATH = Path(".classifier") / "newsletter_domains.yaml"
DEFAULT_DATA_DIR = Path.home() / ".ampersand" / "vault"


def _user_yaml_path() -> Path:
    raw = os.environ.get("AMPERSAND_DATA_DIR")
    root = Path(raw).expanduser() if raw else DEFAULT_DATA_DIR
    return root / USER_YAML_RELPATH


@dataclass(frozen=True)
class DomainLists:
    """Effective domain lists + per-entry provenance for `domains` admin command.

    `sources` maps each domain → "bundled" or "user" so the admin command
    can show the user where each entry was loaded from.
    """

    newsletter_domains: frozenset[str]
    promo_domains: frozenset[str]
    sources: dict[str, str] = field(default_factory=dict)
    bundled_path: Path = field(default_factory=lambda: BUNDLED_YAML)
    user_path: Path | None = None


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("newsletter_domains: failed to parse %s (%s)", path, e)
        return {}
    if not isinstance(data, dict):
        logger.warning("newsletter_domains: %s isn't a mapping, ignoring", path)
        return {}
    return data


def _normalize(values) -> list[str]:
    if not values:
        return []
    if not isinstance(values, list):
        logger.warning("newsletter_domains: expected list, got %r", type(values))
        return []
    return [str(v).strip().lower() for v in values if str(v).strip()]


@lru_cache(maxsize=1)
def load_domains() -> DomainLists:
    """Build the effective domain lists from bundled + user YAML.

    Cached for the life of the process. Call `reload()` to force a refresh
    (used by tests; production picks up edits on watcher/server restart).
    """
    bundled = _read_yaml(BUNDLED_YAML)
    user_path = _user_yaml_path()
    user = _read_yaml(user_path) if user_path.exists() else {}

    sources: dict[str, str] = {}
    newsletters: set[str] = set()
    promos: set[str] = set()

    for d in _normalize(bundled.get("newsletter_domains")):
        newsletters.add(d)
        sources[f"newsletter:{d}"] = "bundled"
    for d in _normalize(bundled.get("promo_domains")):
        promos.add(d)
        sources[f"promo:{d}"] = "bundled"
    for d in _normalize(user.get("newsletter_domains")):
        if d not in newsletters:
            sources[f"newsletter:{d}"] = "user"
        newsletters.add(d)
    for d in _normalize(user.get("promo_domains")):
        if d not in promos:
            sources[f"promo:{d}"] = "user"
        promos.add(d)

    return DomainLists(
        newsletter_domains=frozenset(newsletters),
        promo_domains=frozenset(promos),
        sources=sources,
        bundled_path=BUNDLED_YAML,
        user_path=user_path if user_path.exists() else None,
    )


def reload() -> None:
    """Drop the cached config so the next load_domains() re-reads files."""
    load_domains.cache_clear()
