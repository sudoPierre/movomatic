"""Identification de l'œuvre via Gemini (analyse du texte, puis fallback sur la vidéo)."""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.config import Settings
from app.extractor import guess_mime_type
from app.models import MediaIdentification, ReelMetadata

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a film, TV series and anime expert. You receive data from an Instagram Reel
(caption, hashtags, author and sometimes the video itself). Your task is to identify the
SINGLE movie, TV series or anime the Reel is about, so it can be requested on a media server.

Rules:
- Use every clue: explicit titles, hashtags, actor names, characters, quotes, scenes,
  on-screen text, soundtrack, the author's handle (e.g. a studio or distributor account).
- "title" must be the most widely known title, preferably the international/English title
  as listed on TMDB. Put the original-language title in "original_title" when different.
- "media_type" is "movie" for a feature film, "tv" for a series, mini-series or episodic anime.
  An anime movie is "movie" with is_anime=true.
- "is_anime" is true only for Japanese animation.
- "year" is the release year (movie) or first air year (series).
- If several works are mentioned (top 10, compilation...), pick the main one or the first one;
  if none stands out, set identified=false.
- Never invent a title. If you are not reasonably sure, set identified=false and a low confidence.
- "confidence" ranges from 0.0 to 1.0. "reasoning" is one or two short sentences.
"""


class AnalysisError(RuntimeError):
    """Gemini n'a pas pu produire une réponse exploitable."""


class GeminiAnalyzer:
    def __init__(self, settings: Settings, client: Optional[genai.Client] = None) -> None:
        self._settings = settings
        self._client = client or genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        self._config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=MediaIdentification,
            temperature=0.1,
        )

    async def analyze_metadata(self, meta: ReelMetadata) -> MediaIdentification:
        prompt = "Identify the work from this Instagram Reel data:\n\n" + meta.as_prompt_text()
        return await self._generate([prompt])

    async def analyze_video(self, video: Path, meta: ReelMetadata) -> MediaIdentification:
        client = self._client
        uploaded = await client.aio.files.upload(
            file=video,
            config=types.UploadFileConfig(mime_type=guess_mime_type(video) or "video/mp4"),
        )
        try:
            uploaded = await self._wait_until_active(uploaded)
            prompt = (
                "Identify the work shown in this Instagram Reel video. "
                "Look at the scenes, characters, on-screen text and listen to the audio. "
                "Additional context from the post:\n\n" + meta.as_prompt_text()
            )
            return await self._generate([uploaded, prompt])
        finally:
            if uploaded.name:
                try:
                    await client.aio.files.delete(name=uploaded.name)
                except Exception:  # noqa: BLE001 — le nettoyage ne doit jamais faire échouer la requête
                    logger.warning("Impossible de supprimer le fichier Gemini %s", uploaded.name)

    async def _wait_until_active(self, file: types.File) -> types.File:
        deadline = asyncio.get_running_loop().time() + self._settings.gemini_timeout_seconds
        while file.state == types.FileState.PROCESSING:
            if asyncio.get_running_loop().time() > deadline:
                raise AnalysisError("délai dépassé pendant le traitement de la vidéo par Gemini")
            await asyncio.sleep(2)
            file = await self._client.aio.files.get(name=file.name)
        if file.state == types.FileState.FAILED:
            raise AnalysisError("Gemini n'a pas pu traiter la vidéo")
        return file

    async def _generate(self, contents: list) -> MediaIdentification:
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._settings.gemini_model,
                    contents=contents,
                    config=self._config,
                ),
                timeout=self._settings.gemini_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise AnalysisError("délai dépassé lors de l'appel à Gemini") from exc
        except genai_errors.APIError as exc:
            raise AnalysisError(f"erreur API Gemini ({exc.code}) : {exc.message}") from exc

        parsed = response.parsed
        if isinstance(parsed, MediaIdentification):
            result = parsed
        else:
            text = response.text or ""
            try:
                result = MediaIdentification.model_validate_json(text)
            except ValueError as exc:
                raise AnalysisError("réponse Gemini illisible") from exc
        logger.info(
            "Gemini: identified=%s title=%r year=%s type=%s anime=%s confidence=%.2f",
            result.identified, result.title, result.year,
            result.media_type, result.is_anime, result.confidence,
        )
        return result
