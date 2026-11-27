"""Request signing: known Ed25519 vectors, RSA-PSS sign-then-verify, and loading keys from the environment."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pmfa.errors import ConfigError
from pmfa.world.kalshi.signing import (
    KEY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    KalshiSigner,
    load_private_key,
    load_signer,
    presign_text,
    sign_text,
)
from tests.world.kalshi.fakes import config

# RFC 8032, section 7.1, TEST 1 (empty message) and TEST 2 (message 0x72, "r").
RFC_TEST1_SECRET = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
RFC_TEST1_SIGNATURE = (
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065"
    "224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)
RFC_TEST2_SECRET = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
RFC_TEST2_SIGNATURE = (
    "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
    "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
)
# The RFC TEST 1 key over a Kalshi pre-sign text. Ed25519 is deterministic, so this is fixed.
KALSHI_VECTOR_SIGNATURE = (
    "NZNbn42nWTCkd3HNR5AZoq/t/QrVPbmH3kEjYPYNKXYaxQNVfzDcKmioxh2AAht4ymXHC+6UxJgODwo32QYwBg=="
)


def rfc_key(secret_hex: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret_hex))


def pem(key: Ed25519PrivateKey | rsa.RSAPrivateKey, fmt: serialization.PrivateFormat) -> str:
    return key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption()).decode(
        "ascii"
    )


def test_presign_text_is_timestamp_method_and_path_without_query() -> None:
    text = presign_text(1700000000000, "get", "/trade-api/v2/portfolio/orders?limit=5")
    assert text == "1700000000000GET/trade-api/v2/portfolio/orders"


@pytest.mark.parametrize(
    ("secret", "message", "expected"),
    [(RFC_TEST1_SECRET, "", RFC_TEST1_SIGNATURE), (RFC_TEST2_SECRET, "r", RFC_TEST2_SIGNATURE)],
    ids=["rfc8032-test1", "rfc8032-test2"],
)
def test_ed25519_matches_rfc8032_vectors(secret: str, message: str, expected: str) -> None:
    signature = base64.b64decode(sign_text(rfc_key(secret), message))
    assert signature.hex() == expected


def test_signer_headers_known_answer() -> None:
    signer = KalshiSigner("key-1", rfc_key(RFC_TEST1_SECRET), now_ms=lambda: 1700000000000)
    headers = signer.headers("GET", "/trade-api/v2/portfolio/balance?subaccount=1")
    assert headers == {
        KEY_HEADER: "key-1",
        TIMESTAMP_HEADER: "1700000000000",
        SIGNATURE_HEADER: KALSHI_VECTOR_SIGNATURE,
    }
    public = rfc_key(RFC_TEST1_SECRET).public_key()
    public.verify(
        base64.b64decode(KALSHI_VECTOR_SIGNATURE),
        b"1700000000000GET/trade-api/v2/portfolio/balance",
    )


def test_rsa_pss_sha256_with_digest_length_salt_verifies() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = KalshiSigner("key-rsa", key, now_ms=lambda: 1700000000123)
    assert signer.algorithm == "rsa-pss-sha256"
    headers = signer.headers("POST", "/trade-api/v2/portfolio/events/orders")
    key.public_key().verify(
        base64.b64decode(headers[SIGNATURE_HEADER]),
        b"1700000000123POST/trade-api/v2/portfolio/events/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )


def test_keys_load_from_pkcs8_pkcs1_and_escaped_newlines() -> None:
    ed = rfc_key(RFC_TEST2_SECRET)
    escaped = pem(ed, serialization.PrivateFormat.PKCS8).strip().replace("\n", "\\n")
    assert isinstance(load_private_key(escaped), Ed25519PrivateKey)
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    loaded = load_private_key(pem(rsa_key, serialization.PrivateFormat.TraditionalOpenSSL))
    assert isinstance(loaded, rsa.RSAPrivateKey)


def test_a_key_given_as_its_base64_body_alone_loads_as_the_same_key() -> None:
    """An environment-variable form can keep only the body of a pasted multi-line key."""
    ed = rfc_key(RFC_TEST2_SECRET)
    lines = pem(ed, serialization.PrivateFormat.PKCS8).strip().splitlines()
    body = "".join(lines[1:-1])
    loaded = load_private_key(body)
    assert isinstance(loaded, Ed25519PrivateKey)
    assert loaded.sign(b"m") == ed.sign(b"m")  # Ed25519 signatures are deterministic
    with pytest.raises(ConfigError) as info:
        load_private_key("not-a-key-SECRETVALUE")
    assert "SECRETVALUE" not in str(info.value)


def test_bad_pem_is_a_config_error_that_does_not_echo_the_input() -> None:
    with pytest.raises(ConfigError) as info:
        load_private_key(
            "-----BEGIN PRIVATE KEY-----\nnot-a-key-SECRETVALUE\n-----END PRIVATE KEY-----"
        )
    assert "SECRETVALUE" not in str(info.value)
    assert info.value.__cause__ is None


def test_load_signer_reads_the_named_variables() -> None:
    cfg = config()
    names = cfg.kalshi
    assert load_signer(cfg, environ={}) is None
    text = pem(rfc_key(RFC_TEST1_SECRET), serialization.PrivateFormat.PKCS8)
    signer = load_signer(
        cfg, environ={names.demo_key_id_env: "k", names.demo_private_key_env: text}
    )
    assert signer is not None
    assert signer.key_id == "k"
    assert "BEGIN" not in repr(signer)
    assert text not in repr(signer)


def test_load_signer_reads_a_key_file(tmp_path) -> None:
    cfg = config()
    path = tmp_path / "demo.key"
    path.write_text(pem(rfc_key(RFC_TEST1_SECRET), serialization.PrivateFormat.PKCS8))
    env = {cfg.kalshi.demo_key_id_env: "k", cfg.kalshi.demo_private_key_path_env: str(path)}
    signer = load_signer(cfg, environ=env)
    assert signer is not None
    assert signer.algorithm == "ed25519"


def test_load_signer_rejects_half_a_credential() -> None:
    cfg = config()
    with pytest.raises(ConfigError):
        load_signer(cfg, environ={cfg.kalshi.demo_key_id_env: "k"})
    with pytest.raises(ConfigError):
        load_signer(cfg, environ={cfg.kalshi.demo_private_key_env: "x"})
