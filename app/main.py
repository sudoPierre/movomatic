"""FastAPI entry point: /movomatic and /health routes."""

import asyncio
import logging
import secrets
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app import __version__
from app.analyzer import AnalysisError, GeminiAnalyzer
from app.config import Settings, get_settings
from app.extractor import ExtractionError, ReelExtractor
from app.jellyseerr import (
    AlreadyRequestedError,
    JellyseerrClient,
    JellyseerrError,
    SearchMatch,
    normalize_title,
)
from app.models import (
    ErrorResponse,
    HealthResponse,
    ItemResult,
    MediaIdentification,
    MediaResult,
    MovomaticRequest,
    MovomaticResponse,
    ReelAnalysis,
    Status,
)

logger = logging.getLogger("movomatic")


@dataclass
class Services:
    settings: Settings
    extractor: ReelExtractor
    analyzer: GeminiAnalyzer
    jellyseerr: JellyseerrClient
    semaphore: asyncio.Semaphore


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    jellyseerr = JellyseerrClient(settings)
    app.state.services = Services(
        settings=settings,
        extractor=ReelExtractor(settings),
        analyzer=GeminiAnalyzer(settings),
        jellyseerr=jellyseerr,
        semaphore=asyncio.Semaphore(settings.max_concurrent_jobs),
    )
    logger.info("Movomatic %s started (model %s)", __version__, settings.gemini_model)
    try:
        yield
    finally:
        await jellyseerr.aclose()


app = FastAPI(
    title="Movomatic",
    version=__version__,
    description="Instagram Reel ➜ Gemini ➜ Jellyseerr",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
def get_services(request: Request) -> Services:
    return request.app.state.services


def require_api_key(
    services: Annotated[Services, Depends(get_services)],
    x_api_key: Annotated[Optional[str], Header()] = None,
) -> None:
    expected = services.settings.movomatic_api_key.get_secret_value()
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
@app.exception_handler(HTTPException)
async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        ErrorResponse(message=f"❌ {exc.detail}").model_dump(), status_code=exc.status_code
    )


@app.exception_handler(ExtractionError)
async def _extraction_error(_: Request, exc: ExtractionError) -> JSONResponse:
    logger.warning("Extraction failed: %s", exc)
    return JSONResponse(
        ErrorResponse(message=f"❌ Could not read the Reel: {exc}").model_dump(),
        status_code=422,
    )


