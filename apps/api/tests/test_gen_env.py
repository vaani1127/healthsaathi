"""`make env` (app.scripts.gen_env) writes a .env the API settings accept (AUDIT_REPORT M1)."""

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import REPO_ROOT, Settings
from app.core.crypto import KeyRing
from app.scripts import gen_env

SECRETS = ("JWT_SECRET", "OTP_HMAC_KEY", "DATA_KEYS", "LEDGER_SIGNING_SEED")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder with the real .env.example, and no secrets from the test environment, so the
    settings can only come from the file."""
    shutil.copy(REPO_ROOT / ".env.example", tmp_path / ".env.example")
    monkeypatch.setattr(gen_env, "REPO_ROOT", tmp_path)
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_make_env_writes_settings_the_api_accepts(repo: Path) -> None:
    assert gen_env.main() == 0
    settings = Settings(_env_file=repo / ".env")
    assert len(settings.jwt_secret.get_secret_value()) >= 32
    assert len(settings.otp_hmac_key.get_secret_value()) >= 32
    assert len(settings.ledger_signing_seed.get_secret_value()) >= 32
    KeyRing(settings.data_keys.get_secret_value())


def test_a_copied_env_example_is_left_alone_and_does_not_load(repo: Path) -> None:
    # The old README step: copying .env.example first leaves the secrets empty, and make env
    # (run by make dev) does not touch an existing .env.
    shutil.copy(repo / ".env.example", repo / ".env")
    before = (repo / ".env").read_text(encoding="utf-8")
    assert gen_env.main() == 0
    assert (repo / ".env").read_text(encoding="utf-8") == before
    with pytest.raises(ValidationError):
        Settings(_env_file=repo / ".env")
