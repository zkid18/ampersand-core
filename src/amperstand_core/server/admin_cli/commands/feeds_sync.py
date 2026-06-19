"""`amperstand-admin feeds-sync` — fire POST /feeds/sync against the local server.

Replaces the old `ExecStart=/bin/sh -c 'curl ... -H "Authorization: Bearer
$AMPERSTAND_API_KEY" ...'` pattern in amperstand-feed-sync.service. That
pattern expanded the secret into the curl process's argv, where any local
user could read it from /proc/<pid>/cmdline.

Here the key is read from the process environment in-process and passed via
httpx headers — never on a command line, never visible in /proc/cmdline.
"""

from __future__ import annotations

import os
import sys
from typing import Annotated

import httpx
import typer

from amperstand_core.server.admin_cli.config import AdminConfig


def run(
    cfg: AdminConfig,
    *,
    base_url: str = "http://127.0.0.1:8765",
    timeout: float = 600.0,
) -> None:
    api_key = os.environ.get("AMPERSTAND_API_KEY") or ""
    if not api_key:
        typer.echo(
            "error: AMPERSTAND_API_KEY is not in the environment. The systemd "
            "unit should load it via EnvironmentFile=. If running by hand, "
            "`source /etc/amperstand/env; export AMPERSTAND_API_KEY` first.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        r = httpx.post(
            f"{base_url.rstrip('/')}/feeds/sync",
            headers={"Authorization": f"Bearer {api_key}"},
            json={},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        typer.echo(f"error: HTTP request failed: {e}", err=True)
        raise typer.Exit(code=1)

    if r.status_code >= 400:
        # Surface the server's failure detail but do NOT echo the request
        # headers (which carry the bearer).
        typer.echo(
            f"error: server returned {r.status_code}: {_safe_detail(r)}",
            err=True,
        )
        raise typer.Exit(code=1)

    body = _safe_detail(r)
    typer.echo(f"feed sync ok: {body}")


def _safe_detail(r: httpx.Response) -> str:
    try:
        data = r.json()
        if isinstance(data, dict):
            return ", ".join(f"{k}={data[k]}" for k in sorted(data) if k != "items")
        return str(data)
    except Exception:
        return r.text[:200]


# CLI shim so `amperstand-admin feeds-sync` works.
def cli_entry(
    env_file: Annotated[
        str | None,
        typer.Option("--env-file", help="Path to env file (loaded by systemd; usually unused here)."),
    ] = None,
    base_url: Annotated[
        str,
        typer.Option("--base-url", help="Local server URL."),
    ] = "http://127.0.0.1:8765",
) -> None:
    """Trigger a server-side feed sync (POST /feeds/sync). Designed for systemd timers."""
    # The admin CLI's _resolve helper is in cli.py; for this subcommand we
    # don't need vault paths, only the env var. AdminConfig with minimal
    # fields is fine.
    from pathlib import Path

    cfg = AdminConfig(
        data_dir=Path("/var/lib/amperstand/vault"),
        api_key_present=bool(os.environ.get("AMPERSTAND_API_KEY")),
        env_file=Path(env_file) if env_file else None,
        env_file_exists=Path(env_file).exists() if env_file else False,
    )
    run(cfg, base_url=base_url)
    sys.exit(0)