@app.exception_handler(AnalysisError)
@app.exception_handler(JellyseerrError)
async def _upstream_error(_: Request, exc: Exception) -> JSONResponse:
    logger.error("Upstream error: %s", exc)
    # 503 rather than 502/504: Cloudflare replaces origin 502/504 bodies with its own
    # HTML page, which the iOS Shortcut cannot parse as a dictionary.
    return JSONResponse(
        ErrorResponse(message=f"❌ {exc}").model_dump(),
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        headers={"Retry-After": "60"},
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@app.post(
    "/movomatic",
    response_model=MovomaticResponse,
    dependencies=[Depends(require_api_key)],
    responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    tags=["movomatic"],
)
async def movomatic(
    body: MovomaticRequest,
    services: Annotated[Services, Depends(get_services)],
) -> MovomaticResponse:
    # The job runs in its own task so it survives the Shortcut giving up (or our early
    # answer below): titles still get requested once the analysis is done.
    task = asyncio.create_task(_run_job(body, services))
    _background_jobs.add(task)
    task.add_done_callback(_background_jobs.discard)
    task.add_done_callback(_log_outcome)

    done, _ = await asyncio.wait({task}, timeout=services.settings.response_timeout_seconds)
    if task in done:
        return task.result()

    logger.info("Job for %s still running: answering the Shortcut now", body.url)
    return MovomaticResponse(
        status=Status.PROCESSING,
        message="⏳ Still analysing this Reel. The titles will be requested automatically "
        "when it is done (check Jellyseerr in a few minutes).",
    )


# Strong references to running jobs (asyncio only keeps weak ones).
_background_jobs: set[asyncio.Task] = set()


async def _run_job(body: MovomaticRequest, services: Services) -> MovomaticResponse:
    async with services.semaphore:
        try:
            return await asyncio.wait_for(
                process_reel(body, services), timeout=services.settings.job_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise AnalysisError("the analysis took too long") from exc


def _log_outcome(task: asyncio.Task) -> None:
    if task.cancelled():
        logger.warning("Job cancelled")
    elif task.exception() is not None:
        logger.error("Job failed: %s", task.exception())
    else:
        logger.info("Job done: %s", task.result().message.replace("\n", " | "))


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _confident_items(analysis: Optional[ReelAnalysis], settings: Settings) -> list[MediaIdentification]:
    """Items above the confidence threshold, deduplicated, capped to the configured limit."""
    best: dict[tuple[str, Optional[int]], MediaIdentification] = {}
    for item in analysis.items if analysis else []:
        key = (normalize_title(item.title), item.year)
        if not key[0] or item.confidence < settings.gemini_min_confidence:
            continue
        if key not in best or item.confidence > best[key].confidence:
            best[key] = item
    return list(best.values())[: settings.max_titles_per_reel]


def _merge(first: Optional[ReelAnalysis], second: ReelAnalysis) -> ReelAnalysis:
    return ReelAnalysis(
        works_featured=max(first.works_featured if first else 0, second.works_featured),
        items=[*(first.items if first else []), *second.items],
    )


async def identify(url: str, services: Services) -> tuple[Optional[ReelAnalysis], Optional[str]]:
    """Identify the works: from the Reel text first, then from the video if some are missing."""
    settings = services.settings
    meta = await services.extractor.extract_metadata(url)
    logger.info("Reel %s by %s", meta.id, meta.uploader)

    analysis: Optional[ReelAnalysis] = None
    source: Optional[str] = None
    if meta.description or meta.title or meta.tags:
        analysis = await services.analyzer.analyze_metadata(meta)
        source = "metadata"

    found = len(_confident_items(analysis, settings))
    featured = analysis.works_featured if analysis else 0
    if not settings.gemini_video_fallback or (found and found >= featured):
        return analysis, source

    logger.info("Text identified %d of %d work(s): analysing the video", found, featured)
    with tempfile.TemporaryDirectory(prefix="movomatic-", dir=settings.temp_dir) as tmp:
        try:
            video = await services.extractor.download_video(url, Path(tmp))
        except ExtractionError as exc:
            logger.warning("Video download failed: %s", exc)
            return analysis, source
        video_analysis = await services.analyzer.analyze_video(video, meta)
    return _merge(analysis, video_analysis), "video"


async def process_reel(body: MovomaticRequest, services: Services) -> MovomaticResponse:
    settings = services.settings
    analysis, source = await identify(body.url, services)
    items = _confident_items(analysis, settings)
    featured = max(analysis.works_featured if analysis else 0, len(items))

    if not items:
        return MovomaticResponse(
            status=Status.NOT_IDENTIFIED,
            message="🤷 Could not identify any title in this Reel.",
            source=source,
            works_featured=featured,
        )

    matches = await asyncio.gather(
        *(services.jellyseerr.find_best_match(item) for item in items), return_exceptions=True
    )
    results: list[ItemResult] = []
    seen: set[int] = set()
    for item, match in zip(items, matches):
        if isinstance(match, SearchMatch):
            if match.tmdb_id in seen:  # two Gemini titles resolved to the same TMDB entry
                continue
            seen.add(match.tmdb_id)
        results.append(await _handle_item(item, match, body.dry_run, services))

    errors = [r for r in results if r.status == Status.ERROR]
    if len(errors) == len(results):
        raise JellyseerrError(errors[0].message.removeprefix("❌ "))

    unidentified = featured - len(items)
    return MovomaticResponse(
        status=min((r.status for r in results), key=_STATUS_ORDER.index),
        message=_summary(results, unidentified),
        source=source,
        works_featured=featured,
        results=results,
    )


async def _handle_item(
    item: MediaIdentification,
    match: "SearchMatch | None | BaseException",
    dry_run: bool,
    services: Services,
) -> ItemResult:
    if isinstance(match, BaseException):
        if not isinstance(match, JellyseerrError):
            raise match
        return ItemResult(status=Status.ERROR, message=f"❌ {item.label}: {match}", identification=item)
    if match is None:
        return ItemResult(status=Status.NOT_FOUND, message=f"🔎 {item.label}: not found on TMDB",
                          identification=item)

    media = _media_result(match)
    label = f"{media.title} ({media.year})" if media.year else media.title
    common = {"identification": item, "media": media}

    if match.is_available:
        return ItemResult(status=Status.ALREADY_AVAILABLE, message=f"📺 {label}: already available", **common)
    if match.is_pending:
        return ItemResult(status=Status.ALREADY_REQUESTED, message=f"⏳ {label}: already requested", **common)
    if dry_run:
        return ItemResult(status=Status.IDENTIFIED, message=f"🎯 {label}: identified (dry run)", **common)

    try:
        request_id = await services.jellyseerr.request_media(match)
    except AlreadyRequestedError:
        return ItemResult(status=Status.ALREADY_REQUESTED, message=f"⏳ {label}: already requested", **common)
    except JellyseerrError as exc:
        return ItemResult(status=Status.ERROR, message=f"❌ {label}: {exc}", **common)

    kind = "Anime" if media.is_anime else ("Movie" if media.media_type == "movie" else "Series")
    return ItemResult(status=Status.REQUESTED, message=f"✅ {kind} requested: {label}",
                      request_id=request_id, **common)


# Order used to pick the overall status: the most "useful" outcome wins.
_STATUS_ORDER = [
    Status.REQUESTED, Status.IDENTIFIED, Status.ALREADY_REQUESTED,
    Status.ALREADY_AVAILABLE, Status.NOT_FOUND, Status.ERROR, Status.NOT_IDENTIFIED,
]

_SUMMARY_LABELS = {
    Status.REQUESTED: "requested",
    Status.IDENTIFIED: "identified",
    Status.ALREADY_REQUESTED: "already requested",
    Status.ALREADY_AVAILABLE: "already available",
    Status.NOT_FOUND: "not found",
    Status.ERROR: "failed",
}


def _summary(results: list[ItemResult], unidentified: int) -> str:
    """One line for a single title, otherwise a header plus one line per title."""
    if len(results) == 1 and unidentified <= 0:
        return results[0].message
    counts = [
        f"{sum(r.status == status for r in results)} {label}"
        for status, label in _SUMMARY_LABELS.items()
        if any(r.status == status for r in results)
    ]
    lines = [f"🎬 {len(results)} titles: " + ", ".join(counts)]
    lines += [r.message for r in results]
    if unidentified > 0:
        lines.append(f"🤷 {unidentified} more could not be identified")
    return "\n".join(lines)


def _media_result(match: SearchMatch) -> MediaResult:
    return MediaResult(
        tmdb_id=match.tmdb_id,
        media_type=match.media_type,  # type: ignore[arg-type]
        title=match.title,
        year=match.year,
        is_anime=match.is_anime,
        poster_url=match.poster_url,
    )


def run() -> None:
    """Run the server (used by systemd and the `movomatic` command)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        proxy_headers=True,
    )


if __name__ == "__main__":
    run()
