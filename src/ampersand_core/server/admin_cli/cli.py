"""Top-level typer app for `ampersand-admin`.

Run on the box (over SSH) as the service user. Reads config from process env
plus an optional env-file (default `/etc/ampersand/env`). No HTTP surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ampersand_core.server.admin_cli.commands import backup as backup_cmd
from ampersand_core.server.admin_cli.commands import classifier as classifier_cmd
from ampersand_core.server.admin_cli.commands import integrity as integrity_cmd
from ampersand_core.server.admin_cli.commands import rotate_key as rotate_key_cmd
from ampersand_core.server.admin_cli.commands import stats as stats_cmd
from ampersand_core.server.admin_cli.commands import vec_rebuild as vec_rebuild_cmd
from ampersand_core.server.admin_cli.config import DEFAULT_ENV_FILE, resolve_config

app = typer.Typer(
    name="ampersand-admin",
    help="Out-of-band admin for the ampersand vault server. Run via SSH on the box.",
    no_args_is_help=True,
    add_completion=False,
)


def _resolve(env_file_opt: Path | None):
    chosen = env_file_opt or DEFAULT_ENV_FILE
    return resolve_config(chosen)


EnvFileOpt = Annotated[
    Path | None,
    typer.Option(
        "--env-file",
        help="Path to KEY=VALUE env file. Defaults to /etc/ampersand/env if present.",
        exists=False,
    ),
]


@app.command()
def stats(env_file: EnvFileOpt = None) -> None:
    """Show vault size, doc count, oldest/newest, disk free, key status."""
    stats_cmd.run(_resolve(env_file))


@app.command(name="rotate-key")
def rotate_key(
    env_file: EnvFileOpt = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print what would happen without writing."),
    ] = False,
) -> None:
    """Mint a new AMPERSAND_API_KEY and write it to the env file."""
    rotate_key_cmd.run(_resolve(env_file), dry_run=dry_run)


@app.command()
def backup(
    output: Annotated[
        str,
        typer.Argument(
            help="Output path for the .tar.gz, or '-' to stream to stdout.",
        ),
    ],
    env_file: EnvFileOpt = None,
) -> None:
    """Tar the vault data dir to a file or stdout (for piping to S3 etc.)."""
    backup_cmd.run(_resolve(env_file), output=output)


@app.command()
def integrity(
    env_file: EnvFileOpt = None,
    deep: Annotated[
        bool,
        typer.Option("--deep", help="Also recompute content_hash per doc (slower)."),
    ] = False,
) -> None:
    """Walk every doc, verify it parses, ids match, indexes resolve."""
    integrity_cmd.run(_resolve(env_file), deep=deep)


@app.command(name="vec-rebuild")
def vec_rebuild(
    env_file: EnvFileOpt = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Drop the existing vec index and re-embed everything."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print scope without making API calls."),
    ] = False,
) -> None:
    """Backfill (or refresh) the semantic search vector index."""
    vec_rebuild_cmd.run(_resolve(env_file), force=force, dry_run=dry_run)


classifier_app = typer.Typer(
    name="classifier",
    help="Newsletter classifier: retrain on user feedback, manage the model.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(classifier_app)


@classifier_app.command(name="retrain")
def classifier_retrain(
    env_file: EnvFileOpt = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Promote even if the candidate is worse on the holdout (overrides safety check).",
        ),
    ] = False,
) -> None:
    """Train a candidate from bundled + user feedback, promote if it doesn't regress."""
    classifier_cmd.run(_resolve(env_file), force=force)


@classifier_app.command(name="diff")
def classifier_diff(env_file: EnvFileOpt = None) -> None:
    """Show per-example holdout disagreements between current and candidate."""
    classifier_cmd.run_diff(_resolve(env_file))


@classifier_app.command(name="domains")
def classifier_domains(env_file: EnvFileOpt = None) -> None:
    """Show effective newsletter + promo domain lists (bundled + user override)."""
    classifier_cmd.run_domains(_resolve(env_file))


if __name__ == "__main__":
    app()
