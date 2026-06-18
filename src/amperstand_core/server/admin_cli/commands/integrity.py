"""`amperstand-admin integrity` — verify each doc parses, ids match, optionally rehash."""

from __future__ import annotations

from pathlib import Path

import typer

from amperstand_core.store import MarkdownStore, recompute_hash
from amperstand_core.store import frontmatter as fm
from amperstand_core.store.errors import StoreError
from amperstand_core.store.paths import docs_root, index_path

from amperstand_core.server.admin_cli.config import AdminConfig


def run(cfg: AdminConfig, deep: bool = False) -> None:
    if not cfg.data_dir.exists():
        typer.echo(f"error: data dir does not exist: {cfg.data_dir}", err=True)
        raise typer.Exit(code=1)

    docs_dir = docs_root(cfg.data_dir)
    md_files = sorted(p for p in docs_dir.rglob("*.md") if not p.name.startswith(".tmp-"))

    parseable = 0
    parse_failures: list[tuple[Path, str]] = []
    id_mismatches: list[Path] = []
    missing_indexes: list[tuple[Path, str]] = []
    hash_mismatches: list[tuple[Path, str, str]] = []

    typer.echo(f"checking {len(md_files)} docs in {cfg.data_dir}…")

    for path in md_files:
        try:
            text = path.read_text(encoding="utf-8")
            meta, _ = fm.parse(text)
        except (OSError, StoreError) as exc:
            parse_failures.append((path, str(exc)))
            continue
        parseable += 1

        doc_id = meta.get("id")
        if not doc_id or doc_id not in path.stem:
            id_mismatches.append(path)

        if doc_id:
            idx = index_path(cfg.data_dir, doc_id)
            if not idx.exists():
                missing_indexes.append((path, doc_id))

        if deep and doc_id:
            try:
                actual = recompute_hash(path)
                stored = meta.get("content_hash")
                if stored != actual:
                    hash_mismatches.append((path, str(stored), actual))
            except StoreError as exc:
                parse_failures.append((path, f"deep-hash failed: {exc}"))

    _line(f"parseable:           {parseable}/{len(md_files)}")
    _line(f"id-matches-filename: {parseable - len(id_mismatches)}/{parseable}")
    _line(f"index entries:       {parseable - len(missing_indexes)}/{parseable}")
    if deep:
        _line(f"content-hash:        {parseable - len(hash_mismatches)}/{parseable}")

    failed = parse_failures or id_mismatches or missing_indexes or hash_mismatches
    if not failed:
        typer.echo("OK")
        return

    typer.echo("")
    if parse_failures:
        typer.echo(f"parse failures ({len(parse_failures)}):")
        for path, msg in parse_failures:
            typer.echo(f"  {path}: {msg}")
    if id_mismatches:
        typer.echo(f"id mismatches ({len(id_mismatches)}):")
        for path in id_mismatches:
            typer.echo(f"  {path}")
    if missing_indexes:
        typer.echo(f"missing index entries ({len(missing_indexes)}):")
        for path, doc_id in missing_indexes:
            typer.echo(f"  {doc_id}  ({path})")
    if hash_mismatches:
        typer.echo(f"content-hash mismatches ({len(hash_mismatches)}):")
        for path, stored, actual in hash_mismatches:
            typer.echo(f"  {path}")
            typer.echo(f"    stored:   {stored}")
            typer.echo(f"    computed: {actual}")
    raise typer.Exit(code=1)


def _line(s: str) -> None:
    typer.echo(f"  {s}")
