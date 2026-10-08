"""Clinical note bodies are stored with AES-256-GCM (SPEC 4). The note id is the associated data,
so an encrypted body cannot be moved to another note row."""

import uuid

from app.core.crypto import get_keyring


def encrypt_body(note_id: uuid.UUID, body: str) -> bytes:
    return get_keyring().encrypt(body.encode("utf-8"), aad=f"note:{note_id}".encode())


def decrypt_body(note_id: uuid.UUID, blob: bytes) -> str:
    return get_keyring().decrypt(blob, aad=f"note:{note_id}".encode()).decode("utf-8")
