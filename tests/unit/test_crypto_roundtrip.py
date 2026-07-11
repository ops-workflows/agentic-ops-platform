"""Layer 0 — age encrypt/decrypt round-trip."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

try:
    import pyrage  # noqa: F401
except Exception:  # pragma: no cover
    pyrage = None  # type: ignore[assignment]

from shared.lib.crypto import (  # noqa: E402
    CryptoError,
    decrypt_secret,
    derive_age_public_key,
    encrypt_secret,
    is_encrypted,
)

pyrage_required = pytest.mark.skipif(pyrage is None, reason="pyrage not installed")


def test_is_encrypted_detects_marker() -> None:
    assert is_encrypted("ENC[age,abcdef]")
    assert not is_encrypted("plain-text")
    assert not is_encrypted("")


@pyrage_required
def test_age_encrypt_decrypt_roundtrip() -> None:
    ident = pyrage.x25519.Identity.generate()
    public_key = ident.to_public().__str__()
    private_key = ident.__str__()

    ciphertext = encrypt_secret("super-secret", public_key=public_key)
    assert ciphertext.startswith("ENC[age,")

    plaintext = decrypt_secret(ciphertext, identity=private_key)
    assert plaintext == "super-secret"


@pyrage_required
def test_decrypt_with_wrong_identity_fails() -> None:
    ident_a = pyrage.x25519.Identity.generate()
    ident_b = pyrage.x25519.Identity.generate()
    ciphertext = encrypt_secret("secret", public_key=ident_a.to_public().__str__())
    with pytest.raises(CryptoError):
        decrypt_secret(ciphertext, identity=ident_b.__str__())


@pyrage_required
def test_derive_age_public_key_matches_identity() -> None:
    ident = pyrage.x25519.Identity.generate()
    assert derive_age_public_key(identity=ident.__str__()) == ident.to_public().__str__()
