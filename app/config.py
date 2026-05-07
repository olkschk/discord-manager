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
        "Chrome/147.0.0.0 Safari/537.36"
    )
    discord_api_base: str = "https://discord.com/api/v9"
    discord_http_timeout: int = 20

    ai_provider: str = "anthropic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    monitor_enabled: bool = True
    monitor_topic_interval: int = 60
    monitor_dm_interval: int = 60

    imap_default_host: str = ""
    imap_default_port: int = 993
    imap_fetch_limit: int = 10
    imap_timeout: int = 25

    discord_gateway_url: str = "wss://gateway.discord.gg/?v=9&encoding=json"
    gateway_identify_os: str = "Windows"
    gateway_identify_browser: str = "Chrome"

    sounds_dir: str = r"C:\Users\olksc\OneDrive\Desktop\Discord\sounds"

    captcha_provider: str = "disabled"  # capsolver | twocaptcha | capmonster | anticaptcha | disabled
    captcha_api_key: str = ""
    captcha_poll_interval: int = 3
    captcha_poll_attempts: int = 40
    captcha_timeout: int = 180
    captcha_default_page_url: str = "https://discord.com/login"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
