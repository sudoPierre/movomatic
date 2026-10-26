"""Extraction des Reels Instagram via yt-dlp (métadonnées + téléchargement temporaire)."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import yt_dlp

from app.config import Settings
from app.models import ReelMetadata

logger = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    """Le Reel n'a pas pu être lu (privé, supprimé, connexion requise, etc.)."""


class _YtdlpLogger:
    """Redirige les messages yt-dlp vers le logging standard."""

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            logger.debug(msg)

    def info(self, msg: str) -> None:
        logger.debug(msg)

    def warning(self, msg: str) -> None:
        logger.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.error("yt-dlp: %s", msg)


class ReelExtractor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 3,
            "logger": _YtdlpLogger(),
        }
        cookies = self._settings.ytdlp_cookies_file
        if cookies:
            if cookies.is_file():
                opts["cookiefile"] = str(cookies)
            else:
                logger.warning("Fichier de cookies introuvable : %s", cookies)
        return opts

    # ------------------------------------------------------------------ #
    # Métadonnées
    # ------------------------------------------------------------------ #
    def _extract_metadata_sync(self, url: str) -> ReelMetadata:
        opts = self._base_opts() | {"skip_download": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise ExtractionError(_clean_error(exc)) from exc
        if not info:
            raise ExtractionError("aucune donnée renvoyée par yt-dlp")
        # Un carrousel (/p/) peut renvoyer une playlist : on prend la première vidéo.
        if info.get("_type") == "playlist" and info.get("entries"):
            first = next((e for e in info["entries"] if e), None)
            info = {**info, **(first or {})}
        return ReelMetadata(
            url=url,
            id=info.get("id"),
            title=info.get("title") or info.get("fulltitle"),
            description=info.get("description"),
            uploader=info.get("uploader") or info.get("channel") or info.get("uploader_id"),
            duration=info.get("duration"),
            tags=[t for t in (info.get("tags") or []) if isinstance(t, str)],
        )

    async def extract_metadata(self, url: str) -> ReelMetadata:
        return await asyncio.to_thread(self._extract_metadata_sync, url)

    # ------------------------------------------------------------------ #
    # Téléchargement vidéo (fallback)
    # ------------------------------------------------------------------ #
    def _download_sync(self, url: str, dest_dir: Path) -> Path:
        s = self._settings
        opts = self._base_opts() | {
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
            # 720p max avec audio : suffisant pour Gemini et économe en tokens.
            "format": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
            "merge_output_format": "mp4",
            "max_filesize": s.ytdlp_max_filesize_mb * 1024 * 1024,
            "match_filter": yt_dlp.utils.match_filter_func(
                f"duration <=? {s.ytdlp_max_duration_seconds}"
            ),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as exc:
            raise ExtractionError(_clean_error(exc)) from exc

        files = [p for p in dest_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise ExtractionError(
                "vidéo non téléchargée (trop longue, trop lourde ou indisponible)"
            )
        return max(files, key=lambda p: p.stat().st_size)

    async def download_video(self, url: str, dest_dir: Path) -> Path:
        return await asyncio.to_thread(self._download_sync, url, dest_dir)


def _clean_error(exc: Exception) -> str:
    msg = str(exc)
    return msg.removeprefix("ERROR: ").strip() or exc.__class__.__name__


def guess_mime_type(path: Path) -> Optional[str]:
    return {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
    }.get(path.suffix.lower())
