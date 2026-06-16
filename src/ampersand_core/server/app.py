"""FastAPI application — capture URLs and serve a markdown vault over HTTP."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
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

from ampersand_core.server.capture_jobs import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    JobStore,
)
from ampersand_core.server.chat_api import router as chat_router
from ampersand_core.server.feed_api import router as feed_router
from ampersand_core.server.vault_api import router as vault_router
from ampersand_core.server.vault_api.auth import require_api_key
from ampersand_core.server.vault_api.store_factory import get_store
from ampersand_core.server.web import mount_static as mount_web_static, router as web_router

# Surface ampersand-core's INFO logs through the same stderr stream uvicorn
# writes to, which systemd's StandardError=journal then captures. Without
# this the worker startup line + per-job done/failed lines were INVISIBLE
# in `journalctl -u ampersand-server` (F3 from the queue e2e report). Only
# touched when no handler is already configured — tests + production both
# fall through cleanly.
if not logging.getLogger("ampersand_core").handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    _root = logging.getLogger("ampersand_core")
    _root.addHandler(_h)
    _root.setLevel(logging.INFO)

log = logging.getLogger(__name__)

_job_store_singleton: JobStore | None = None


def _job_store() -> JobStore:
    """Lazy singleton — the JobStore lives next to the vault on disk so it
    follows the same data-dir hygiene as everything else (one DB per droplet,
    survives restarts, no separate config to forget)."""
    global _job_store_singleton
    if _job_store_singleton is None:
        data_dir = Path(os.environ.get("AMPERSAND_DATA_DIR", "/var/lib/ampersand/vault"))
        _job_store_singleton = JobStore(data_dir / ".store" / "capture_jobs.db")
    return _job_store_singleton


def reset_job_store_cache() -> None:
    """Drop the cached JobStore so the next call picks up a new AMPERSAND_DATA_DIR.
    Used by tests."""
    global _job_store_singleton
    _job_store_singleton = None


# Set by create_app() once the closure-scoped dispatch functions are wired up.
# The worker runs in a separate task and needs to call into the same
# extraction + persistence path that the sync endpoints use, but those are
# defined inside create_app's closure. The cleanest plumbing is to register
# the runner from inside the closure.
_run_job_callback: callable | None = None


def _register_job_runner(fn) -> None:
    global _run_job_callback
    _run_job_callback = fn


def _worker_count() -> int:
    """How many concurrent drain workers to run. 1 by default — fine for the
    typical clip-or-two-at-a-time desktop user. Bump it for boxes that see
    burst traffic (the bot fan-out, a feed-sync that enqueues 50 URLs at once,
    a CLI script that batch-captures a folder of bookmarks).

    The SQLite claim_next is already atomic via JobStore's write lock, so
    multiple workers won't grab the same job. Clamped to a sane range so
    operators can't accidentally fork-bomb their droplet."""
    raw = os.environ.get("AMPERSAND_CAPTURE_WORKERS", "").strip()
    try:
        v = int(raw) if raw else 1
        return max(1, min(v, 8))
    except ValueError:
        return 1


def _job_timeout_s() -> float:
    """Per-job wall-clock budget for extract+persist. Anything slower gets
    marked failed and the worker moves on. F2 from the queue e2e report:
    a single 62s httpbin failure stalled the line behind it. Configurable
    so you can bump it for heavy YouTube/Playwright loads."""
    raw = os.environ.get("AMPERSAND_CAPTURE_JOB_TIMEOUT_S", "").strip()
    try:
        v = float(raw) if raw else 90.0
        # Clamp to a sane range — silently-tiny timeouts would brick the
        # queue; absurdly large ones defeat the point.
        return max(10.0, min(v, 600.0))
    except ValueError:
        return 90.0


