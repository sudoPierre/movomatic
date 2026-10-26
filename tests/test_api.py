"""Movomatic API tests: yt-dlp and Gemini are mocked, Jellyseerr is simulated with respx."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.extractor import ExtractionError
from app.jellyseerr import JellyseerrClient
from app.main import Services, app
from app.models import MediaIdentification, MovomaticRequest, ReelMetadata

API_KEY = "test-api-key-0123456789"
JELLYSEERR = "http://jellyseerr.test:5055"
REEL = "https://www.instagram.com/reel/C1a2B3c4D5e/"

DUNE_RESULT = {
    "id": 693134,
    "mediaType": "movie",
    "title": "Dune: Part Two",
    "originalTitle": "Dune: Part Two",
    "releaseDate": "2024-02-27",
    "genreIds": [878, 12],
    "originalLanguage": "en",
    "posterPath": "/poster.jpg",
}
FRIEREN_RESULT = {
    "id": 209867,
    "mediaType": "tv",
    "name": "Frieren: Beyond Journey's End",
    "originalName": "葬送のフリーレン",
    "firstAirDate": "2023-09-29",
    "genreIds": [16, 10759, 10765],
    "originalLanguage": "ja",
}


def make_settings(**overrides) -> Settings:
    values = {
        "movomatic_api_key": API_KEY,
        "gemini_api_key": "fake-gemini-key",
        "jellyseerr_url": JELLYSEERR,
        "jellyseerr_api_key": "fake-jellyseerr-key",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def ident(**kw) -> MediaIdentification:
    base = {"identified": True, "title": "Dune: Part Two", "year": 2024,
            "media_type": "movie", "is_anime": False, "confidence": 0.95, "reasoning": "title in the caption"}
    return MediaIdentification(**{**base, **kw})


@pytest.fixture
def jellyseerr_mock():
    with respx.mock(base_url=JELLYSEERR, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def make_client(jellyseerr_mock):
    created: list[Services] = []

    def _make(settings: Settings | None = None, text=None, video=None, meta=None):
        settings = settings or make_settings()
        extractor = MagicMock()
        extractor.extract_metadata = AsyncMock(
            return_value=meta or ReelMetadata(url=REEL, id="C1a2B3c4D5e", description="Dune 2 🔥 #dune")
        )
        extractor.download_video = AsyncMock(return_value=Path("/tmp/fake.mp4"))
        analyzer = MagicMock()
        analyzer.analyze_metadata = AsyncMock(return_value=text if text is not None else ident())
        analyzer.analyze_video = AsyncMock(return_value=video)
        services = Services(
            settings=settings,
            extractor=extractor,
            analyzer=analyzer,
            jellyseerr=JellyseerrClient(settings),
            semaphore=asyncio.Semaphore(1),
        )
        app.state.services = services
        created.append(services)
        # Without `with`, TestClient skips the lifespan, so our mocked services stay in place.
        return TestClient(app), services

    yield _make


def post(client: TestClient, url: str = REEL, key: str = API_KEY, **extra):
    return client.post("/movomatic", json={"url": url, **extra}, headers={"X-API-Key": key})


# --------------------------------------------------------------------------- #
# Basic routes & security
# --------------------------------------------------------------------------- #
def test_health(make_client):
    client, _ = make_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.parametrize("key", ["", "wrong-key-wrong-key"])
def test_rejects_bad_api_key(make_client, key):
    client, services = make_client()
    resp = post(client, key=key)
    assert resp.status_code == 401
    assert resp.json()["status"] == "error"
    services.extractor.extract_metadata.assert_not_called()


def test_rejects_non_instagram_url(make_client):
    client, _ = make_client()
    assert post(client, url="https://www.youtube.com/watch?v=abc").status_code == 422


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=MWQ1ZGUxMzBkMA==",
        "https://instagram.com/reel/C1a2B3c4D5e",
        "Check this out! https://www.instagram.com/reel/C1a2B3c4D5e/?utm_source=ig_web_copy_link",
    ],
)
def test_url_normalization(raw):
    assert MovomaticRequest(url=raw).url == REEL


# --------------------------------------------------------------------------- #
# Full pipeline
# --------------------------------------------------------------------------- #
def test_movie_is_requested(make_client, jellyseerr_mock):
    client, services = make_client()
    search = jellyseerr_mock.get("/api/v1/search").respond(json={"results": [DUNE_RESULT]})
    request = jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 42})

    resp = post(client)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "requested"
    assert data["request_id"] == 42
    assert data["source"] == "metadata"
    assert data["media"]["tmdb_id"] == 693134
    assert data["media"]["poster_url"] == "https://image.tmdb.org/t/p/w500/poster.jpg"
    # Strict encoding required by Jellyseerr.
    assert "query=Dune%3A%20Part%20Two" in str(search.calls.last.request.url)
    assert request.calls.last.request.headers["X-Api-Key"] == "fake-jellyseerr-key"
    assert httpx.Response(200, content=request.calls.last.request.content).json() == {
        "mediaType": "movie", "mediaId": 693134, "is4k": False,
    }
    services.analyzer.analyze_video.assert_not_called()


def test_anime_series_uses_overrides(make_client, jellyseerr_mock):
    settings = make_settings(anime_sonarr_server_id=1, anime_sonarr_profile_id=7,
                             anime_sonarr_root_folder="/media/anime")
    frieren = ident(title="Frieren: Beyond Journey's End", original_title="Sousou no Frieren",
                    year=2023, media_type="tv", is_anime=True)
    client, _ = make_client(settings=settings, text=frieren)
    jellyseerr_mock.get("/api/v1/search").respond(json={"results": [FRIEREN_RESULT]})
    request = jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 7})

    data = post(client).json()

    assert data["status"] == "requested"
    assert data["media"]["is_anime"] is True
    assert data["message"].startswith("✅ Anime")
    sent = httpx.Response(200, content=request.calls.last.request.content).json()
    assert sent == {"mediaType": "tv", "mediaId": 209867, "is4k": False, "seasons": "all",
                    "serverId": 1, "profileId": 7, "rootFolder": "/media/anime"}


def test_video_fallback_when_text_is_unsure(make_client, jellyseerr_mock):
    unsure = MediaIdentification(identified=False, is_anime=False, confidence=0.1)
    client, services = make_client(text=unsure, video=ident(confidence=0.9))
    jellyseerr_mock.get("/api/v1/search").respond(json={"results": [DUNE_RESULT]})
    jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 1})

    data = post(client).json()

    assert data["status"] == "requested"
    assert data["source"] == "video"
    services.extractor.download_video.assert_awaited_once()
    services.analyzer.analyze_video.assert_awaited_once()


def test_not_identified(make_client, jellyseerr_mock):
    unsure = MediaIdentification(identified=False, is_anime=False, confidence=0.2)
    client, _ = make_client(text=unsure, video=unsure)
    data = post(client).json()
    assert data["status"] == "not_identified"
    assert not jellyseerr_mock.calls


def test_not_found_on_tmdb(make_client, jellyseerr_mock):
    client, _ = make_client()
    jellyseerr_mock.get("/api/v1/search").respond(
        json={"results": [{**DUNE_RESULT, "title": "Something Else", "originalTitle": "Autre"}]}
    )
    assert post(client).json()["status"] == "not_found"


def test_already_available(make_client, jellyseerr_mock):
    client, _ = make_client()
    jellyseerr_mock.get("/api/v1/search").respond(
        json={"results": [{**DUNE_RESULT, "mediaInfo": {"status": 5}}]}
    )
    request = jellyseerr_mock.post("/api/v1/request")
    assert post(client).json()["status"] == "already_available"
    assert not request.called


def test_duplicate_request_conflict(make_client, jellyseerr_mock):
    client, _ = make_client()
    jellyseerr_mock.get("/api/v1/search").respond(json={"results": [DUNE_RESULT]})
    jellyseerr_mock.post("/api/v1/request").respond(409, json={"message": "Request already exists"})
    assert post(client).json()["status"] == "already_requested"


def test_dry_run_does_not_request(make_client, jellyseerr_mock):
    client, _ = make_client()
    jellyseerr_mock.get("/api/v1/search").respond(json={"results": [DUNE_RESULT]})
    request = jellyseerr_mock.post("/api/v1/request")
    assert post(client, dry_run=True).json()["status"] == "identified"
    assert not request.called


def test_best_match_prefers_year_and_type(make_client, jellyseerr_mock):
    client, _ = make_client(text=ident(title="Dune", year=2021))
    old = {**DUNE_RESULT, "id": 841, "title": "Dune", "originalTitle": "Dune", "releaseDate": "1984-12-14"}
    new = {**DUNE_RESULT, "id": 438631, "title": "Dune", "originalTitle": "Dune", "releaseDate": "2021-09-15"}
    series = {**FRIEREN_RESULT, "id": 1, "name": "Dune", "originalName": "Dune", "firstAirDate": "2000-12-03"}
    jellyseerr_mock.get("/api/v1/search").respond(json={"results": [old, series, new]})
    jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 3})
    assert post(client).json()["media"]["tmdb_id"] == 438631


# --------------------------------------------------------------------------- #
# Upstream errors
# --------------------------------------------------------------------------- #
def test_extraction_error_returns_422(make_client):
    client, services = make_client()
    services.extractor.extract_metadata.side_effect = ExtractionError("login required")
    resp = post(client)
    assert resp.status_code == 422
    assert "login required" in resp.json()["message"]


def test_jellyseerr_down_returns_502(make_client, jellyseerr_mock):
    client, _ = make_client()
    jellyseerr_mock.get("/api/v1/search").mock(side_effect=httpx.ConnectError("refused"))
    resp = post(client)
    assert resp.status_code == 502
    assert resp.json()["status"] == "error"
