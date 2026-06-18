"""Admin CLI for the amperstand server. Run via SSH on the box, not over HTTP."""

from amperstand_core.server.admin_cli.cli import app

__all__ = ["app"]
