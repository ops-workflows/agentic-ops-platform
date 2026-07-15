"""Secret encryption/decryption for agent credentials.

Pulumi-style approach: secrets are encrypted inline in agent.yaml,
committed to Git safely, and decrypted at container spawn time by the
session manager.

Encrypted values use the format:  ENC[backend,base64_ciphertext]

Supported backends:
  - age     (default for on-premise, uses pyrage)
    - plain   (test-only passthrough backend for local fixtures)
  - aws-kms (future — wrap age key with KMS)
  - gcp-kms (future — wrap age key with KMS)

Usage:
  # Encrypt (gateway / builder — has public key)
  encrypted = encrypt_secret("my-api-token", public_key="age1...")

  # Decrypt (session manager — has identity/private key)
  plaintext = decrypt_secret("ENC[age,YWdl...]", identity="AGE-SECRET-KEY-1...")

  # Batch decrypt all secrets from agent.yaml
  env_vars = decrypt_agent_secrets(agent_config, identity="AGE-SECRET-KEY-1...")
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Pattern: ENC[backend,base64_ciphertext]
_ENC_PATTERN = re.compile(r"^ENC\[(\w[\w-]*),\s*(.+)\]$", re.DOTALL)
_AGE_IDENTITY_LINE_PATTERN = re.compile(r"^AGE-SECRET-KEY-[A-Z0-9]+$")


class CryptoError(Exception):
    """Raised when encryption or decryption fails."""


# ─── Core API ────────────────────────────────────────────────────────


def encrypt_secret(plaintext: str, *, public_key: str) -> str:
    """Encrypt a secret value using age.

    Args:
        plaintext: The secret value to encrypt.
        public_key: age recipient public key (age1...).

    Returns:
        Encrypted string in ENC[age,...] format.
    """
    try:
        import pyrage
    except ImportError as exc:
        raise CryptoError("pyrage is required for secret encryption. Install with: pip install pyrage") from exc

    recipient = pyrage.x25519.Recipient.from_str(public_key)
    ciphertext = pyrage.encrypt(plaintext.encode("utf-8"), [recipient])
    encoded = base64.b64encode(ciphertext).decode("ascii")
    return f"ENC[age,{encoded}]"


def derive_age_public_key(*, identity: str) -> str:
    """Derive an age recipient public key from a private identity reference."""
    try:
        import pyrage
    except ImportError as exc:
        raise CryptoError("pyrage is required for age key derivation. Install with: pip install pyrage") from exc

    try:
        ident = pyrage.x25519.Identity.from_str(_resolve_identity(identity))
        return str(ident.to_public())
    except Exception as exc:
        raise CryptoError("Failed to derive age public key") from exc


def decrypt_secret(encrypted: str, *, identity: str) -> str:
    """Decrypt an ENC[...] secret value.

    Args:
        encrypted: Encrypted string in ENC[backend,data] format.
        identity: age identity (secret key) string (AGE-SECRET-KEY-1...).

    Returns:
        Decrypted plaintext string.
    """
    match = _ENC_PATTERN.match(encrypted.strip())
    if not match:
        raise CryptoError(f"Invalid encrypted format: {encrypted[:40]}...")

    backend = match.group(1)
    data = match.group(2).strip()

    if backend == "age":
        return _decrypt_age(data, identity)
    if backend == "plain":
        return data
    else:
        raise CryptoError(f"Unsupported encryption backend: {backend}")


def is_encrypted(value: str) -> bool:
    """Check if a string is an ENC[...] encrypted value."""
    return bool(_ENC_PATTERN.match(value.strip())) if isinstance(value, str) else False


# ─── Agent YAML helpers ──────────────────────────────────────────────


def decrypt_agent_secrets(
    agent_config: dict[str, Any],
    *,
    identity: str,
) -> dict[str, str]:
    """Decrypt all secrets from an agent.yaml config.

    Args:
        agent_config: Parsed agent.yaml dict.
        identity: age identity (secret key) string.

    Returns:
        Dict of ENV_VAR_NAME → decrypted_plaintext for all secrets.

    Example agent.yaml secrets section:
        secrets:
                    API_TOKEN:
            encrypted: "ENC[age,YWdl...]"
                    ANOTHER_TOKEN:
            encrypted: "ENC[age,YWdl...]"

        Returns: {"API_TOKEN": "tok_...", "ANOTHER_TOKEN": "secret_..."}
    """
    secrets = agent_config.get("secrets", {})
    return decrypt_named_secrets(secrets, identity=identity)


def decrypt_named_secrets(
    secrets: dict[str, Any],
    *,
    identity: str,
) -> dict[str, str]:
    """Decrypt a generic secret mapping with agent.yaml-style entries.

    Example input:
        {
            "API_TOKEN": {"encrypted": "ENC[age,...]"},
        }
    """
    if not secrets:
        return {}

    decrypted: dict[str, str] = {}
    for env_var, spec in secrets.items():
        if not isinstance(spec, dict):
            logger.warning("Skipping malformed secret entry: %s", env_var)
            continue

        encrypted_value = spec.get("encrypted", "")
        if not encrypted_value:
            logger.warning("Secret %s has no encrypted value", env_var)
            continue

        if not is_encrypted(encrypted_value):
            logger.warning("Secret %s value is not in ENC[...] format", env_var)
            continue

        try:
            decrypted[env_var] = decrypt_secret(encrypted_value, identity=identity)
        except CryptoError:
            logger.warning("Failed to decrypt secret %s; leaving it unset", env_var)

    return decrypted


def list_agent_secrets(agent_config: dict[str, Any]) -> list[dict[str, str]]:
    """List secret metadata from agent.yaml (no decryption).

    Returns list of {name, description, has_value} for the UI.
    """
    secrets = agent_config.get("secrets", {})
    result = []
    for env_var, spec in secrets.items():
        if isinstance(spec, dict):
            result.append(
                {
                    "name": env_var,
                    "description": spec.get("description", ""),
                    "has_value": bool(spec.get("encrypted")),
                }
            )
    return result


# ─── Backend implementations ─────────────────────────────────────────


def _decrypt_age(base64_ciphertext: str, identity: str) -> str:
    """Decrypt using age (via pyrage)."""
    try:
        import pyrage
    except ImportError as exc:
        raise CryptoError("pyrage is required for secret decryption. Install with: pip install pyrage") from exc

    try:
        ciphertext = base64.b64decode(base64_ciphertext)
    except (ValueError, binascii.Error) as exc:
        raise CryptoError("Invalid base64 payload for age secret") from exc

    try:
        ident = pyrage.x25519.Identity.from_str(_resolve_identity(identity))
        plaintext = pyrage.decrypt(ciphertext, [ident])
        return plaintext.decode("utf-8")
    except Exception as exc:
        raise CryptoError("Failed to decrypt age secret") from exc


def _resolve_identity(identity: str) -> str:
    """Resolve an inline age identity or read it from a file reference."""
    candidate = identity.strip()
    if not candidate:
        raise CryptoError("AGE identity is empty")

    path: Path | None = None
    if candidate.startswith("file:"):
        path = Path(candidate[5:]).expanduser()
    else:
        possible_path = Path(candidate).expanduser()
        if possible_path.exists() and possible_path.is_file():
            path = possible_path

    if path is not None:
        try:
            return _normalize_age_identity_text(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CryptoError(f"Unable to read age identity file: {path}") from exc

    return _normalize_age_identity_text(candidate)


def _normalize_age_identity_text(identity_text: str) -> str:
    """Extract the actual age secret key from inline text or a key file."""
    candidate = identity_text.strip()
    if not candidate:
        raise CryptoError("AGE identity is empty")

    for line in candidate.splitlines():
        normalized = line.strip()
        if _AGE_IDENTITY_LINE_PATTERN.fullmatch(normalized):
            return normalized

    if "\n" in candidate or candidate.startswith("#"):
        raise CryptoError("AGE identity file does not contain a valid secret key")

    return candidate
