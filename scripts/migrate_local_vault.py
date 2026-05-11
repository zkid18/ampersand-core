"""One-shot bulk migrator: legacy local vault folder → MarkdownStore.

Walks a directory of `.md` files written by the legacy `save_markdown()` path
(YAML frontmatter + body), parses each one, and creates a fresh doc in the
target vault via the configured backend. ULIDs are minted by the store —
existing local files have no ID, so each migrated doc gets a fresh one.

Idempotency: tracks the set of source paths already migrated in a sidecar
state file so re-running the script doesn't create duplicates.

Usage:
    # On the droplet, write directly into the local store:
    sudo -u ampersand /opt/ampersand/venv/bin/python3 \
        /opt/ampersand/ampersand-server/scripts/migrate_local_vault.py \
        --src /tmp/legacy-vault \
        --kind store --path /var/lib/ampersand/vault \
        --state /var/lib/ampersand/migrate-state.json

    # From a Mac, push over HTTP:
    python3 migrate_local_vault.py \
        --src ~/Documents/ampersand \
        --kind http --url http://68.183.29.223 --api-key-env AMPERSAND_API_KEY \
        --state /tmp/migrate-state.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from ampersand_core.backend import BackendError, build_backend


def parse_md_file(path: Path) -> tuple[dict[str, Any], str]:
    """Parse a legacy frontmatter+body markdown file.

    Returns (frontmatter_dict, body_str). Frontmatter values are passed through
    as-is. Body is everything after the closing `---\\n` fence.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text  # no frontmatter — treat the whole file as body

    rest = text[4:]  # drop leading "---\n"
    closing = rest.find("\n---\n")
    if closing == -1:
        return {}, text

    yaml_block = rest[:closing]
    body = rest[closing + len("\n---\n") :]
    if body.startswith("\n"):
        body = body[1:]

    try:
        meta = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def normalize_frontmatter(meta: dict[str, Any]) -> dict[str, Any]:
    """Translate legacy frontmatter keys into MarkdownStore's expected shape."""
    fm: dict[str, Any] = {}
    if (title := meta.get("title")):
        fm["title"] = str(title)
    if (source := meta.get("source")):
        fm["source"] = str(source)
    if (ctype := meta.get("type")):
        fm["type"] = str(ctype)

    # Legacy `captured: 'YYYY-MM-DDTHH:MM:SSZ'` → `captured_at: datetime`
    captured = meta.get("captured")
    if captured:
        if isinstance(captured, datetime):
            fm["captured_at"] = captured
        else:
            try:
                from datetime import timezone

                if isinstance(captured, str):
                    s = captured.rstrip("Z")
                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    fm["captured_at"] = dt
            except ValueError:
                pass

    if (tags := meta.get("tags")):
        if isinstance(tags, list):
            fm["tags"] = [str(t) for t in tags]
    if (author := meta.get("author")):
        fm["author"] = str(author)
    if (sender := meta.get("sender_email")):
        fm["sender_email"] = str(sender)
    return fm


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"migrated": [], "failed": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"migrated": [], "failed": []}


def save_state(state: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def migrate(
    src: Path,
    backend_cfg: dict[str, Any],
    state_path: Path,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    flush_every: int = 50,
) -> dict[str, int]:
    src = src.resolve()
    md_files = sorted(p for p in src.rglob("*.md") if not p.name.startswith("."))
    if limit is not None:
        md_files = md_files[:limit]

    state = load_state(state_path)
    migrated: set[str] = set(state.get("migrated", []))
    failed: list[dict] = list(state.get("failed", []))

    backend = build_backend(backend_cfg) if not dry_run else None

    counters = {"total": len(md_files), "skipped": 0, "created": 0, "errors": 0}
    started = time.time()

    try:
        for i, path in enumerate(md_files, start=1):
            rel = str(path.relative_to(src))
            if rel in migrated:
                counters["skipped"] += 1
                continue

            meta, body = parse_md_file(path)
            fm = normalize_frontmatter(meta)
            body_to_send = body if body.endswith("\n") else body + "\n"

            if dry_run:
                print(f"  [dry] {rel} title={fm.get('title')!r}")
                counters["created"] += 1
                continue

            try:
                doc = backend.create(body_to_send, fm)
            except BackendError as exc:
                counters["errors"] += 1
                failed.append({"path": rel, "error": str(exc)})
                if counters["errors"] % 10 == 1:
                    print(f"  ERROR ({counters['errors']}): {rel}: {exc}", file=sys.stderr)
                continue

            counters["created"] += 1
            migrated.add(rel)

            if i % flush_every == 0:
                state["migrated"] = sorted(migrated)
                state["failed"] = failed
                save_state(state, state_path)
                elapsed = time.time() - started
                rate = i / elapsed if elapsed > 0 else 0
                eta = (counters["total"] - i) / rate if rate > 0 else 0
                print(
                    f"  {i}/{counters['total']}  "
                    f"created={counters['created']} skipped={counters['skipped']} "
                    f"errors={counters['errors']}  "
                    f"rate={rate:.1f}/s eta={eta/60:.1f}min",
                    file=sys.stderr,
                )
    finally:
        if not dry_run:
            state["migrated"] = sorted(migrated)
            state["failed"] = failed
            save_state(state, state_path)
            try:
                backend.close()
            except Exception:
                pass

    return counters


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--src", type=Path, required=True, help="Local vault dir to migrate")
    p.add_argument(
        "--kind",
        choices=["store", "http"],
        required=True,
        help="Backend kind. 'store' = local MarkdownStore (run on the droplet); 'http' = remote",
    )
    p.add_argument("--path", type=Path, help="For --kind=store: vault data dir")
    p.add_argument("--url", help="For --kind=http: server URL")
    p.add_argument("--api-key", help="For --kind=http: bearer token (or use --api-key-env)")
    p.add_argument("--api-key-env", help="For --kind=http: env var name holding bearer")
    p.add_argument(
        "--state",
        type=Path,
        default=Path("./migrate-state.json"),
        help="Sidecar state file for resumability",
    )
    p.add_argument("--limit", type=int, help="Stop after N files (for testing)")
    p.add_argument("--dry-run", action="store_true", help="Walk + parse, don't write")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.kind == "store":
        if not args.path:
            print("--path is required for --kind=store", file=sys.stderr)
            return 2
        cfg = {"kind": "store", "store": {"path": str(args.path)}}
    else:
        if not args.url:
            print("--url is required for --kind=http", file=sys.stderr)
            return 2
        if not args.api_key and not args.api_key_env:
            print("Provide --api-key or --api-key-env", file=sys.stderr)
            return 2
        cfg = {"kind": "http", "http": {"url": args.url}}
        if args.api_key:
            cfg["http"]["api_key"] = args.api_key
        if args.api_key_env:
            cfg["http"]["api_key_env"] = args.api_key_env

    print(f"src:   {args.src}", file=sys.stderr)
    print(f"kind:  {args.kind}", file=sys.stderr)
    print(f"state: {args.state}", file=sys.stderr)
    if args.dry_run:
        print("(dry-run — no writes)", file=sys.stderr)

    counters = migrate(
        src=args.src,
        backend_cfg=cfg,
        state_path=args.state,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    print("", file=sys.stderr)
    print("done:", file=sys.stderr)
    for k, v in counters.items():
        print(f"  {k:8} {v}", file=sys.stderr)
    return 0 if counters["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
