"""Application settings, loaded from the environment or a .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Movomatic API security ---
    movomatic_api_key: SecretStr = Field(
        ..., min_length=16, description="Key expected in the X-API-Key header."
    )

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = Field(8000, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    max_concurrent_jobs: int = Field(2, ge=1, le=16)

    # --- Gemini ---
    gemini_api_key: SecretStr
    gemini_model: str = "gemini-3.5-flash"
    # Tried in order when the main model is overloaded, rate-limited or retired.
    gemini_fallback_models: str = "gemini-3.8-flash,gemini-flash-latest"
    gemini_min_confidence: float = Field(0.6, ge=0.0, le=1.0)
    gemini_video_fallback: bool = True
    gemini_timeout_seconds: int = Field(45, ge=5)

    # --- yt-dlp ---
    ytdlp_cookies_file: Optional[Path] = None
    ytdlp_max_filesize_mb: int = Field(100, ge=1)
    ytdlp_max_duration_seconds: int = Field(600, ge=10)
    temp_dir: Optional[Path] = None

    # --- Jellyseerr ---
    jellyseerr_url: AnyHttpUrl
    jellyseerr_api_key: SecretStr
    jellyseerr_timeout_seconds: float = Field(20.0, gt=0)
    jellyseerr_verify_ssl: bool = True
    jellyseerr_request_4k: bool = False
    # Jellyseerr user ID to make requests on behalf of (optional).
    jellyseerr_user_id: Optional[int] = None
    # Optional anime overrides (otherwise Jellyseerr applies its own rules).
    anime_sonarr_server_id: Optional[int] = None
    anime_sonarr_profile_id: Optional[int] = None
    anime_sonarr_root_folder: Optional[str] = None
    anime_radarr_server_id: Optional[int] = None
    anime_radarr_profile_id: Optional[int] = None
    anime_radarr_root_folder: Optional[str] = None

    @field_validator("movomatic_api_key")
    @classmethod
    def _reject_placeholder(cls, v: SecretStr) -> SecretStr:
        if v.get_secret_value().startswith("change-me"):
            raise ValueError("replace the example value (openssl rand -hex 32)")
        return v

    @field_validator("ytdlp_cookies_file", "temp_dir", mode="before")
    @classmethod
    def _empty_path_is_none(cls, v):
        return v or None

    @field_validator(
        "jellyseerr_user_id",
        "anime_sonarr_server_id",
        "anime_sonarr_profile_id",
        "anime_sonarr_root_folder",
        "anime_radarr_server_id",
        "anime_radarr_profile_id",
        "anime_radarr_root_folder",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, v):
        return None if v == "" else v

    @property
    def gemini_models(self) -> list[str]:
        models = [self.gemini_model]
        for name in self.gemini_fallback_models.split(","):
            if name.strip() and name.strip() not in models:
                models.append(name.strip())
        return models

    @property
    def jellyseerr_base_url(self) -> str:
        return str(self.jellyseerr_url).rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
