"""FastAPI application — capture URLs and serve a markdown vault over HTTP."""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ampersand_core.converter import to_markdown
from ampersand_core.extractor import (
    extract_article,
    extract_article_from_html,
    is_linkedin_url,
    is_youtube_url,
)
from ampersand_core.linkedin import extract_linkedin
from ampersand_core.youtube import (
    YouTubeTranscriptUnavailable,
    extract_youtube,
    youtube_stub_from_html,
)

from ampersand_core.server.chat_api import router as chat_router
from ampersand_core.server.feed_api import router as feed_router
from ampersand_core.server.vault_api import router as vault_router
from ampersand_core.server.vault_api.auth import require_api_key
from ampersand_core.server.web import mount_static as mount_web_static, router as web_router


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


class CaptureRequest(BaseModel):
    url: str


class CaptureHtmlRequest(BaseModel):
    url: str
    html: str
    title: str | None = None


class CaptureResponse(BaseModel):
    title: str
    url: str
    content_type: str
    markdown: str


def create_app(*, docs_visible: bool | None = None) -> FastAPI:
    """Build the FastAPI app. By default Swagger/OpenAPI are disabled so the
    surface isn't advertised on a public deploy. Set AMPERSAND_PUBLIC_DOCS=1 to
    re-enable them.
    """
    if docs_visible is None:
        docs_visible = _env_bool("AMPERSAND_PUBLIC_DOCS")
    docs_kw = (
        {}
        if docs_visible
        else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    )

    app = FastAPI(
        title="Ampersand API",
        description="Capture anything from the web as markdown.",
        version="0.1.0",
        **docs_kw,
    )

    # CORS for the browser-extension clipper. Extensions live on opaque
    # chrome-extension:// origins, so we have to allow * here. Auth still
    # gates write endpoints via the Bearer key; CORS only controls who can
    # *attempt* a request, not whether it succeeds without credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "If-Match"],
        expose_headers=["ETag"],
    )

    app.include_router(vault_router)
    app.include_router(feed_router)
    app.include_router(chat_router)
    app.include_router(web_router)
    mount_web_static(app)

    # Root redirect so the bare URL (http://<host>/) lands on the web shell
    # instead of FastAPI's default `{"detail":"Not Found"}`. Anyone who pastes
    # just the host into a browser bar should get *something*, not a JSON 404.
    from fastapi.responses import RedirectResponse

    @app.get("/", include_in_schema=False)
    def _root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    def _dispatch(url: str, *, html: str | None, fallback_title: str | None):
        """Pick the right extractor for a URL.

        YouTube and LinkedIn have URL-only extractors that fetch their own
        canonical sources (oembed + transcript API; LinkedIn embed iframe).
        For everything else, prefer the caller-supplied HTML when we have
        it (cheap, uses the user's authenticated browser session) and fall
        back to a fresh server-side fetch.

        YouTube special case: when no transcript is available AND the caller
        supplied HTML (the clipper), fall back to article-from-HTML
        extraction. The watch page's rendered DOM usually contains the
        description, comments, etc. — beats saving a "no transcript" stub.
        """
        if is_youtube_url(url):
            try:
                return extract_youtube(url)
            except YouTubeTranscriptUnavailable as exc:
                if not html:
                    raise
                # YouTube watch pages render the description into JS-only DOM,
                # so trafilatura would just grab the sidebar/comments chrome.
                # Pull og:title + og:description from the supplied HTML instead.
                return youtube_stub_from_html(url, html, fallback=exc)
        if is_linkedin_url(url):
            return extract_linkedin(url)
        if html:
            return extract_article_from_html(url, html, fallback_title=fallback_title)
        return extract_article(url)

    @app.post(
        "/capture",
        response_model=CaptureResponse,
        dependencies=[Depends(require_api_key)],
    )
    def capture(req: CaptureRequest) -> CaptureResponse:
        """Capture a URL and return clean markdown. Requires a Bearer API key."""
        try:
            content = _dispatch(req.url, html=None, fallback_title=None)
        except Exception as e:
            raise HTTPException(status_code=422, detail=str(e))

        return CaptureResponse(
            title=content.title,
            url=content.url,
            content_type=content.content_type.value,
            markdown=to_markdown(content),
        )

    @app.post(
        "/capture/html",
        response_model=CaptureResponse,
        dependencies=[Depends(require_api_key)],
    )
    def capture_html(req: CaptureHtmlRequest) -> CaptureResponse:
        """Capture a page from caller-supplied HTML (browser-extension clipper).

        YouTube/LinkedIn URLs route through their own URL-based extractors
        (transcript API, embed iframe) — the supplied page HTML is ignored
        for those because the canonical content lives elsewhere. Other
        URLs run trafilatura on the supplied HTML.
        """
        try:
            content = _dispatch(req.url, html=req.html, fallback_title=req.title)
        except Exception as e:
            raise HTTPException(status_code=422, detail=str(e))

        return CaptureResponse(
            title=content.title,
            url=content.url,
            content_type=content.content_type.value,
            markdown=to_markdown(content),
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


# Module-level singleton used by `uvicorn ampersand_core.server.app:app`.
app = create_app()