async def _capture_worker(stop: asyncio.Event, *, worker_id: int = 0) -> None:
    """Drain loop. One instance = one concurrent extraction at a time. Multiple
    instances can run safely because JobStore.claim_next is transactional —
    the SQLite UPDATE with the status guard means two workers can't pick up
    the same row. Each worker pulls from the same shared queue and processes
    independently; there's no inter-worker coordination beyond the shared
    `stop` event.

    Runs the actual extract+persist on a thread so we don't block the event
    loop on httpx/playwright/sqlite. Per-job timeout (F2): wraps the thread
    call in asyncio.wait_for so a single hung extraction can't stall the
    line. Note: a timed-out job's underlying thread keeps running (Python
    can't safely kill a thread) until the extraction's own internal timeouts
    fire. We accept short-term thread pile-up — most "slow" jobs are
    network-bound and idle. If this ever becomes a real memory issue we'd
    need subprocess-based workers, not threads.

    Exceptions are caught and recorded on the job, never propagated — the loop
    must survive any single-job failure or one bad URL kills the queue."""
    if _run_job_callback is None:
        log.error("capture-worker[%d]: no runner registered; queue will not drain", worker_id)
        return
    store = _job_store()
    timeout = _job_timeout_s()
    log.info("capture-worker[%d]: started (per-job timeout=%.0fs)", worker_id, timeout)
    while not stop.is_set():
        job = await asyncio.to_thread(store.claim_next)
        if job is None:
            # Empty queue — back off briefly, but stay responsive to shutdown.
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            continue
        job_id = job["id"]
        try:
            doc_id, doc_path, body_hash = await asyncio.wait_for(
                asyncio.to_thread(_run_job_callback, job),
                timeout=timeout,
            )
            await asyncio.to_thread(
                store.mark_done, job_id,
                doc_id=doc_id, doc_path=doc_path, body_hash=body_hash,
            )
            log.info("capture-worker[%d]: job %s done (doc_id=%s)", worker_id, job_id, doc_id)
        except asyncio.TimeoutError:
            log.warning(
                "capture-worker[%d]: job %s timed out after %.0fs; "
                "marking failed and moving on (thread continues in background)",
                worker_id, job_id, timeout,
            )
            await asyncio.to_thread(
                store.mark_failed, job_id,
                f"Job exceeded {timeout:.0f}s budget — extractor stalled. "
                "Try again later or increase AMPERSAND_CAPTURE_JOB_TIMEOUT_S.",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("capture-worker[%d]: job %s failed", worker_id, job_id)
            await asyncio.to_thread(store.mark_failed, job_id, f"{type(e).__name__}: {e}")
    log.info("capture-worker[%d]: stopped", worker_id)


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


class CaptureRequest(BaseModel):
    url: str
    # When true (default), persist the captured doc to the vault as well as
    # returning the extracted markdown. Was extract-only historically; the
    # README's "POST /capture — for your own scripts" implies persistence
    # and the friction inventory flagged the gap. Backward-compat is
    # preserved by leaving the extract-only fields in CaptureResponse — old
    # callers that ignore the new `id`/`path` fields keep working.
    persist: bool = True
    # Optional frontmatter overrides merged on top of what the extractor
    # produces. Used by the extension/bot to add tags like ["clipper"]
    # without having to do a second POST /vault step.
    frontmatter: dict[str, Any] | None = None


class CaptureHtmlRequest(BaseModel):
    url: str
    html: str
    title: str | None = None
    persist: bool = True
    frontmatter: dict[str, Any] | None = None


class CaptureResponse(BaseModel):
    title: str
    url: str
    content_type: str
    markdown: str
    # Populated when persist=True (default). Mirrors DocResponse identity
    # fields so callers can act on the saved doc without a second round-trip.
    id: str | None = None
    path: str | None = None
    body_hash: str | None = None


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

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Reset any jobs left in `running` state by a previous crash/restart
        # so the worker picks them up again.
        store = _job_store()
        n = store.reset_running_on_startup()
        if n:
            log.info("capture-jobs: reset %d running jobs to queued", n)
        n_workers = _worker_count()
        log.info("capture-worker: lifespan starting (workers=%d)", n_workers)

        # Spawn N workers, all sharing the same stop event. claim_next on the
        # JobStore is transactional, so they won't grab duplicates.
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(_capture_worker(stop, worker_id=i))
            for i in range(n_workers)
        ]
        try:
            yield
        finally:
            # Graceful shutdown — F4 from the queue e2e report: the previous
            # 10s wait_for + task.cancel() was too aggressive; mid-Playwright
            # jobs run on a thread that ignores asyncio cancellation, and
            # systemd then SIGKILL'd the process ~20s later. Better path:
            # signal the workers (which exit at the top of the next loop
            # iteration), and give in-flight jobs 25s to finish on their
            # own. If they overrun, systemd's StopTimeoutSec eventually wins
            # — but most jobs complete inside the budget. Jobs still
            # `running` at exit are reset to `queued` on next startup.
            stop.set()
            log.info(
                "capture-worker: stop signalled; waiting up to 25s for "
                "%d in-flight workers", n_workers,
            )
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=25)
                log.info("capture-worker: all workers shut down cleanly")
            except asyncio.TimeoutError:
                log.warning(
                    "capture-worker: %d workers did not shut down within 25s — "
                    "letting systemd terminate the process; in-flight jobs "
                    "will be reset to queued on next startup",
                    sum(1 for t in tasks if not t.done()),
                )
                for t in tasks:
                    if not t.done():
                        t.cancel()
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="Ampersand API",
        description="Capture anything from the web as markdown.",
        version="0.1.0",
        lifespan=_lifespan,
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

    def _run_queued_job(job: dict) -> tuple[str | None, str | None, str | None]:
        """Worker-side: run a job row through the same extract+persist path
        that POST /capture uses synchronously. Returns (doc_id, path, body_hash)
        — None values when persist=False on the job."""
        url = job["url"]
        html = job.get("html")
        fallback_title = job.get("fallback_title")
        frontmatter = json.loads(job["frontmatter"]) if job.get("frontmatter") else None
        persist = bool(job.get("persist", 1))

        content = _dispatch(url, html=html, fallback_title=fallback_title)
        if not persist:
            return None, None, None
        return _persist_capture(content, frontmatter)

    _register_job_runner(_run_queued_job)

    def _persist_capture(content, frontmatter_override: dict[str, Any] | None):
        """Write captured content to the vault using the same store the vault
        router writes to. Returns (id, path, body_hash). Idempotent on
        `source` URL — re-capturing the same URL updates in place rather
        than creating a duplicate doc."""
        fm: dict[str, Any] = {
            "title": content.title,
            "source": content.url,
            "type": content.content_type.value,
        }
        if getattr(content, "author", None):
            fm["author"] = content.author
        if frontmatter_override:
            fm.update(frontmatter_override)
            # The extractor's title/source/type are authoritative — don't let
            # caller-supplied "" or None silently wipe them.
            for key in ("title", "source", "type"):
                if not fm.get(key):
                    fm.pop(key, None)
        store = get_store()
        doc = store.create(to_markdown(content), fm)
        return doc.meta.id, doc.meta.path, doc.meta.body_hash

    @app.post(
        "/capture",
        response_model=CaptureResponse,
        dependencies=[Depends(require_api_key)],
    )
    def capture(req: CaptureRequest) -> CaptureResponse:
        """Capture a URL and return clean markdown. Requires a Bearer API key.

        By default the doc is also persisted to the vault (idempotent on
        the source URL). Pass `persist=false` to extract without saving."""
        try:
            content = _dispatch(req.url, html=None, fallback_title=None)
        except Exception as e:
            raise HTTPException(status_code=422, detail=str(e))

        doc_id = doc_path = body_hash = None
        if req.persist:
            try:
                doc_id, doc_path, body_hash = _persist_capture(content, req.frontmatter)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"capture extracted but persist failed: {e}")

        return CaptureResponse(
            title=content.title,
            url=content.url,
            content_type=content.content_type.value,
            markdown=to_markdown(content),
            id=doc_id,
            path=doc_path,
            body_hash=body_hash,
        )

    # ── async capture (extension queue) ─────────────────────────────
    #
    # Click-and-walk-away flow: the extension/bot POSTs a URL (and optionally
    # HTML), gets a job_id back instantly, and the actual extract+persist
    # happens in a background worker. Lets the popup close in <100ms instead
    # of hanging for 30-120s on YouTube/long articles.

    @app.post(
        "/capture/async",
        status_code=202,
        dependencies=[Depends(require_api_key)],
    )
    def capture_async(req: CaptureRequest) -> dict:
        """Enqueue a URL capture for the background worker. Returns 202 +
        {job_id}. Poll GET /jobs/{id} for status."""
        job_id = _job_store().enqueue(
            url=req.url,
            persist=req.persist,
            frontmatter=req.frontmatter,
        )
        return {"job_id": job_id, "status": STATUS_QUEUED}

    @app.post(
        "/capture/html/async",
        status_code=202,
        dependencies=[Depends(require_api_key)],
    )
    def capture_html_async(req: CaptureHtmlRequest) -> dict:
        """Same as /capture/async but with caller-supplied HTML (extension's
        clip path)."""
        job_id = _job_store().enqueue(
            url=req.url,
            persist=req.persist,
            frontmatter=req.frontmatter,
            html=req.html,
            fallback_title=req.title,
        )
        return {"job_id": job_id, "status": STATUS_QUEUED}

    @app.get(
        "/jobs/{job_id}",
        dependencies=[Depends(require_api_key)],
    )
    def get_job(job_id: str) -> dict:
        """Job status + (when done) the saved doc identity."""
        row = _job_store().get(job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        # Drop the bulky html payload from the response — it was useful
        # for resuming work but the caller doesn't need it back.
        row.pop("html", None)
        return row

    @app.get(
        "/jobs",
        dependencies=[Depends(require_api_key)],
    )
    def list_jobs(
        status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict:
        """List jobs, newest first. Useful for the extension's 'recent clips'
        view and for operators inspecting queue depth."""
        rows = _job_store().list(status=status, limit=limit)
        for r in rows:
            r.pop("html", None)
        return {
            "items": rows,
            "queue_depth": _job_store().queue_depth(),
        }

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

        doc_id = doc_path = body_hash = None
        if req.persist:
            try:
                doc_id, doc_path, body_hash = _persist_capture(content, req.frontmatter)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"capture extracted but persist failed: {e}")

        return CaptureResponse(
            title=content.title,
            url=content.url,
            content_type=content.content_type.value,
            markdown=to_markdown(content),
            id=doc_id,
            path=doc_path,
            body_hash=body_hash,
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


# Module-level singleton used by `uvicorn ampersand_core.server.app:app`.
app = create_app()
