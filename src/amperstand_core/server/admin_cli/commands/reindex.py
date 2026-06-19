"""`amperstand-admin reindex` — rebuild the metadata sidecar from the markdown files.

The vault's source of truth is the markdown files; SQLite is a derived
index. Use this command after a restore from backup (where you brought
the markdown back but not the sidecar), or to recover from a corrupted
.store/meta.db.

For the semantic vector index, use `amperstand-admin vec-rebuild` instead
(separate command because it needs OPENAI_API_KEY to regenerate embeddings).
"""

from __future__ import annotations

import typer

from amperstand_core.server.admin_cli.config import AdminConfig
from amperstand_core.store import MarkdownStore


def run(cfg: AdminConfig) -> None:
    if not cfg.data_dir.exists():
        typer.echo(f"error: data dir does not exist: {cfg.data_dir}", err=True)
        raise typer.Exit(code=1)

    store = MarkdownStore(cfg.data_dir)
    typer.echo(f"rebuilding meta index in {cfg.data_dir}/.store/meta.db…")
    count = store.rebuild_meta_index()
    typer.echo(f"reindexed {count} docs.")
    typer.echo("note: the semantic vector index is separate; run `vec-rebuild` to refresh it.")
