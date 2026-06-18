"""`amperstand-admin rotate-key` — mint a new API key + write it to the env file."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile

import typer

from amperstand_core.server.admin_cli.config import AdminConfig, load_env_file


def run(cfg: AdminConfig, dry_run: bool = False) -> None:
    if cfg.env_file is None:
        typer.echo(
            "error: rotate-key needs --env-file. Pass the path to your env file "
            "(default on a deployed box: /etc/amperstand/env).",
            err=True,
        )
        raise typer.Exit(code=2)

    new_key = secrets.token_hex(32)

    if dry_run:
        typer.echo("(dry-run) would generate a new 64-char hex key and write it to:")
        typer.echo(f"  {cfg.env_file}")
        typer.echo("no files modified.")
        return

    cfg.env_file.parent.mkdir(parents=True, exist_ok=True)
    existing = load_env_file(cfg.env_file)
    existing["AMPERSTAND_API_KEY"] = new_key

    body = "".join(f"{k}={v}\n" for k, v in existing.items())
    _atomic_write(cfg.env_file, body.encode("utf-8"), mode=0o600)

    typer.echo("new api key (paste into clients now — it will not be shown again):")
    typer.echo("")
    typer.echo(f"  {new_key}")
    typer.echo("")
    typer.echo(f"written to: {cfg.env_file}")
    typer.echo("restart the server to activate:")
    typer.echo("  sudo systemctl restart amperstand-server")


def _atomic_write(target: Path, data: bytes, mode: int = 0o600) -> None:
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        dir=parent, prefix=".tmp-", suffix=target.suffix, delete=False
    ) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    os.chmod(tmp_name, mode)
    os.replace(tmp_name, target)
