"""
Encryption at rest for Telegram session strings and bot tokens (audit H1).

Stored form:  "enc:v1:<fernet token>"
Key:          SESSION_KEY env var (any string; a Fernet key or a passphrase).
              If SESSION_KEY is not set, a key is derived from API_ID + API_HASH so
              the bot still works, and a warning is logged. Set SESSION_KEY for real
              protection: the database alone is then not enough to read sessions.

Decryption tries every known key, so you can add SESSION_KEY later: old values
keep working and are re-encrypted with the new key by migrate_secrets() on startup.
Values without the prefix are treated as legacy plaintext (still readable).
"""
import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"
SECRET_FIELDS = ("session", "token")


def _fernet_from(material: str) -> Fernet:
    material = (material or "").strip()
    try:
        raw = base64.urlsafe_b64decode(material.encode())
        if len(raw) == 32:
            return Fernet(material.encode())
    except Exception:
        pass
    digest = hashlib.sha256(("tgfwd-session-key:" + material).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _build():
    keys = []
    env_key = os.environ.get("SESSION_KEY", "").strip()
    if env_key:
        keys.append(_fernet_from(env_key))
    fallback = f"{os.environ.get('API_ID', '')}:{os.environ.get('API_HASH', '')}"
    if fallback != ":":
        keys.append(_fernet_from("fallback:" + fallback))
    if not keys:
        keys.append(_fernet_from("fallback:unset"))
    if not env_key:
        logger.warning(
            "SESSION_KEY is not set: sessions/tokens are encrypted with a key derived "
            "from API_ID/API_HASH. Set SESSION_KEY for stronger protection."
        )
    return MultiFernet(keys), bool(env_key)


_BOX, HAS_ENV_KEY = _build()


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value):
    """Encrypt a str; None/empty and already-encrypted values are returned unchanged."""
    if not value or not isinstance(value, str) or is_encrypted(value):
        return value
    return PREFIX + _BOX.encrypt(value.encode()).decode()


def decrypt(value):
    """Return plaintext. Legacy plaintext is returned as is."""
    if not is_encrypted(value):
        return value
    try:
        return _BOX.decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        raise ValueError(
            "Stored session/token cannot be decrypted (SESSION_KEY changed?). "
            "Remove it in /settings and add it again."
        )


def needs_rotation(value) -> bool:
    """True if the value is plaintext or not encrypted with the primary key."""
    if not value or not isinstance(value, str):
        return False
    if not is_encrypted(value):
        return True
    try:
        _BOX._fernets[0].decrypt(value[len(PREFIX):].encode())
        return False
    except Exception:
        return True


def rotate(value):
    if not value or not isinstance(value, str):
        return value
    return encrypt(decrypt(value)) if is_encrypted(value) and needs_rotation(value) else encrypt(value)


def seal(data: dict) -> dict:
    """Copy of a bot/session record with secret fields encrypted."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for f in SECRET_FIELDS:
        if out.get(f):
            out[f] = encrypt(out[f])
    return out


def unseal(data: dict) -> dict:
    """Copy of a bot/session record with secret fields decrypted."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for f in SECRET_FIELDS:
        if out.get(f):
            out[f] = decrypt(out[f])
    return out
