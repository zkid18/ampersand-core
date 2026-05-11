"""Web view router — serves the static HTML+JS shell at /ui/.

The static asset mount (/ui/static/*) lives on the FastAPI app itself
because APIRouter.mount() doesn't propagate through include_router. The
HTML shell is served as a regular APIRouter route.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

router = APIRouter(prefix="/ui")

STATIC_DIR = Path(__file__).parent / "static"


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def ui_index() -> FileResponse:
    """Serve the main HTML shell. Same content for /ui and /ui/."""
    return FileResponse(
        STATIC_DIR / "index.html",
        media_type="text/html; charset=utf-8",
    )


def mount_static(app: FastAPI) -> None:
    """Mount /ui/static on the FastAPI app. Call from create_app()."""
    app.mount(
        "/ui/static",
        StaticFiles(directory=STATIC_DIR),
        name="ui-static",
    )
