from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AITU Music Club"
    environment: str = "local"
    log_level: str = "INFO"
    sql_echo: bool = False
    database_url: str = "postgresql+asyncpg://aitu:aitu@localhost:5432/aitu_music"
    bot_token: str = ""
    backend_base_url: str = "http://localhost:8000"
    webapp_base_url: str = ""
    ngrok_api_url: str = "http://ngrok:4040/api/tunnels"
    ngrok_authtoken: str = ""
    ngrok_domain: str = ""
    internal_api_token: str = "change-me"
    redis_url: str = "redis://localhost:6379/0"
    initial_admin_telegram_ids: str = ""
    registration_retry_hours: int = 24
    default_language: str = "ru"
    timezone: str = "Asia/Almaty"
    weekly_planning_enabled: bool = False
    allocation_time: str = "18:00"
    google_sheets_id: str = ""
    google_service_account_file: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def initial_admin_ids(self) -> set[int]:
        values = [item.strip() for item in self.initial_admin_telegram_ids.split(",")]
        return {int(item) for item in values if item.isdigit()}


MIN_INTERNAL_TOKEN_LENGTH = 32


def validate_internal_token(loaded: Settings) -> None:
    token = loaded.internal_api_token
    if not token.strip():
        raise RuntimeError("INTERNAL_API_TOKEN must not be empty")
    if loaded.environment != "local" and (token == "change-me" or len(token) < MIN_INTERNAL_TOKEN_LENGTH):
        raise RuntimeError(
            f"INTERNAL_API_TOKEN must be a non-default secret of at least {MIN_INTERNAL_TOKEN_LENGTH} characters "
            "when ENVIRONMENT is not 'local' (generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\")"
        )


@lru_cache
def get_settings() -> Settings:
    loaded = Settings()
    validate_internal_token(loaded)
    return loaded


settings = get_settings()
