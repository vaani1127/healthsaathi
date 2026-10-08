from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["local", "test", "staging", "prod"] = Field(default="local", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    database_url: str = Field(
        default="postgresql+asyncpg://healthsaathi:healthsaathi@localhost:5432/healthsaathi",
        alias="DATABASE_URL",
    )
    # Used only by Alembic and the seed script. It must be able to create roles and own tables.
    migrator_database_url: str = Field(
        default="postgresql+asyncpg://healthsaathi:healthsaathi@localhost:5432/healthsaathi",
        alias="MIGRATOR_DATABASE_URL",
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173"], alias="CORS_ORIGINS"
    )
    public_app_url: str = Field(default="http://localhost:5173", alias="PUBLIC_APP_URL")

    # Secrets. Generate local values with `make env`.
    jwt_secret: SecretStr = Field(alias="JWT_SECRET")
    # Comma separated "key_id:base64_key" pairs for AES-256-GCM. The first one encrypts new data.
    data_keys: SecretStr = Field(alias="DATA_KEYS")
    otp_hmac_key: SecretStr = Field(alias="OTP_HMAC_KEY")
    # Seed for the per-clinic Ed25519 keys that sign Merkle checkpoints (HKDF per clinic).
    ledger_signing_seed: SecretStr = Field(alias="LEDGER_SIGNING_SEED")

    access_token_minutes: int = 15
    refresh_token_days: int = Field(default=14, alias="REFRESH_TOKEN_DAYS")
    cookie_domain: str | None = Field(default=None, alias="COOKIE_DOMAIN")
    cookie_secure: bool = Field(default=True, alias="COOKIE_SECURE")
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")

    # Files (lab reports). Local folder unless an Azure container SAS URL is set.
    storage_dir: str | None = Field(default=None, alias="STORAGE_DIR")
    azure_blob_container_sas_url: SecretStr | None = Field(
        default=None, alias="AZURE_BLOB_CONTAINER_SAS_URL"
    )
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")

    email_backend: Literal["console", "brevo"] = Field(default="console", alias="EMAIL_BACKEND")
    brevo_api_key: SecretStr | None = Field(default=None, alias="BREVO_API_KEY")
    email_from: str = Field(default="no-reply@healthsaathi.test", alias="EMAIL_FROM")
    email_from_name: str = Field(default="HealthSaathi", alias="EMAIL_FROM_NAME")
    # Local and test only: also write console emails as files here (used by browser tests).
    email_outbox_dir: str | None = Field(default=None, alias="EMAIL_OUTBOX_DIR")

    @field_validator(
        "cookie_domain",
        "brevo_api_key",
        "storage_dir",
        "azure_blob_container_sas_url",
        "email_outbox_dir",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("jwt_secret", "otp_hmac_key", "ledger_signing_seed")
    @classmethod
    def _long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("must be at least 32 characters")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
