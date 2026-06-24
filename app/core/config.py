from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AITU Music Club"
    environment: str = "local"
    database_url: str = "postgresql+asyncpg://aitu:aitu@localhost:5432/aitu_music"
    bot_token: str = ""
    backend_base_url: str = "http://localhost:8000"
    internal_api_token: str = "change-me"
    redis_url: str = "redis://localhost:6379/0"
    initial_admin_telegram_ids: str = ""
    registration_retry_hours: int = 24
    default_language: str = "ru"
    timezone: str = "Asia/Almaty"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def initial_admin_ids(self) -> set[int]:
        values = [item.strip() for item in self.initial_admin_telegram_ids.split(",")]
        return {int(item) for item in values if item.isdigit()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
