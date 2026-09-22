# tests/test_v2_llm_secrets.py
"""What "encrypted at rest" must mean for a stored provider credential (§21).

A remote connection's API key is stored, so it is stored encrypted with an
authenticated scheme (Fernet), under a version tag that lets a rotation re-encrypt
old rows. These tests pin the properties that make that real: a round trip returns
the exact secret, a tampered ciphertext fails loudly rather than yielding garbage, a
version with no registered key is refused, an older key still decrypts what it wrote,
and the ciphertext never contains the plaintext.
"""
import pytest
from pydantic import SecretStr

from backend.app.llm.secrets import (
    FernetSecretCipher,
    SecretCipher,
    SecretDecryptionError,
    generate_master_key,
)


def test_a_fernet_cipher_satisfies_the_protocol():
    assert isinstance(FernetSecretCipher(generate_master_key()), SecretCipher)


def test_a_secret_round_trips_exactly():
    cipher = FernetSecretCipher(generate_master_key())
    token = cipher.encrypt(SecretStr("sk-the-real-key-value"))
    back = cipher.decrypt(token, secret_version=cipher.version)
    assert back.get_secret_value() == "sk-the-real-key-value"


def test_the_ciphertext_does_not_contain_the_plaintext():
    cipher = FernetSecretCipher(generate_master_key())
    token = cipher.encrypt(SecretStr("sk-visible-secret"))
    assert "sk-visible-secret" not in token


def test_a_tampered_ciphertext_is_refused():
    cipher = FernetSecretCipher(generate_master_key())
    token = cipher.encrypt(SecretStr("sk-the-real-key-value"))
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(tampered, secret_version=cipher.version)


def test_a_version_with_no_registered_key_is_refused():
    cipher = FernetSecretCipher(generate_master_key())
    token = cipher.encrypt(SecretStr("sk-the-real-key-value"))
    with pytest.raises(SecretDecryptionError) as caught:
        cipher.decrypt(token, secret_version=999)
    assert caught.value.secret_version == 999


def test_the_wrong_master_key_cannot_decrypt():
    writer = FernetSecretCipher(generate_master_key())
    token = writer.encrypt(SecretStr("sk-the-real-key-value"))
    other = FernetSecretCipher(generate_master_key())
    with pytest.raises(SecretDecryptionError):
        other.decrypt(token, secret_version=other.version)


def test_an_older_registered_key_still_decrypts_what_it_wrote():
    """A rotation registers the new key active and the old one under its version."""
    old_key = generate_master_key()
    old_cipher = FernetSecretCipher(old_key, version=1)
    token_v1 = old_cipher.encrypt(SecretStr("sk-old-era-secret"))

    rotated = FernetSecretCipher(generate_master_key(), version=2,
                                 older={1: old_key})
    # New writes go at version 2...
    assert rotated.version == 2
    # ...but a value stored at version 1 still decrypts through the retained key.
    back = rotated.decrypt(token_v1, secret_version=1)
    assert back.get_secret_value() == "sk-old-era-secret"


def test_the_error_message_names_no_key_material():
    cipher = FernetSecretCipher(generate_master_key())
    token = cipher.encrypt(SecretStr("sk-super-secret-value"))
    tampered = token[:-4] + "ZZZZ"
    try:
        cipher.decrypt(tampered, secret_version=cipher.version)
    except SecretDecryptionError as exc:
        assert "sk-super-secret-value" not in str(exc)
    else:  # pragma: no cover - the decrypt must raise
        pytest.fail("a tampered ciphertext must not decrypt")
