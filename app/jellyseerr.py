"""Client minimal de l'API Jellyseerr : recherche TMDB, détection d'anime et création de requêtes."""

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import httpx

from app.config import Settings
from app.models import MediaIdentification

logger = logging.getLogger(__name__)

TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w500"
TMDB_GENRE_ANIMATION = 16

# Statuts de MediaInfo côté Jellyseerr.
MEDIA_STATUS_PENDING = 2
MEDIA_STATUS_PROCESSING = 3
MEDIA_STATUS_PARTIALLY_AVAILABLE = 4
MEDIA_STATUS_AVAILABLE = 5


class JellyseerrError(RuntimeError):
    """Erreur de communication avec Jellyseerr."""


class AlreadyRequestedError(JellyseerrError):
    """Jellyseerr indique que la requête existe déjà."""


@dataclass(frozen=True)
class SearchMatch:
    tmdb_id: int
    media_type: str  # "movie" | "tv"
    title: str
    year: Optional[int]
    is_anime: bool
    poster_path: Optional[str]
    media_status: Optional[int]

    @property
    def poster_url(self) -> Optional[str]:
        return f"{TMDB_POSTER_BASE}{self.poster_path}" if self.poster_path else None

    @property
    def is_available(self) -> bool:
        return self.media_status == MEDIA_STATUS_AVAILABLE

    @property
    def is_pending(self) -> bool:
        return self.media_status in (MEDIA_STATUS_PENDING, MEDIA_STATUS_PROCESSING)


class JellyseerrClient:
    def __init__(self, settings: Settings, http: Optional[httpx.AsyncClient] = None) -> None:
        self._settings = settings
        self._http = http or httpx.AsyncClient(
            base_url=settings.jellyseerr_base_url,
            headers={
                "X-Api-Key": settings.jellyseerr_api_key.get_secret_value(),
                "Accept": "application/json",
            },
            timeout=settings.jellyseerr_timeout_seconds,
            verify=settings.jellyseerr_verify_ssl,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise JellyseerrError(f"Jellyseerr injoignable : {exc.__class__.__name__}") from exc

        if resp.status_code == 409:
            raise AlreadyRequestedError(_error_message(resp))
        if resp.status_code in (401, 403):
            raise JellyseerrError("clé API Jellyseerr refusée")
        if resp.status_code >= 400:
            raise JellyseerrError(f"Jellyseerr HTTP {resp.status_code} : {_error_message(resp)}")
        return resp.json() if resp.content else None

    async def ping(self) -> bool:
        try:
            await self._request("GET", "/api/v1/status")
            return True
        except JellyseerrError:
            return False

    # ------------------------------------------------------------------ #
    # Recherche
    # ------------------------------------------------------------------ #
    async def search(self, query: str) -> list[dict[str, Any]]:
        # Jellyseerr exige un encodage strict (RFC 3986) : les espaces doivent être
        # %20 et les caractères !'()* encodés, sinon il répond 400.
        path = f"/api/v1/search?query={quote(query, safe='')}&page=1&language=en"
        data = await self._request("GET", path)
        return [r for r in (data or {}).get("results", []) if r.get("mediaType") in ("movie", "tv")]

    async def find_best_match(self, ident: MediaIdentification) -> Optional[SearchMatch]:
        queries: list[str] = []
        for q in (ident.title, ident.original_title):
            if q and q.strip() and q.strip().lower() not in (x.lower() for x in queries):
                queries.append(q.strip())

        best: Optional[tuple[float, dict[str, Any]]] = None
        for query in queries:
            for rank, result in enumerate(await self.search(query)):
                score = _score(result, ident, rank)
                if best is None or score > best[0]:
                    best = (score, result)
            # Correspondance quasi certaine : inutile d'interroger le titre original.
            if best and best[0] >= 8:
                break

        if best is None or best[0] < 2:
            return None
        return _to_match(best[1], ident, self._settings.jellyseerr_request_4k)

    # ------------------------------------------------------------------ #
    # Requête
    # ------------------------------------------------------------------ #
    async def request_media(self, match: SearchMatch) -> Optional[int]:
        s = self._settings
        body: dict[str, Any] = {
            "mediaType": match.media_type,
            "mediaId": match.tmdb_id,
            "is4k": s.jellyseerr_request_4k,
        }
        if match.media_type == "tv":
            body["seasons"] = "all"
        if s.jellyseerr_user_id is not None:
            body["userId"] = s.jellyseerr_user_id

        if match.is_anime:
            prefix = "anime_sonarr" if match.media_type == "tv" else "anime_radarr"
            overrides = {
                "serverId": getattr(s, f"{prefix}_server_id"),
                "profileId": getattr(s, f"{prefix}_profile_id"),
                "rootFolder": getattr(s, f"{prefix}_root_folder"),
            }
            body.update({k: v for k, v in overrides.items() if v is not None})

        logger.info("Requête Jellyseerr : %s", body)
        data = await self._request("POST", "/api/v1/request", json=body)
        return (data or {}).get("id")


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _normalize(text: Optional[str]) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _year(result: dict[str, Any]) -> Optional[int]:
    date = result.get("releaseDate") or result.get("firstAirDate") or ""
    return int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None


def _title(result: dict[str, Any]) -> str:
    return result.get("title") or result.get("name") or ""


def is_anime_result(result: dict[str, Any]) -> bool:
    return (
        TMDB_GENRE_ANIMATION in (result.get("genreIds") or [])
        and result.get("originalLanguage") == "ja"
    )


def _score(result: dict[str, Any], ident: MediaIdentification, rank: int) -> float:
    score = 0.0
    wanted = {_normalize(ident.title), _normalize(ident.original_title)} - {""}
    candidates = {
        _normalize(result.get(k)) for k in ("title", "name", "originalTitle", "originalName")
    } - {""}

    if wanted & candidates:
        score += 5
    elif any(w in c or c in w for w in wanted for c in candidates):
        score += 2
    else:
        return -10.0  # aucun recoupement de titre : jamais retenu

    if ident.media_type and result.get("mediaType") == ident.media_type:
        score += 2

    year = _year(result)
    if ident.year and year:
        if year == ident.year:
            score += 3
        elif abs(year - ident.year) == 1:
            score += 1.5
        else:
            score -= 1

    if ident.is_anime and is_anime_result(result):
        score += 1

    # TMDB trie déjà par pertinence/popularité : léger bonus aux premiers résultats.
    score += max(0.0, 1.0 - rank * 0.2)
    return score


def _to_match(result: dict[str, Any], ident: MediaIdentification, use_4k: bool = False) -> SearchMatch:
    media_info = result.get("mediaInfo") or {}
    return SearchMatch(
        tmdb_id=int(result["id"]),
        media_type=result["mediaType"],
        title=_title(result),
        year=_year(result),
        is_anime=is_anime_result(result) or (ident.is_anime and TMDB_GENRE_ANIMATION in (result.get("genreIds") or [])),
        poster_path=result.get("posterPath"),
        media_status=media_info.get("status4k" if use_4k else "status"),
    )


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("message") or data.get("error") or data)
    except ValueError:
        return resp.text[:200]
