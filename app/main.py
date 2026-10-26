"""Point d'entrée FastAPI : routes /movomatic et /health."""

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
from app.jellyseerr import AlreadyRequestedError, JellyseerrClient, JellyseerrError, SearchMatch
from app.models import (
    ErrorResponse,
    HealthResponse,
    MediaIdentification,
    MediaResult,
    MovomaticRequest,
    MovomaticResponse,
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
    logger.info("Movomatic %s démarré (modèle %s)", __version__, settings.gemini_model)
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
# Dépendances
# --------------------------------------------------------------------------- #
def get_services(request: Request) -> Services:
    return request.app.state.services


def require_api_key(
    services: Annotated[Services, Depends(get_services)],
    x_api_key: Annotated[Optional[str], Header()] = None,
) -> None:
    expected = services.settings.movomatic_api_key.get_secret_value()
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "clé API invalide ou absente")


# --------------------------------------------------------------------------- #
# Gestion des erreurs
# --------------------------------------------------------------------------- #
@app.exception_handler(HTTPException)
async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        ErrorResponse(message=f"❌ {exc.detail}").model_dump(), status_code=exc.status_code
    )


@app.exception_handler(ExtractionError)
async def _extraction_error(_: Request, exc: ExtractionError) -> JSONResponse:
    logger.warning("Extraction impossible : %s", exc)
    return JSONResponse(
        ErrorResponse(message=f"❌ Reel illisible : {exc}").model_dump(),
        status_code=422,
    )


@app.exception_handler(AnalysisError)
@app.exception_handler(JellyseerrError)
async def _upstream_error(_: Request, exc: Exception) -> JSONResponse:
    logger.error("Erreur amont : %s", exc)
    return JSONResponse(
        ErrorResponse(message=f"❌ {exc}").model_dump(), status_code=status.HTTP_502_BAD_GATEWAY
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
    responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    tags=["movomatic"],
)
async def movomatic(
    body: MovomaticRequest,
    services: Annotated[Services, Depends(get_services)],
) -> MovomaticResponse:
    async with services.semaphore:
        return await process_reel(body, services)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _is_confident(ident: Optional[MediaIdentification], settings: Settings) -> bool:
    return bool(
        ident
        and ident.identified
        and ident.title
        and ident.confidence >= settings.gemini_min_confidence
    )


async def identify(url: str, services: Services) -> tuple[Optional[MediaIdentification], Optional[str]]:
    """Identifie l'œuvre : d'abord sur le texte du Reel, puis sur la vidéo si nécessaire."""
    settings = services.settings
    meta = await services.extractor.extract_metadata(url)
    logger.info("Reel %s par %s", meta.id, meta.uploader)

    ident: Optional[MediaIdentification] = None
    source: Optional[str] = None
    if meta.description or meta.title or meta.tags:
        ident = await services.analyzer.analyze_metadata(meta)
        source = "metadata"
    if _is_confident(ident, settings) or not settings.gemini_video_fallback:
        return ident, source

    logger.info("Confiance insuffisante sur le texte : analyse de la vidéo")
    with tempfile.TemporaryDirectory(prefix="movomatic-", dir=settings.temp_dir) as tmp:
        try:
            video = await services.extractor.download_video(url, Path(tmp))
        except ExtractionError as exc:
            logger.warning("Téléchargement vidéo impossible : %s", exc)
            return ident, source
        video_ident = await services.analyzer.analyze_video(video, meta)

    if ident is None or video_ident.confidence >= ident.confidence:
        return video_ident, "video"
    return ident, source


async def process_reel(body: MovomaticRequest, services: Services) -> MovomaticResponse:
    settings = services.settings
    ident, source = await identify(body.url, services)

    if not _is_confident(ident, settings):
        return MovomaticResponse(
            status=Status.NOT_IDENTIFIED,
            message="🤷 Impossible d'identifier l'œuvre de ce Reel.",
            source=source,
            identification=ident,
        )
    assert ident is not None

    match = await services.jellyseerr.find_best_match(ident)
    if match is None:
        label = f"{ident.title} ({ident.year})" if ident.year else ident.title
        return MovomaticResponse(
            status=Status.NOT_FOUND,
            message=f"🔎 « {label} » introuvable sur TMDB.",
            source=source,
            identification=ident,
        )

    media = _media_result(match)
    label = f"{media.title} ({media.year})" if media.year else media.title
    common = {"source": source, "identification": ident, "media": media}

    if match.is_available:
        return MovomaticResponse(status=Status.ALREADY_AVAILABLE, message=f"📺 {label} est déjà disponible.", **common)
    if match.is_pending:
        return MovomaticResponse(status=Status.ALREADY_REQUESTED, message=f"⏳ {label} est déjà demandé.", **common)
    if body.dry_run:
        return MovomaticResponse(status=Status.IDENTIFIED, message=f"🎯 {label} identifié (dry run).", **common)

    try:
        request_id = await services.jellyseerr.request_media(match)
    except AlreadyRequestedError:
        return MovomaticResponse(status=Status.ALREADY_REQUESTED, message=f"⏳ {label} est déjà demandé.", **common)

    kind = "Anime" if media.is_anime else ("Film" if media.media_type == "movie" else "Série")
    return MovomaticResponse(
        status=Status.REQUESTED,
        message=f"✅ {kind} demandé : {label}",
        request_id=request_id,
        **common,
    )


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
    """Lance le serveur (utilisé par systemd et la commande `movomatic`)."""
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
