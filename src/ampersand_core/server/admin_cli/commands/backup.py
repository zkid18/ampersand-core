"""`ampersand-admin backup` — tar the data dir to a file or stdout."""

from __future__ import annotations

import hashlib
import sys
import tarfile
from pathlib import Path

import typer

from ampersand_core.server.admin_cli.config import AdminConfig


def run(cfg: AdminConfig, output: str) -> None:
    if not cfg.data_dir.exists():
        typer.echo(f"error: data dir does not exist: {cfg.data_dir}", err=True)
        raise typer.Exit(code=1)

    to_stdout = output == "-"
    if to_stdout:
        _write_tar(cfg.data_dir, sys.stdout.buffer)
        return

    out_path = Path(output).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    typer.echo(f"backing up:  {cfg.data_dir}", err=True)
    with out_path.open("wb") as fh:
        _write_tar(cfg.data_dir, fh)
    size = out_path.stat().st_size
    digest = _sha256_file(out_path)
    typer.echo(f"written:     {out_path} ({size} bytes)", err=True)
    typer.echo(f"sha256:      {digest}", err=True)


def _write_tar(data_dir: Path, fileobj) -> None:
    with tarfile.open(fileobj=fileobj, mode="w:gz") as tar:
        tar.add(data_dir, arcname=data_dir.name)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
