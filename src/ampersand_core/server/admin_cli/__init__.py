"""Admin CLI for the ampersand server. Run via SSH on the box, not over HTTP."""

from ampersand_core.server.admin_cli.cli import app

__all__ = ["app"]
