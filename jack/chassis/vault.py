"""Pillar VI: Sovereign Vault secrets-at-rest support.

This module provides Jack's deterministic Chassis boundary for local encrypted
credential storage. Secrets are persisted only inside ``jack.vault`` as a Fernet
payload prefixed by a 16-byte salt. The master passphrase is never stored on the
``JackVault`` instance; it is used only to derive an in-memory Fernet key during
``unlock``.

File format::

    [SALT (16 bytes)][FERNET_PAYLOAD]

The Vault deliberately exposes only logical secret names as metadata. Secret
values are returned only through explicit deterministic lookup calls, allowing
future provider code to inject credentials at the outbound API-call boundary
without putting them into HotContext, Manager plans, or sandbox inputs.

V0.5 (BETA) hardening stores unlocked secret values as mutable ``bytearray`` buffers
so ``lock`` and ``remove_secret`` can physically overwrite the Vault-owned copy
before clearing internal state. ``get_secret`` still returns a normal Python
string because provider adapters require string credentials; callers must treat
that return value as short-lived and keep it outside reasoning context.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

DEFAULT_VAULT_PATH = "jack.vault"
SALT_SIZE = 16
KDF_ITERATIONS = 100_000
FERNET_KEY_BYTES = 32
VAULT_FILE_MODE = 0o600


class VaultError(RuntimeError):
    """Base error raised by Jack's deterministic Vault boundary."""


class VaultLockedError(VaultError):
    """Raised when a caller attempts to access secrets while the Vault is locked."""


class VaultAuthenticationError(VaultError):
    """Raised when the provided master passphrase cannot decrypt the Vault."""


class VaultFormatError(VaultError):
    """Raised when the encrypted Vault file is malformed or contains invalid data."""


