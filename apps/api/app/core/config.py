from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["local", "test", "staging", "prod"] = Field(default="local", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    database_url: str = Field(
        default="postgresql+asyncpg://healthsaathi:healthsaathi@localhost:5432/healthsaathi",
        alias="DATABASE_URL",
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173"], alias="CORS_ORIGINS"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
