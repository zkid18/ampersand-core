"""`amperstand-admin rotate-key` — mint a new API key + write it to the env file.

Surgical line-replace, not a round-trip-through-a-dict parser:
- Comments preserved.
- Surrounding quotes preserved verbatim.
- Lines without `=` preserved.
- File ordering preserved.
- Mode + owner + group preserved from the existing file via shutil.copystat
  (so the systemd `EnvironmentFile=` keeps working).
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile

import typer

from amperstand_core.server.admin_cli.config import AdminConfig

_KEY_LINE_RE = re.compile(r"^\s*AMPERSTAND_API_KEY\s*=.*$", re.MULTILINE)


def run(cfg: AdminConfig, dry_run: bool = False) -> None:
    if cfg.env_file is None:
        typer.echo(
            "error: rotate-key needs --env-file. Pass the path to your env file "
            "(default on a deployed box: /etc/amperstand/env).",
            err=True,
        )
        raise typer.Exit(code=2)

    if not cfg.env_file.exists():
        typer.echo(
            f"error: env file {cfg.env_file} does not exist. Refusing to mint a "
            f"new key into a fresh file (would silently rotate every client). "
            f"Run bootstrap.sh first, or create the file manually with the "
            f"existing AMPERSTAND_API_KEY=<value> line, then re-run rotate-key.",
            err=True,
        )
        raise typer.Exit(code=2)

    new_key = secrets.token_hex(32)

    original = cfg.env_file.read_text(encoding="utf-8")

    if not _KEY_LINE_RE.search(original):
        typer.echo(
            f"error: no `AMPERSTAND_API_KEY=` line found in {cfg.env_file}. "
            f"Add it manually first, then re-run rotate-key.",
            err=True,
        )
        raise typer.Exit(code=2)

    # Surgical replacement — touches only the matching line.
    rewritten = _KEY_LINE_RE.sub(f"AMPERSTAND_API_KEY={new_key}", original, count=1)

    if dry_run:
        typer.echo("(dry-run) would replace the AMPERSTAND_API_KEY= line in:")
        typer.echo(f"  {cfg.env_file}")
        typer.echo("(dry-run) all other lines, comments, quoting, and file ownership preserved.")
        typer.echo("no files modified.")
        return

    _atomic_write_preserve_meta(cfg.env_file, rewritten.encode("utf-8"))

    typer.echo("new api key (paste into clients now — it will not be shown again):")
    typer.echo("")
    typer.echo(f"  {new_key}")
    typer.echo("")
    typer.echo(f"written to: {cfg.env_file}")
    typer.echo("")
    typer.echo("restart the affected units to activate:")
    typer.echo("  sudo systemctl restart amperstand-server amperstand-vault-watcher amperstand-feed-sync.service")
    typer.echo("  # plus any client units you run (e.g. the Telegram bot — its unit name varies by deployment)")
    typer.echo("")
    typer.echo("then verify they came back:")
    typer.echo("  sudo systemctl is-active amperstand-server")
    typer.echo("")
    typer.echo("note: in-flight requests holding the old key keep working for up")
    typer.echo("to ~25s while the server drains gracefully. If the leak is hot,")
    typer.echo("`systemctl kill --signal=SIGKILL amperstand-server` after the new")
    typer.echo("worker is up cuts the drain window to zero.")


def _atomic_write_preserve_meta(target: Path, data: bytes) -> None:
    """Atomically replace `target` with `data`, preserving mode + owner + group.

    Uses `shutil.copystat` + `os.chown` so the new inode inherits the
    existing file's perms (not the temp file's). This keeps the
    `0640 root:amperstand` invariant that bootstrap.sh establishes and that
    the systemd `EnvironmentFile=/etc/amperstand/env` depends on.

    Cleanup guarantee: if ANY step fails (write, fsync, chmod, chown, replace),
    the temp file containing the new secret is unlinked before the exception
    propagates. NamedTemporaryFile(delete=False) means we own the cleanup
    contract.
    """
    parent = target.parent

    # Snapshot original stat — needed to restore mode + owner + group.
    orig_stat = target.stat()

    tmp_name: Path | None = None
    try:
        with NamedTemporaryFile(
            dir=parent, prefix=".tmp-", suffix=target.suffix, delete=False
        ) as tmp:
            tmp_name = Path(tmp.name)
            # Tighten perms BEFORE writing the secret so even an instantaneous
            # ls during write can't catch a wider mode. NamedTemporaryFile
            # defaults to 0o600 already on POSIX, but be explicit.
            os.chmod(tmp_name, 0o600)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())

        # Preserve perms BEFORE replace. copystat handles mode/atime/mtime
        # (may widen mode back to 0o640 to match the target — fine because
        # the file's group is amperstand and the directory is 0o750).
        shutil.copystat(target, tmp_name)
        try:
            os.chown(tmp_name, orig_stat.st_uid, orig_stat.st_gid)
        except PermissionError:
            # Non-root operator can't chown to a different uid; not fatal
            # because in production rotate-key runs as root. Warn loudly.
            typer.echo(
                "warning: could not preserve owner:group via chown — "
                "file ownership may differ from the original. Run as root "
                "for full preservation.",
                err=True,
            )
        os.replace(tmp_name, target)
        tmp_name = None  # transferred ownership; nothing to clean up
    finally:
        # Best-effort cleanup of the temp file on ANY failure path. If we
        # succeeded above, tmp_name was set to None so this is a no-op.
        if tmp_name is not None:
            try:
                tmp_name.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # If we can't even unlink, the secret persists on disk under
                # `.tmp-XXXX` until manual cleanup. Loud-fail rather than
                # silent-fail: re-raise the original exception via finally.
                pass
