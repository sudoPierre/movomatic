"""Schémas Pydantic : entrées/sorties de l'API et schéma de réponse structurée Gemini."""

import re
from enum import Enum
from typing import Literal, Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am"}
_INSTAGRAM_PATHS = ("/reel/", "/reels/", "/p/", "/tv/")


# --------------------------------------------------------------------------- #
# API — entrée
# --------------------------------------------------------------------------- #
class MovomaticRequest(BaseModel):
    """Corps envoyé par le Raccourci iOS.

    Le champ ``url`` accepte aussi un texte partagé contenant l'URL
    (la feuille de partage iOS ajoute parfois du texte autour du lien).
    """

    url: str = Field(..., max_length=2048, examples=["https://www.instagram.com/reel/C1a2B3c4D5e/"])
    dry_run: bool = Field(False, description="Identifie sans envoyer de requête à Jellyseerr.")

    @field_validator("url")
    @classmethod
    def _normalize_instagram_url(cls, v: str) -> str:
        match = _URL_RE.search(v.strip())
        if not match:
            raise ValueError("aucune URL trouvée dans le champ 'url'")
        parts = urlsplit(match.group(0).rstrip(").,;!?\"'"))
        host = (parts.hostname or "").lower()
        if host not in _INSTAGRAM_HOSTS:
            raise ValueError("l'URL doit pointer vers instagram.com")
        path = parts.path if parts.path.endswith("/") else parts.path + "/"
        if not path.startswith(_INSTAGRAM_PATHS):
            raise ValueError("l'URL doit être celle d'un Reel ou d'une publication Instagram")
        # On retire les paramètres de suivi (?igsh=..., ?utm_source=...).
        return urlunsplit(("https", "www.instagram.com", path, "", ""))


# --------------------------------------------------------------------------- #
# Extraction yt-dlp
# --------------------------------------------------------------------------- #
class ReelMetadata(BaseModel):
    url: str
    id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    uploader: Optional[str] = None
    duration: Optional[float] = None
    tags: list[str] = Field(default_factory=list)

    def as_prompt_text(self) -> str:
        lines = [
            f"Auteur : {self.uploader or 'inconnu'}",
            f"Titre : {self.title or ''}",
            f"Légende : {self.description or ''}",
        ]
        if self.tags:
            lines.append("Hashtags : " + " ".join(f"#{t}" for t in self.tags))
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Gemini — schéma de sortie structurée
# --------------------------------------------------------------------------- #
class MediaIdentification(BaseModel):
    """Schéma JSON imposé à Gemini via ``response_schema``."""

    identified: bool = Field(description="true si une œuvre précise a été identifiée avec certitude raisonnable.")
    title: Optional[str] = Field(None, description="Titre le plus connu (de préférence le titre international/anglais).")
    original_title: Optional[str] = Field(None, description="Titre original dans la langue d'origine, si différent.")
    year: Optional[int] = Field(None, description="Année de sortie (film) ou de première diffusion (série).")
    media_type: Optional[Literal["movie", "tv"]] = Field(
        None, description="'movie' pour un film, 'tv' pour une série ou un anime en épisodes."
    )
    is_anime: bool = Field(description="true s'il s'agit d'une animation japonaise (anime).")
    confidence: float = Field(description="Confiance entre 0.0 et 1.0.")
    reasoning: str = Field("", description="Justification courte (une ou deux phrases).")

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


# --------------------------------------------------------------------------- #
# API — sortie
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    REQUESTED = "requested"
    ALREADY_AVAILABLE = "already_available"
    ALREADY_REQUESTED = "already_requested"
    IDENTIFIED = "identified"  # dry_run
    NOT_IDENTIFIED = "not_identified"
    NOT_FOUND = "not_found"


class MediaResult(BaseModel):
    tmdb_id: int
    media_type: Literal["movie", "tv"]
    title: str
    year: Optional[int] = None
    is_anime: bool = False
    poster_url: Optional[str] = None


class MovomaticResponse(BaseModel):
    status: Status
    message: str = Field(description="Message court, prêt à être affiché dans une notification iOS.")
    source: Optional[Literal["metadata", "video"]] = None
    identification: Optional[MediaIdentification] = None
    media: Optional[MediaResult] = None
    request_id: Optional[int] = None


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    message: str


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
