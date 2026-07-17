"""Symmetric encryption for account credentials at rest.

Passwords are never stored in plaintext. We use Fernet (AES-128-CBC + HMAC)
from the ``cryptography`` package. The master key is kept *outside* the
database:

1. Preferred: the OS keyring (Windows Credential Manager / macOS Keychain /
   Secret Service on Linux) via the ``keyring`` package.
2. Fallback: a ``secret.key`` file in the app directory with 0600 permissions
   (used when no keyring backend is available, e.g. a headless Linux box).

If the key is lost, stored passwords cannot be recovered -- the user simply
re-enters credentials. That is an acceptable trade-off for a local tool.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config

log = logging.getLogger(__name__)


class CryptoError(Exception):
    """Raised when encryption or decryption fails."""


def _load_key_from_keyring() -> bytes | None:
    try:
        import keyring
        from keyring.errors import KeyringError

        try:
            value = keyring.get_password(
                config.KEYRING_SERVICE, config.KEYRING_USERNAME
            )
        except KeyringError as exc:  # backend present but failed
            log.warning("Keyring read failed, will use file key: %s", exc)
            return None
        return value.encode("utf-8") if value else None
    except Exception as exc:  # keyring not installed / no backend
        log.info("Keyring unavailable (%s); using file-based key.", exc)
        return None


def _store_key_in_keyring(key: bytes) -> bool:
    try:
        import keyring
        from keyring.errors import KeyringError

        try:
            keyring.set_password(
                config.KEYRING_SERVICE,
                config.KEYRING_USERNAME,
                key.decode("utf-8"),
            )
            return True
        except KeyringError as exc:
            log.warning("Keyring write failed, will use file key: %s", exc)
            return False
    except Exception as exc:
        log.info("Keyring unavailable for storing key (%s).", exc)
        return False


def _load_key_from_file() -> bytes | None:
    path: Path = config.KEY_PATH
    if path.exists():
        return path.read_bytes().strip()
    return None


def _store_key_in_file(key: bytes) -> None:
    config.ensure_dirs()
    path: Path = config.KEY_PATH
    path.write_bytes(key)
    # Best-effort lock-down of permissions (no-op semantics on Windows).
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get_or_create_key() -> bytes:
    """Return the Fernet master key, creating and persisting one if needed."""
    key = _load_key_from_keyring()
    if key:
        return key

    key = _load_key_from_file()
    if key:
        return key

    # No key anywhere yet -> generate one and persist it.
    key = Fernet.generate_key()
    if not _store_key_in_keyring(key):
        _store_key_in_file(key)
    else:
        log.info("Master key stored in OS keyring.")
    return key


class CredentialCipher:
    """Thin wrapper around Fernet for encrypting/decrypting credentials."""

    def __init__(self, key: bytes | None = None) -> None:
        self._fernet = Fernet(key or get_or_create_key())

    def encrypt(self, plaintext: str) -> bytes:
        if plaintext is None:
            plaintext = ""
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes | str) -> str:
        if isinstance(token, str):
            token = token.encode("utf-8")
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise CryptoError(
                "Could not decrypt credential. The encryption key may have "
                "changed or the data is corrupt."
            ) from exc