class JackVault:
    """A deterministic encrypted credential store for the Jack Chassis.

    ``JackVault`` owns secrets-at-rest encryption only. It does not perform
    provider configuration, prompt construction, planning, or tool execution.
    The unlocked secret map is kept in mutable memory only until ``lock`` is
    called.
    """

    def __init__(self, vault_path: str | Path = DEFAULT_VAULT_PATH) -> None:
        """Create a Vault handle for ``vault_path`` without unlocking it."""
        self.vault_path = Path(vault_path)
        self._fernet: Fernet | None = None
        self._secrets: dict[str, bytearray] = {}
        self._unlocked = False
        self._salt: bytes | None = None

    @property
    def is_unlocked(self) -> bool:
        """Return whether the Vault currently has decrypted in-memory state."""
        return self._unlocked

    @property
    def exists(self) -> bool:
        """Return whether the encrypted Vault file exists on disk."""
        return self.vault_path.exists()

    def unlock(self, passphrase: str) -> bool:
        """Unlock an existing Vault or initialize a new empty Vault.

        The passphrase is consumed only for key derivation and is not retained.
        If ``jack.vault`` does not exist, this method creates a new encrypted
        empty Vault using a fresh 16-byte random salt.
        """
        self._validate_passphrase(passphrase)
        if not self.vault_path.exists():
            self._initialize_new_vault(passphrase)
            return True

        salt, encrypted_payload = self._read_vault_file()
        try:
            fernet = Fernet(self._derive_key(passphrase, salt))
            plaintext = fernet.decrypt(encrypted_payload)
            secrets = self._decode_secret_payload(plaintext)
        except InvalidToken as exc:
            self.lock()
            raise VaultAuthenticationError("Master passphrase could not decrypt the Vault.") from exc
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
            self.lock()
            raise VaultFormatError("Vault decrypted, but its payload is not a valid secret map.") from exc
        finally:
            # Drop the immutable plaintext reference as soon as it has been decoded.
            plaintext = b"" if "plaintext" in locals() else b""

        self._fernet = fernet
        self._secrets = secrets
        self._salt = salt
        self._unlocked = True
        return True

    def lock(self) -> None:
        """Physically zeroize unlocked secret buffers and lock the Vault.

        V2.1.1 stores Vault-owned decrypted secrets as ``bytearray`` values.
        This allows deterministic best-effort physical overwrite of the mutable
        buffer before the dictionary is cleared. Python cannot retroactively
        erase short-lived strings returned by ``get_secret`` or temporary bytes
        produced by Fernet/JSON internals, but the Vault no longer retains
        immutable plaintext secret strings as its authoritative in-memory state.
        """
        for secret_buffer in self._secrets.values():
            self._zeroize_buffer(secret_buffer)
        self._secrets.clear()
        self._fernet = None
        self._salt = None
        self._unlocked = False

    def get_secret(self, key_name: str) -> str:
        """Return a secret value by logical name.

        This is the only method that intentionally releases a secret value from
        the Vault boundary. The returned string is a short-lived provider input;
        callers must keep it out of reasoning context, plans, artifacts, and
        sandbox inputs.
        """
        self._require_unlocked()
        normalized_key = self._normalize_key_name(key_name)
        try:
            return bytes(self._secrets[normalized_key]).decode("utf-8")
        except KeyError as exc:
            raise KeyError(f"Secret {normalized_key!r} not found in Vault.") from exc

    def add_secret(self, key_name: str, value: str) -> None:
        """Add or replace a secret and persist the encrypted Vault immediately."""
        self._require_unlocked()
        normalized_key = self._normalize_key_name(key_name)
        if not isinstance(value, str) or not value:
            raise ValueError("Vault secret value must be a non-empty string.")
        if normalized_key in self._secrets:
            self._zeroize_buffer(self._secrets[normalized_key])
        self._secrets[normalized_key] = bytearray(value.encode("utf-8"))
        self._save_to_disk()

    def remove_secret(self, key_name: str) -> bool:
        """Remove a secret if present, persist the Vault, and return whether it existed."""
        self._require_unlocked()
        normalized_key = self._normalize_key_name(key_name)
        if normalized_key not in self._secrets:
            return False
        self._zeroize_buffer(self._secrets[normalized_key])
        del self._secrets[normalized_key]
        self._save_to_disk()
        return True

    def has_secret(self, key_name: str) -> bool:
        """Return whether a logical secret name is present without exposing its value."""
        self._require_unlocked()
        return self._normalize_key_name(key_name) in self._secrets

    def list_metadata(self) -> list[str]:
        """Return sorted logical secret names only, never secret values."""
        self._require_unlocked()
        return sorted(self._secrets.keys())

    def redacted_metadata(self) -> dict[str, Any]:
        """Return safe Vault metadata suitable for logs or status displays."""
        return {
            "vault_path": str(self.vault_path),
            "exists": self.vault_path.exists(),
            "is_unlocked": self._unlocked,
            "secret_names": self.list_metadata() if self._unlocked else [],
            "file_format": "[SALT (16 bytes)][FERNET_PAYLOAD]",
            "kdf": "PBKDF2HMAC-SHA256",
            "kdf_iterations": KDF_ITERATIONS,
            "memory_secret_type": "bytearray",
        }

    def _initialize_new_vault(self, passphrase: str) -> None:
        """Create a fresh encrypted empty Vault file with a new random salt."""
        salt = os.urandom(SALT_SIZE)
        self._fernet = Fernet(self._derive_key(passphrase, salt))
        self._salt = salt
        self._secrets = {}
        self._unlocked = True
        self._save_to_disk()

    def _save_to_disk(self) -> None:
        """Persist the current secret map using the exact Jack Vault file format."""
        self._require_unlocked()
        if self._fernet is None or self._salt is None:
            raise VaultLockedError("Cannot save Vault without unlocked encryption state.")

        payload_map = self._string_secret_map()
        try:
            payload = json.dumps(payload_map, sort_keys=True, separators=(",", ":")).encode("utf-8")
            encrypted_payload = self._fernet.encrypt(payload)
        finally:
            payload_map.clear()
            payload = b"" if "payload" in locals() else b""

        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("wb", delete=False, dir=str(self.vault_path.parent)) as handle:
            temporary_path = Path(handle.name)
            handle.write(self._salt)
            handle.write(encrypted_payload)
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(temporary_path, VAULT_FILE_MODE)
        temporary_path.replace(self.vault_path)
        os.chmod(self.vault_path, VAULT_FILE_MODE)

    def _read_vault_file(self) -> tuple[bytes, bytes]:
        """Read and validate the salt-prefixed encrypted Vault payload."""
        data = self.vault_path.read_bytes()
        if len(data) <= SALT_SIZE:
            raise VaultFormatError("Vault file is too short to contain a salt and Fernet payload.")
        salt = data[:SALT_SIZE]
        encrypted_payload = data[SALT_SIZE:]
        if len(salt) != SALT_SIZE:
            raise VaultFormatError("Vault file salt is malformed.")
        return salt, encrypted_payload

    def _string_secret_map(self) -> dict[str, str]:
        """Return a transient JSON-serializable copy of the mutable secret map."""
        return {key: bytes(value).decode("utf-8") for key, value in self._secrets.items()}

    @staticmethod
    def _derive_key(passphrase: str, salt: bytes) -> bytes:
        """Derive a Fernet-compatible key using PBKDF2HMAC with 100,000 iterations."""
        if len(salt) != SALT_SIZE:
            raise ValueError(f"Vault salt must be exactly {SALT_SIZE} bytes.")
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=FERNET_KEY_BYTES,
            salt=salt,
            iterations=KDF_ITERATIONS,
        )
        return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

    @staticmethod
    def _decode_secret_payload(plaintext: bytes) -> dict[str, bytearray]:
        """Decode a decrypted Vault payload and enforce a string-to-string map."""
        decoded = json.loads(plaintext.decode("utf-8"))
        if not isinstance(decoded, Mapping):
            raise ValueError("Vault payload must be a JSON object.")
        secrets: dict[str, bytearray] = {}
        for key, value in decoded.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("Vault payload must contain only string keys and string values.")
            normalized_key = JackVault._normalize_key_name(key)
            if not value:
                raise ValueError(f"Vault secret {normalized_key!r} is empty.")
            secrets[normalized_key] = bytearray(value.encode("utf-8"))
        return secrets

    @staticmethod
    def _zeroize_buffer(secret_buffer: bytearray) -> None:
        """Overwrite a mutable secret buffer in place with NUL bytes."""
        for index in range(len(secret_buffer)):
            secret_buffer[index] = 0

    @staticmethod
    def _normalize_key_name(key_name: str) -> str:
        """Normalize and validate a logical Vault secret name."""
        if not isinstance(key_name, str):
            raise TypeError("Vault secret name must be a string.")
        normalized = key_name.strip()
        if not normalized:
            raise ValueError("Vault secret name cannot be empty.")
        if any(character.isspace() for character in normalized):
            raise ValueError("Vault secret name cannot contain whitespace.")
        return normalized

    @staticmethod
    def _validate_passphrase(passphrase: str) -> None:
        """Reject invalid master passphrases without retaining them."""
        if not isinstance(passphrase, str) or not passphrase:
            raise ValueError("Master passphrase must be a non-empty string.")

    def _require_unlocked(self) -> None:
        """Raise if the Vault is not currently unlocked."""
        if not self._unlocked:
            raise VaultLockedError("Vault is locked. Provide the master passphrase first.")