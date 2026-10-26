"""Pydantic schemas: API input/output and Gemini structured-output schema."""

import re
from enum import Enum
from typing import Literal, Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am"}
_INSTAGRAM_PATHS = ("/reel/", "/reels/", "/p/", "/tv/")


# --------------------------------------------------------------------------- #
# API — input
# --------------------------------------------------------------------------- #
class MovomaticRequest(BaseModel):
    """Body sent by the iOS Shortcut.

    The ``url`` field also accepts shared text containing the URL
    (the iOS share sheet sometimes adds text around the link).
    """

    url: str = Field(..., max_length=2048, examples=["https://www.instagram.com/reel/C1a2B3c4D5e/"])
    dry_run: bool = Field(False, description="Identify without sending a request to Jellyseerr.")

    @field_validator("url")
    @classmethod
    def _normalize_instagram_url(cls, v: str) -> str:
        match = _URL_RE.search(v.strip())
        if not match:
            raise ValueError("no URL found in the 'url' field")
        parts = urlsplit(match.group(0).rstrip(").,;!?\"'"))
        host = (parts.hostname or "").lower()
        if host not in _INSTAGRAM_HOSTS:
            raise ValueError("the URL must point to instagram.com")
        path = parts.path if parts.path.endswith("/") else parts.path + "/"
        if not path.startswith(_INSTAGRAM_PATHS):
            raise ValueError("the URL must be an Instagram Reel or post")
        # Strip tracking parameters (?igsh=..., ?utm_source=...).
        return urlunsplit(("https", "www.instagram.com", path, "", ""))


# --------------------------------------------------------------------------- #
# yt-dlp extraction
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
            f"Author: {self.uploader or 'unknown'}",
            f"Title: {self.title or ''}",
            f"Caption: {self.description or ''}",
        ]
        if self.tags:
            lines.append("Hashtags: " + " ".join(f"#{t}" for t in self.tags))
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Gemini — structured output schema
# --------------------------------------------------------------------------- #
class MediaIdentification(BaseModel):
    """One work featured in the Reel (item of ``ReelAnalysis.items``)."""

    title: str = Field(description="Best-known title (preferably the international/English title).")
    original_title: Optional[str] = Field(None, description="Original-language title, if different.")
    year: Optional[int] = Field(None, description="Release year (movie) or first air year (series).")
    media_type: Optional[Literal["movie", "tv"]] = Field(
        None, description="'movie' for a film, 'tv' for a series or episodic anime."
    )
    is_anime: bool = Field(description="true if this is Japanese animation (anime).")
    confidence: float = Field(description="Confidence between 0.0 and 1.0 for this specific title.")
    reasoning: str = Field("", description="Short justification (one sentence).")

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


class ReelAnalysis(BaseModel):
    """JSON schema enforced on Gemini through ``response_schema``."""

    works_featured: int = Field(
        description="Number of distinct movies/series/anime the Reel features, "
        "including the ones you could not identify. 0 if it is not about any."
    )
    items: list[MediaIdentification] = Field(
        description="Every work you could identify, in order of appearance. Empty if none."
    )


# --------------------------------------------------------------------------- #
# API — output
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    REQUESTED = "requested"
    IDENTIFIED = "identified"  # dry_run
    ALREADY_REQUESTED = "already_requested"
    ALREADY_AVAILABLE = "already_available"
    NOT_FOUND = "not_found"
    ERROR = "error"
    NOT_IDENTIFIED = "not_identified"
    PROCESSING = "processing"  # answered early, the job continues in the background


class MediaResult(BaseModel):
    tmdb_id: int
    media_type: Literal["movie", "tv"]
    title: str
    year: Optional[int] = None
    is_anime: bool = False
    poster_url: Optional[str] = None


class ItemResult(BaseModel):
    """Outcome for one title found in the Reel."""

    status: Status
    message: str
    identification: MediaIdentification
    media: Optional[MediaResult] = None
    request_id: Optional[int] = None


class MovomaticResponse(BaseModel):
    status: Status = Field(description="Best outcome across all titles (requested > identified > ...).")
    message: str = Field(description="Ready to be shown in an iOS notification (one line per title).")
    source: Optional[Literal["metadata", "video"]] = None
    works_featured: int = Field(0, description="Number of works Gemini thinks the Reel features.")
    results: list[ItemResult] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    message: str


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
