"""Movomatic API tests: yt-dlp and Gemini are mocked, Jellyseerr is simulated with respx."""

import asyncio
import json
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
from app.models import MediaIdentification, MovomaticRequest, ReelAnalysis, ReelMetadata

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
ALIEN_RESULT = {
    "id": 348,
    "mediaType": "movie",
    "title": "Alien",
    "originalTitle": "Alien",
    "releaseDate": "1979-05-25",
    "genreIds": [27, 878],
    "originalLanguage": "en",
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
    base = {"title": "Dune: Part Two", "year": 2024, "media_type": "movie",
            "is_anime": False, "confidence": 0.95, "reasoning": "title in the caption"}
    return MediaIdentification(**{**base, **kw})


def analysis(*items: MediaIdentification, featured: int | None = None) -> ReelAnalysis:
    return ReelAnalysis(works_featured=len(items) if featured is None else featured, items=list(items))


NOTHING = analysis(featured=0)


@pytest.fixture
def jellyseerr_mock():
    with respx.mock(base_url=JELLYSEERR, assert_all_called=False) as mock:
        yield mock


def search_by_title(jellyseerr_mock, mapping: dict[str, list[dict]]):
    """Answer /search with the results registered for the queried title."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": mapping.get(request.url.params["query"], [])})

    return jellyseerr_mock.get("/api/v1/search").mock(side_effect=handler)


@pytest.fixture
def make_client(jellyseerr_mock):
    def _make(settings: Settings | None = None, text=None, video=None, meta=None):
        settings = settings or make_settings()
        extractor = MagicMock()
        extractor.extract_metadata = AsyncMock(
            return_value=meta or ReelMetadata(url=REEL, id="C1a2B3c4D5e", description="Dune 2 🔥 #dune")
        )
        extractor.download_video = AsyncMock(return_value=Path("/tmp/fake.mp4"))
        analyzer = MagicMock()
        analyzer.analyze_metadata = AsyncMock(return_value=text if text is not None else analysis(ident()))
        analyzer.analyze_video = AsyncMock(return_value=video if video is not None else NOTHING)
        services = Services(
            settings=settings,
            extractor=extractor,
            analyzer=analyzer,
            jellyseerr=JellyseerrClient(settings),
            semaphore=asyncio.Semaphore(1),
        )
        app.state.services = services
        # Without `with`, TestClient skips the lifespan, so our mocked services stay in place.
        return TestClient(app), services

    yield _make


def post(client: TestClient, url: str = REEL, key: str = API_KEY, **extra):
    return client.post("/movomatic", json={"url": url, **extra}, headers={"X-API-Key": key})


def sent_bodies(route) -> list[dict]:
    return [json.loads(call.request.content) for call in route.calls]


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
# Single title
# --------------------------------------------------------------------------- #
def test_movie_is_requested(make_client, jellyseerr_mock):
    client, services = make_client()
    search = jellyseerr_mock.get("/api/v1/search").respond(json={"results": [DUNE_RESULT]})
    request = jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 42})

    resp = post(client)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "requested"
    assert data["source"] == "metadata"
    assert data["message"] == "✅ Movie requested: Dune: Part Two (2024)"
    [item] = data["results"]
    assert item["request_id"] == 42
    assert item["media"]["tmdb_id"] == 693134
    assert item["media"]["poster_url"] == "https://image.tmdb.org/t/p/w500/poster.jpg"
    # Strict encoding required by Jellyseerr.
    assert "query=Dune%3A%20Part%20Two" in str(search.calls.last.request.url)
    assert request.calls.last.request.headers["X-Api-Key"] == "fake-jellyseerr-key"
    assert sent_bodies(request) == [{"mediaType": "movie", "mediaId": 693134, "is4k": False}]
    services.analyzer.analyze_video.assert_not_called()


def test_anime_series_uses_overrides(make_client, jellyseerr_mock):
    settings = make_settings(anime_sonarr_server_id=1, anime_sonarr_profile_id=7,
                             anime_sonarr_root_folder="/media/anime")
    frieren = ident(title="Frieren: Beyond Journey's End", original_title="Sousou no Frieren",
                    year=2023, media_type="tv", is_anime=True)
    client, _ = make_client(settings=settings, text=analysis(frieren))
    jellyseerr_mock.get("/api/v1/search").respond(json={"results": [FRIEREN_RESULT]})
    request = jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 7})

    data = post(client).json()

    assert data["status"] == "requested"
    assert data["results"][0]["media"]["is_anime"] is True
    assert data["message"].startswith("✅ Anime")
    assert sent_bodies(request) == [{"mediaType": "tv", "mediaId": 209867, "is4k": False, "seasons": "all",
                                     "serverId": 1, "profileId": 7, "rootFolder": "/media/anime"}]


def test_video_fallback_when_text_finds_nothing(make_client, jellyseerr_mock):
    client, services = make_client(text=analysis(featured=1), video=analysis(ident(confidence=0.9)))
    jellyseerr_mock.get("/api/v1/search").respond(json={"results": [DUNE_RESULT]})
    jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 1})

    data = post(client).json()

    assert data["status"] == "requested"
    assert data["source"] == "video"
    services.extractor.download_video.assert_awaited_once()
    services.analyzer.analyze_video.assert_awaited_once()


def test_low_confidence_items_are_ignored(make_client, jellyseerr_mock):
    client, _ = make_client(text=analysis(ident(confidence=0.3)), video=analysis(ident(confidence=0.4)))
    data = post(client).json()
    assert data["status"] == "not_identified"
    assert data["results"] == []
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
    client, _ = make_client(text=analysis(ident(title="Dune", year=2021)))
    old = {**DUNE_RESULT, "id": 841, "title": "Dune", "originalTitle": "Dune", "releaseDate": "1984-12-14"}
    new = {**DUNE_RESULT, "id": 438631, "title": "Dune", "originalTitle": "Dune", "releaseDate": "2021-09-15"}
    series = {**FRIEREN_RESULT, "id": 1, "name": "Dune", "originalName": "Dune", "firstAirDate": "2000-12-03"}
    jellyseerr_mock.get("/api/v1/search").respond(json={"results": [old, series, new]})
    jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 3})
    assert post(client).json()["results"][0]["media"]["tmdb_id"] == 438631


REALITY_2015 = {"id": 179150, "mediaType": "movie", "title": "Reality", "originalTitle": "Réalité",
                "releaseDate": "2015-02-18", "genreIds": [35], "originalLanguage": "fr"}


@pytest.mark.parametrize(
    "item, results, expected",
    [
        # Real case: TMDB ignores "+", so "Reality+" (2014) only finds "Reality" (2015).
        (ident(title="Reality+", year=2014), [REALITY_2015], None),
        # Approximate title + exact year is accepted.
        (ident(title="Dune", year=2024), [DUNE_RESULT], 693134),
        # Approximate title + other year is rejected.
        (ident(title="Dune", year=2021), [DUNE_RESULT], None),
        # Same title but a different decade: a homonym or remake, rejected for movies.
        (ident(title="Dune: Part Two", year=1984), [DUNE_RESULT], None),
        # Same title, release year off by one (country release dates): accepted.
        (ident(title="Dune: Part Two", year=2023), [DUNE_RESULT], 693134),
        # Punctuation differences do not matter.
        (ident(title="Dune - Part Two", year=None), [DUNE_RESULT], 693134),
        # Series: first-air year is often wrong, exact title still accepted.
        (ident(title="Frieren: Beyond Journey's End", year=2020, media_type="tv", is_anime=True),
         [FRIEREN_RESULT], 209867),
    ],
)
async def test_title_matching_rules(item, results, expected):
    settings = make_settings()
    with respx.mock(base_url=JELLYSEERR) as mock:
        mock.get("/api/v1/search").respond(json={"results": results})
        match = await JellyseerrClient(settings).find_best_match(item)
    assert (match.tmdb_id if match else None) == expected


# --------------------------------------------------------------------------- #
# Several titles in one Reel
# --------------------------------------------------------------------------- #
def test_every_title_is_requested(make_client, jellyseerr_mock):
    alien = ident(title="Alien", year=1979)
    frieren = ident(title="Frieren: Beyond Journey's End", year=2023, media_type="tv", is_anime=True)
    client, services = make_client(text=analysis(ident(), alien, frieren))
    search_by_title(jellyseerr_mock, {
        "Dune: Part Two": [DUNE_RESULT],
        "Alien": [{**ALIEN_RESULT, "mediaInfo": {"status": 5}}],
        "Frieren: Beyond Journey's End": [FRIEREN_RESULT],
    })
    request = jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 9})

    data = post(client).json()

    assert data["status"] == "requested"
    assert data["works_featured"] == 3
    assert [r["status"] for r in data["results"]] == ["requested", "already_available", "requested"]
    assert {b["mediaId"] for b in sent_bodies(request)} == {693134, 209867}
    assert data["message"].splitlines() == [
        "🎬 3 titles: 2 requested, 1 already available",
        "✅ Movie requested: Dune: Part Two (2024)",
        "📺 Alien (1979): already available",
        "✅ Anime requested: Frieren: Beyond Journey's End (2023)",
    ]
    services.analyzer.analyze_video.assert_not_called()


def test_video_completes_a_partial_caption(make_client, jellyseerr_mock):
    # The caption names 1 of 3 works: the video finds the others, duplicates are merged.
    client, services = make_client(
        text=analysis(ident(), featured=3),
        video=analysis(ident(confidence=0.99), ident(title="Alien", year=1979), featured=3),
    )
    search_by_title(jellyseerr_mock, {"Dune: Part Two": [DUNE_RESULT], "Alien": [ALIEN_RESULT]})
    request = jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 1})

    data = post(client).json()

    services.analyzer.analyze_video.assert_awaited_once()
    assert data["source"] == "video"
    assert [r["media"]["tmdb_id"] for r in data["results"]] == [693134, 348]
    assert len(request.calls) == 2
    assert data["message"].splitlines()[-1] == "🤷 1 more could not be identified"


def test_titles_resolving_to_same_tmdb_entry_are_requested_once(make_client, jellyseerr_mock):
    client, _ = make_client(text=analysis(ident(), ident(title="Dune Part Two", year=None)))
    jellyseerr_mock.get("/api/v1/search").respond(json={"results": [DUNE_RESULT]})
    request = jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 1})

    data = post(client).json()

    assert len(data["results"]) == 1
    assert len(request.calls) == 1


def test_titles_are_capped(make_client, jellyseerr_mock):
    items = [ident(title=f"Movie {i}", year=2000 + i) for i in range(5)]
    client, _ = make_client(settings=make_settings(max_titles_per_reel=2), text=analysis(*items))
    search = jellyseerr_mock.get("/api/v1/search").respond(json={"results": []})
    post(client)
    assert len(search.calls) == 2


def test_one_failing_title_does_not_block_the_others(make_client, jellyseerr_mock):
    client, _ = make_client(text=analysis(ident(), ident(title="Alien", year=1979)))
    search_by_title(jellyseerr_mock, {"Dune: Part Two": [DUNE_RESULT], "Alien": [ALIEN_RESULT]})

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["mediaId"] == 348:
            return httpx.Response(500, json={"message": "Radarr is down"})
        return httpx.Response(201, json={"id": 5})

    jellyseerr_mock.post("/api/v1/request").mock(side_effect=handler)

    data = post(client).json()

    assert data["status"] == "requested"
    assert [r["status"] for r in data["results"]] == ["requested", "error"]
    assert "Radarr is down" in data["results"][1]["message"]


# --------------------------------------------------------------------------- #
# Upstream errors
# --------------------------------------------------------------------------- #
def test_extraction_error_returns_422(make_client):
    client, services = make_client()
    services.extractor.extract_metadata.side_effect = ExtractionError("login required")
    resp = post(client)
    assert resp.status_code == 422
    assert "login required" in resp.json()["message"]


def test_jellyseerr_down_returns_503(make_client, jellyseerr_mock):
    # Not 502: Cloudflare would replace the JSON body with its own HTML error page.
    client, _ = make_client()
    jellyseerr_mock.get("/api/v1/search").mock(side_effect=httpx.ConnectError("refused"))
    resp = post(client)
    assert resp.status_code == 503
    assert resp.json()["status"] == "error"


async def test_slow_job_answers_early_and_finishes_in_background(make_client, jellyseerr_mock):
    from app import main

    _, services = make_client(settings=make_settings(response_timeout_seconds=0.05))
    slow = asyncio.Event()

    async def slow_analysis(meta):
        await slow.wait()
        return analysis(ident())

    services.analyzer.analyze_metadata = AsyncMock(side_effect=slow_analysis)
    jellyseerr_mock.get("/api/v1/search").respond(json={"results": [DUNE_RESULT]})
    request = jellyseerr_mock.post("/api/v1/request").respond(201, json={"id": 1})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/movomatic", json={"url": REEL}, headers={"X-API-Key": API_KEY})

    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"
    assert not request.called

    slow.set()  # Gemini finally answers: the job must still request the title
    await asyncio.gather(*main._background_jobs)
    assert request.called


# --------------------------------------------------------------------------- #
# Gemini model fallback
# --------------------------------------------------------------------------- #
def _api_error(code: int):
    from google.genai import errors

    return errors.APIError(code, {"error": {"code": code, "message": "boom", "status": "X"}})


def _gemini(side_effect):
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=side_effect)
    return client


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr("app.analyzer.RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr("app.analyzer.ROUND_PAUSE_SECONDS", 0)


async def test_gemini_falls_back_to_next_model():
    from app.analyzer import GeminiAnalyzer

    ok = MagicMock(parsed=analysis(ident()))
    # "a" stays overloaded (3 attempts), "b" is retired (no retry), "c" answers.
    client = _gemini([_api_error(503)] * 3 + [_api_error(404), ok])
    settings = make_settings(gemini_model="a", gemini_fallback_models="b, c,a")
    result = await GeminiAnalyzer(settings, client=client).analyze_metadata(ReelMetadata(url=REEL))

    assert result.items[0].title == "Dune: Part Two"
    models = [c.kwargs["model"] for c in client.aio.models.generate_content.await_args_list]
    assert models == ["a", "a", "a", "b", "c"]


async def test_gemini_retries_same_model_on_short_overload():
    from app.analyzer import GeminiAnalyzer

    client = _gemini([_api_error(503), MagicMock(parsed=analysis(ident()))])
    await GeminiAnalyzer(make_settings(gemini_model="a"), client=client).analyze_metadata(ReelMetadata(url=REEL))
    models = [c.kwargs["model"] for c in client.aio.models.generate_content.await_args_list]
    assert models == ["a", "a"]


async def test_gemini_does_not_retry_auth_errors():
    from app.analyzer import AnalysisError, GeminiAnalyzer

    client = _gemini([_api_error(403)])
    with pytest.raises(AnalysisError, match="403"):
        await GeminiAnalyzer(make_settings(), client=client).analyze_metadata(ReelMetadata(url=REEL))
    assert client.aio.models.generate_content.await_count == 1


async def test_gemini_all_models_unavailable():
    from app.analyzer import AnalysisError, GeminiAnalyzer

    client = _gemini([_api_error(503)] * 9)
    settings = make_settings(gemini_patience_seconds=0)
    with pytest.raises(AnalysisError, match="Gemini unavailable"):
        await GeminiAnalyzer(settings, client=client).analyze_metadata(ReelMetadata(url=REEL))
    assert client.aio.models.generate_content.await_count == 9


async def test_gemini_starts_another_round_when_everything_is_overloaded(monkeypatch):
    from app.analyzer import GeminiAnalyzer

    monkeypatch.setattr("app.analyzer.ROUND_PAUSE_SECONDS", 0.01)
    # First round: 3 models x 3 attempts all overloaded; second round: first model answers.
    client = _gemini([_api_error(503)] * 9 + [MagicMock(parsed=analysis(ident()))])
    settings = make_settings(gemini_patience_seconds=60)
    result = await GeminiAnalyzer(settings, client=client).analyze_metadata(ReelMetadata(url=REEL))
    assert result.items[0].title == "Dune: Part Two"
    assert client.aio.models.generate_content.await_count == 10
