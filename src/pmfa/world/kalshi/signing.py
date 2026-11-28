"""Kalshi request signing, for the demo executor's private endpoints.

Per Kalshi's API-key documentation (checked 2026-11-25):

- Three headers: ``KALSHI-ACCESS-KEY`` (the key id), ``KALSHI-ACCESS-TIMESTAMP``
  (Unix time in **milliseconds**) and ``KALSHI-ACCESS-SIGNATURE``.
- The signature is base64 over ``timestamp + METHOD + path``, where the path is
  the full request path **including** the ``/trade-api/v2`` prefix and
  **without** the query string.
- Ed25519 keys sign that text directly. RSA keys use RSA-PSS with SHA-256,
  MGF1-SHA-256 and a salt as long as the digest (32 bytes).
- The PEM header does not say which kind of key it is, so the type is read
  from the parsed key.

Key material is never logged, printed or put in a ``repr``. Only the demo
adapter loads a signer; the production adapter never calls :func:`load_signer`.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from pmfa.config import Config
from pmfa.errors import ConfigError

PrivateKey = Ed25519PrivateKey | RSAPrivateKey

KEY_HEADER = "KALSHI-ACCESS-KEY"
TIMESTAMP_HEADER = "KALSHI-ACCESS-TIMESTAMP"
SIGNATURE_HEADER = "KALSHI-ACCESS-SIGNATURE"


def presign_text(timestamp_ms: int, method: str, path: str) -> str:
    """The text Kalshi expects to be signed: timestamp, method, then the path without its query."""
    return f"{timestamp_ms}{method.upper()}{path.split('?', 1)[0]}"


def sign_text(private_key: PrivateKey, text: str) -> str:
    """Base64 signature of ``text`` with an Ed25519 or RSA (PSS, SHA-256) key."""
    message = text.encode("utf-8")
    if isinstance(private_key, Ed25519PrivateKey):
        signature = private_key.sign(message)
    elif isinstance(private_key, RSAPrivateKey):
        signature = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    else:  # pragma: no cover - guarded by load_private_key
        raise TypeError("Kalshi keys are Ed25519 or RSA")
    return base64.b64encode(signature).decode("ascii")


def load_private_key(pem: str | bytes) -> PrivateKey:
    """Parse an unencrypted PEM private key (PKCS#8 or PKCS#1).

    PEM text copied into a single-line environment variable often carries
    literal ``\\n`` sequences instead of newlines; those are accepted. So is
    the key's base64 body alone, without its ``BEGIN`` and ``END`` lines, which
    is what an environment-variable form keeps of a pasted multi-line value: it
    is read as DER, which holds a PKCS#8 or a PKCS#1 key alike.
    """
    text = pem.decode("utf-8") if isinstance(pem, bytes) else pem
    text = text.strip().replace("\\r\\n", "\n").replace("\\n", "\n")
    try:
        if "-----BEGIN" in text:
            key = serialization.load_pem_private_key(text.encode("utf-8"), password=None)
        else:
            key = serialization.load_der_private_key(base64.b64decode(text), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        # The exception text can quote the input, so it is not chained or repeated.
        raise ConfigError("the Kalshi private key is not a valid unencrypted PEM key") from None
    if not isinstance(key, Ed25519PrivateKey | RSAPrivateKey):
        raise ConfigError("the Kalshi private key must be Ed25519 or RSA")
    return key


class KalshiSigner:
    """Signs requests for one API key. Holds the key; never shows it."""

    def __init__(
        self,
        key_id: str,
        private_key: PrivateKey,
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if not key_id:
            raise ConfigError("the Kalshi key id is empty")
        self.key_id = key_id
        self._key = private_key
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    @property
    def algorithm(self) -> str:
        return "ed25519" if isinstance(self._key, Ed25519PrivateKey) else "rsa-pss-sha256"

    def sign(self, text: str) -> str:
        return sign_text(self._key, text)

    def headers(self, method: str, path: str) -> dict[str, str]:
        """The three ``KALSHI-ACCESS-*`` headers for a request to ``path`` (full path, query ignored)."""
        timestamp = self._now_ms()
        return {
            KEY_HEADER: self.key_id,
            TIMESTAMP_HEADER: str(timestamp),
            SIGNATURE_HEADER: self.sign(presign_text(timestamp, method, path)),
        }

    def __repr__(self) -> str:
        return f"KalshiSigner(algorithm={self.algorithm!r})"


def load_signer(config: Config, *, environ: Mapping[str, str] | None = None) -> KalshiSigner | None:
    """The demo signer from the environment, or ``None`` when no credentials are set.

    Reads the variables *named* in ``config.kalshi``: the key id, and the
    private key either as PEM text or as a path to a PEM file (the text wins
    if both are set). Raises :class:`ConfigError` when only part of a
    credential is present or the key does not parse; messages name variables,
    never values.
    """
    env = os.environ if environ is None else environ
    cfg = config.kalshi
    key_id = env.get(cfg.demo_key_id_env, "").strip()
    pem = env.get(cfg.demo_private_key_env, "").strip()
    pem_path = env.get(cfg.demo_private_key_path_env, "").strip()
    if not key_id and not pem and not pem_path:
        return None
    if not key_id:
        raise ConfigError(f"{cfg.demo_key_id_env} is not set but a Kalshi private key is")
    if not pem and not pem_path:
        raise ConfigError(
            f"{cfg.demo_key_id_env} is set but neither {cfg.demo_private_key_env} "
            f"nor {cfg.demo_private_key_path_env} is"
        )
    if not pem:
        file = Path(pem_path).expanduser()
        if not file.is_file():
            raise ConfigError(f"{cfg.demo_private_key_path_env} does not name a readable file")
        pem = file.read_text(encoding="utf-8")
    return KalshiSigner(key_id, load_private_key(pem))


__all__ = [
    "KEY_HEADER",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "KalshiSigner",
    "PrivateKey",
    "load_private_key",
    "load_signer",
    "presign_text",
    "sign_text",
]
