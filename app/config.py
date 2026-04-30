"""Application configuration loaded from environment variables."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_debug: bool = False
    log_level: str = "INFO"

    session_secret: str = Field(..., min_length=16)
    encryption_key: str = Field(..., min_length=16)

    mongo_uri: str = "mongodb://127.0.0.1:27017"
    mongo_db: str = "discord"

    discord_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    discord_api_base: str = "https://discord.com/api/v9"
    discord_http_timeout: int = 20

    ai_provider: str = "anthropic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
