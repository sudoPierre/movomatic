"""Title identification with Gemini (text analysis first, then video fallback)."""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.config import Settings
from app.extractor import guess_mime_type
from app.models import ReelAnalysis, ReelMetadata

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a film, TV series and anime expert. You receive data from an Instagram Reel
(caption, hashtags, author and sometimes the video itself). Your task is to identify EVERY
movie, TV series or anime the Reel features, so they can all be requested on a media server.

Rules:
- Use every clue: explicit titles, hashtags, actor names, characters, quotes, scenes,
  on-screen text, soundtrack, the author's handle (e.g. a studio or distributor account).
- A Reel can feature one work or several (top 10, "movies like...", compilations,
  recommendations). List each distinct work once in "items", in order of appearance.
- "works_featured" is how many distinct works the Reel features, including the ones you
  could not identify (e.g. a "top 5" whose titles are not in the caption is 5).
- "title" must be the most widely known title, preferably the international/English title
  as listed on TMDB. Put the original-language title in "original_title" when different.
- "media_type" is "movie" for a feature film, "tv" for a series, mini-series or episodic anime.
  An anime movie is "movie" with is_anime=true.
- "is_anime" is true only for Japanese animation.
- "year" is the release year (movie) or first air year (series).
- Ignore works only mentioned in passing (e.g. "from the director of X") unless the Reel
  is clearly recommending them too.
- Never invent a title. Only list works you are reasonably sure about; give each its own
  "confidence" from 0.0 to 1.0. "reasoning" is one short sentence.
"""


# Errors worth retrying with the next model: retired model, quota, overload, timeout.
RETRYABLE_CODES = {404, 408, 429, 500, 502, 503, 504}
# Short overload spikes: retry the same model a couple of times before switching.
TRANSIENT_CODES = {429, 500, 502, 503}
TRANSIENT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0
# Pause between two full rounds over every model (bounded by GEMINI_PATIENCE_SECONDS).
ROUND_PAUSE_SECONDS = 20.0


class AnalysisError(RuntimeError):
    """Gemini could not produce a usable answer."""


class GeminiAnalyzer:
    def __init__(self, settings: Settings, client: Optional[genai.Client] = None) -> None:
        self._settings = settings
        self._client = client or genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        self._config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=ReelAnalysis,
            temperature=0.1,
            # No tools are used: skip the SDK's function-calling loop (and its log line).
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    async def analyze_metadata(self, meta: ReelMetadata) -> ReelAnalysis:
        prompt = "Identify the works featured in this Instagram Reel data:\n\n" + meta.as_prompt_text()
        return await self._generate([prompt])

    async def analyze_video(self, video: Path, meta: ReelMetadata) -> ReelAnalysis:
        client = self._client
        uploaded = await client.aio.files.upload(
            file=video,
            config=types.UploadFileConfig(mime_type=guess_mime_type(video) or "video/mp4"),
        )
        try:
            uploaded = await self._wait_until_active(uploaded)
            prompt = (
                "Identify every work shown in this Instagram Reel video. "
                "Look at the scenes, characters, on-screen text and listen to the audio. "
                "Additional context from the post:\n\n" + meta.as_prompt_text()
            )
            return await self._generate([uploaded, prompt])
        finally:
            if uploaded.name:
                try:
                    await client.aio.files.delete(name=uploaded.name)
                except Exception:  # noqa: BLE001 — cleanup must never fail the request
                    logger.warning("Could not delete Gemini file %s", uploaded.name)

    async def _wait_until_active(self, file: types.File) -> types.File:
        deadline = asyncio.get_running_loop().time() + self._settings.gemini_timeout_seconds
        while file.state == types.FileState.PROCESSING:
            if asyncio.get_running_loop().time() > deadline:
                raise AnalysisError("timed out while Gemini was processing the video")
            await asyncio.sleep(2)
            file = await self._client.aio.files.get(name=file.name)
        if file.state == types.FileState.FAILED:
            raise AnalysisError("Gemini failed to process the video")
        return file

    async def _try_models(self, contents: list) -> tuple[Optional[types.GenerateContentResponse], str, str]:
        """One round over the configured models. Returns (response, model, last_error)."""
        models = self._settings.gemini_models
        last_error = ""
        for index, model in enumerate(models):
            for attempt in range(1 + TRANSIENT_RETRIES):
                try:
                    response = await asyncio.wait_for(
                        self._client.aio.models.generate_content(
                            model=model, contents=contents, config=self._config
                        ),
                        timeout=self._settings.gemini_timeout_seconds,
                    )
                    return response, model, ""
                except asyncio.TimeoutError:
                    last_error = f"{model} timed out"
                    break  # a slow model will not get faster: move on
                except genai_errors.APIError as exc:
                    # Bad request / key problems will not get better with another model.
                    if exc.code not in RETRYABLE_CODES:
                        raise AnalysisError(f"Gemini API error ({exc.code}): {exc.message}") from exc
                    last_error = f"{model} returned {exc.code}"
                    if exc.code not in TRANSIENT_CODES or attempt == TRANSIENT_RETRIES:
                        break
                    logger.info("Gemini: %s (attempt %d)", last_error, attempt + 1)
                    # Quota errors (429) need a longer pause than overload spikes (5xx).
                    factor = 3 if exc.code == 429 else 1
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * factor * (attempt + 1))
            if index + 1 < len(models):
                logger.warning("Gemini: %s, falling back to %s", last_error, models[index + 1])
        return None, "", last_error

    async def _generate(self, contents: list) -> ReelAnalysis:
        """Go through every model; when all are overloaded, pause and start another round."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.gemini_patience_seconds
        round_number = 1
        while True:
            response, model, last_error = await self._try_models(contents)
            if response is not None:
                break
            remaining = deadline - loop.time()
            if remaining <= ROUND_PAUSE_SECONDS:
                raise AnalysisError(f"Gemini unavailable ({last_error})")
            logger.warning(
                "Gemini: every model failed (round %d, %s), retrying in %.0fs",
                round_number, last_error, ROUND_PAUSE_SECONDS,
            )
            await asyncio.sleep(ROUND_PAUSE_SECONDS)
            round_number += 1

        parsed = response.parsed
        if isinstance(parsed, ReelAnalysis):
            result = parsed
        else:
            try:
                result = ReelAnalysis.model_validate_json(response.text or "")
            except ValueError as exc:
                raise AnalysisError("unreadable Gemini response") from exc
        logger.info(
            "Gemini (%s): %d work(s) featured, identified: %s",
            model, result.works_featured,
            ", ".join(f"{i.label} [{i.media_type}{' anime' if i.is_anime else ''} {i.confidence:.2f}]"
                      for i in result.items) or "none",
        )
        return result
