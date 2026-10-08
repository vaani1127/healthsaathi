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

    # Detection (SPEC 5.5). Fitted models are files here; their sha256 is the model_version.
    model_dir: str | None = Field(default=None, alias="MODEL_DIR")
    alert_budget: int = Field(default=5, ge=1, alias="ALERT_BUDGET")
    detect_scorer: Literal["iforest", "ecod", "copod"] = Field(
        default="iforest", alias="DETECT_SCORER"
    )
    detect_interval_seconds: int = Field(default=60, ge=0, alias="DETECT_INTERVAL_SECONDS")

    # Files (lab reports). Local folder unless an Azure container SAS URL is set.
    storage_dir: str | None = Field(default=None, alias="STORAGE_DIR")
    azure_blob_container_sas_url: SecretStr | None = Field(
        default=None, alias="AZURE_BLOB_CONTAINER_SAS_URL"
    )
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")

    # Anchoring (SPEC 6). Every backend is optional; unset means that witness is skipped.
    anchor_rpc_url: str | None = Field(default=None, alias="ANCHOR_RPC_URL")
    anchor_chain: Literal["amoy", "besu"] = Field(default="amoy", alias="ANCHOR_CHAIN")
    audit_anchor_address: str | None = Field(default=None, alias="AUDIT_ANCHOR_ADDRESS")
    clinic_registry_address: str | None = Field(default=None, alias="CLINIC_REGISTRY_ADDRESS")
    anchor_poster_private_key: SecretStr | None = Field(
        default=None, alias="ANCHOR_POSTER_PRIVATE_KEY"
    )
    anchor_explorer_url: str | None = Field(default=None, alias="ANCHOR_EXPLORER_URL")
    anchor_tx_timeout_seconds: int = Field(default=120, alias="ANCHOR_TX_TIMEOUT_SECONDS")
    anchor_trigger_token: SecretStr | None = Field(default=None, alias="ANCHOR_TRIGGER_TOKEN")
    # The worker runs the anchor job itself when no run has happened for this long. 0 turns it off.
    anchor_fallback_minutes: int = Field(default=30, ge=0, alias="ANCHOR_FALLBACK_MINUTES")
    witness_repo: str | None = Field(default=None, alias="WITNESS_REPO")
    witness_branch: str = Field(default="main", alias="WITNESS_BRANCH")
    witness_github_token: SecretStr | None = Field(default=None, alias="WITNESS_GITHUB_TOKEN")
    ots_enabled: bool = Field(default=False, alias="OTS_ENABLED")
    ots_calendars: list[str] = Field(
        default_factory=lambda: [
            "https://a.pool.opentimestamps.org",
            "https://b.pool.opentimestamps.org",
            "https://a.pool.eternitywall.com",
        ],
        alias="OTS_CALENDARS",
    )

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
        "model_dir",
        "anchor_rpc_url",
        "audit_anchor_address",
        "clinic_registry_address",
        "anchor_poster_private_key",
        "anchor_explorer_url",
        "anchor_trigger_token",
        "witness_repo",
        "witness_github_token",
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
