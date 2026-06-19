"""Console-script entry point: `amperstand-server`.

Wraps uvicorn so operators never have to type the module path.
Mirrors uvicorn's most-used flags; pass anything else through env.
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="amperstand-server",
        description="Run the amperstand-core HTTP server.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("AMPERSTAND_HOST", "0.0.0.0"),
        help="Bind address (default: 0.0.0.0, or $AMPERSTAND_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AMPERSTAND_PORT", "8765")),
        help="Bind port (default: 8765, or $AMPERSTAND_PORT).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("AMPERSTAND_UVICORN_WORKERS", "1")),
        help=(
            "Number of uvicorn worker processes (default: 1). Note: this is "
            "uvicorn's worker pool, NOT the capture worker count "
            "(AMPERSTAND_CAPTURE_WORKERS)."
        ),
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Reload on code changes (development only).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("AMPERSTAND_LOG_LEVEL", "info"),
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Uvicorn log level (default: info).",
    )
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        sys.stderr.write(
            "uvicorn is not installed. amperstand-core is missing a runtime "
            "dependency — `pip install --upgrade amperstand-core` should fix it.\n"
        )
        return 1

    uvicorn.run(
        "amperstand_core.server.app:app",
        host=args.host,
        port=args.port,
        workers=args.workers if not args.reload else 1,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
