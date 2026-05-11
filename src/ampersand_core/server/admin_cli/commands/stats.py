"""`ampersand-admin stats` — vault size, doc count, last write, disk free."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer

from ampersand_core.store import MarkdownStore

from ampersand_core.server.admin_cli.config import AdminConfig


def run(cfg: AdminConfig) -> None:
    typer.echo(f"data dir:    {cfg.data_dir}")
    if not cfg.data_dir.exists():
        typer.echo("status:      data dir does not exist yet")
        _print_api_key_status(cfg)
        raise typer.Exit(code=0)

    store = MarkdownStore(cfg.data_dir)
    docs = list(store.iter_all())
    total_bytes = _sum_doc_bytes(cfg.data_dir, docs)

    typer.echo(f"docs:        {len(docs)} ({_human(total_bytes)})")

    if docs:
        oldest = min(docs, key=lambda m: m.captured_at)
        newest = max(docs, key=lambda m: m.updated_at)
        typer.echo(
            f"oldest:      {_iso_short(oldest.captured_at)}  "
            f"{oldest.id[:8]}…  {_clip(oldest.title)}"
        )
        typer.echo(
            f"newest:      {_iso_short(newest.updated_at)}  "
            f"{newest.id[:8]}…  {_clip(newest.title)}"
        )

    usage = shutil.disk_usage(cfg.data_dir)
    used_pct = 100 - int(usage.free / usage.total * 100)
    typer.echo(
        f"disk:        {_human(usage.free)} free of {_human(usage.total)}  ({used_pct}% used)"
    )

    _print_api_key_status(cfg)


def _print_api_key_status(cfg: AdminConfig) -> None:
    typer.echo(f"api key:     {'set' if cfg.api_key_present else 'NOT SET'}")
    if cfg.env_file:
        suffix = "exists" if cfg.env_file_exists else "missing"
        typer.echo(f"env file:    {cfg.env_file} ({suffix})")


def _sum_doc_bytes(root: Path, docs) -> int:
    total = 0
    for meta in docs:
        try:
            total += (root / meta.path).stat().st_size
        except OSError:
            continue
    return total


def _human(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} TB"


def _iso_short(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(value: str | None, n: int = 40) -> str:
    if not value:
        return "(untitled)"
    return value if len(value) <= n else value[: n - 1] + "…"
