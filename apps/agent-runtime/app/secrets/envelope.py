"""Encrypting a customer's own API key at rest.

AES-256-GCM with a master key from configuration. Three properties, in the order
they matter:

**The ciphertext is bound to the organisation it belongs to.** The additional
authenticated data is derived from ``(org_id, kind)`` and is never stored, so a
row copied from one organisation's ``core.org_secrets`` into another's does not
decrypt — it raises. Without that binding, write access to one table would be
enough to make tenant B's runs spend tenant A's OpenRouter credit, and every
individual value in the row would look correct.

**A nonce is never reused.** GCM's failure mode on nonce reuse is not degraded
security, it is catastrophic: two messages under the same key and nonce leak
their XOR and, worse, the authentication key itself. The nonce here is 96 random
bits per encryption, generated at encrypt time and stored beside the ciphertext,
which is the shape the primitive is designed for.

**Rotation is possible without an outage.** ``key_version`` says which master key
encrypted a row. New writes use the current version; old rows stay readable
until something re-encrypts them. Without it, rotating means re-encrypting every
row inside one migration that must not be interrupted.

What this is NOT: envelope encryption. A true envelope scheme wraps a per-secret
data key with a KMS key so the KMS never sees plaintext and can revoke
individually. This is direct encryption under a master key held in the
process's environment. That is the right amount of machinery for one key per
organisation on a single VPS, and the wrong amount the moment there is a KMS —
so it is named honestly here rather than described as something it is not.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 96 bits. The size AES-GCM is specified for; anything else forces the
# primitive through an extra derivation step and buys nothing.
NONCE_BYTES = 12
KEY_BYTES = 32  # AES-256


class SecretsNotConfigured(RuntimeError):
    """No master key. Raised at use, and the process refuses rather than
    falling back to storing the value in clear — which is the one behaviour
    that would make this module worse than not having it."""


class SecretUndecryptable(ValueError):
    """The ciphertext did not authenticate.

    Three causes and they are deliberately indistinguishable from outside: the
    wrong master key, a corrupted row, or a row that belongs to a different
    organisation. Telling a caller which would turn the AAD binding into an
    oracle for whose row it is.
    """


@dataclass(frozen=True, slots=True)
class Sealed:
    nonce: bytes
    ciphertext: bytes
    key_version: int


def _master_keys() -> dict[int, bytes]:
    """Master keys by version, newest first in configuration.

    ``ADVIT_SECRETS_MASTER_KEY`` is the current one. ``ADVIT_SECRETS_MASTER_KEY_2``
    and so on are earlier versions kept readable during a rotation. Base64, 32
    bytes decoded.
    """
    keys: dict[int, bytes] = {}

    current = os.environ.get("ADVIT_SECRETS_MASTER_KEY", "").strip()
    if current:
        keys[_version_of("ADVIT_SECRETS_MASTER_KEY")] = _decode(current, "ADVIT_SECRETS_MASTER_KEY")

    for name, value in os.environ.items():
        if not name.startswith("ADVIT_SECRETS_MASTER_KEY_") or not value.strip():
            continue
        keys[_version_of(name)] = _decode(value.strip(), name)

    return keys


def _version_of(name: str) -> int:
    suffix = name.removeprefix("ADVIT_SECRETS_MASTER_KEY").lstrip("_")
    return int(suffix) if suffix.isdigit() else 1


def _decode(value: str, name: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SecretsNotConfigured(f"{name} is not valid base64") from exc
    if len(raw) != KEY_BYTES:
        raise SecretsNotConfigured(
            f"{name} decodes to {len(raw)} bytes; AES-256 needs exactly {KEY_BYTES}"
        )
    return raw


def current_version() -> int:
    keys = _master_keys()
    if not keys:
        raise SecretsNotConfigured(
            "ADVIT_SECRETS_MASTER_KEY is not set. Generate one with "
            "`python -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\"` "
            "and put it in the environment. There is deliberately no fallback: a "
            "customer's API key is not stored in clear because a variable was missing."
        )
    return max(keys)


def aad(org_id: str, kind: str) -> bytes:
    """What the ciphertext is bound to.

    Not stored anywhere. It is re-derived at decryption from the row's own
    org_id and kind, so a row moved between organisations fails to authenticate
    rather than decrypting into the wrong tenant's runtime.
    """
    return f"advit:org_secret:v1:{org_id}:{kind}".encode()


def seal(plaintext: str, *, org_id: str, kind: str) -> Sealed:
    version = current_version()
    key = _master_keys()[version]
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), aad(org_id, kind))
    return Sealed(nonce=nonce, ciphertext=ciphertext, key_version=version)


def open_sealed(sealed: Sealed, *, org_id: str, kind: str) -> str:
    keys = _master_keys()
    if not keys:
        raise SecretsNotConfigured("ADVIT_SECRETS_MASTER_KEY is not set")

    key = keys.get(sealed.key_version)
    if key is None:
        raise SecretUndecryptable(
            f"no master key at version {sealed.key_version} is configured; a rotation "
            "removed an earlier key before its rows were re-encrypted"
        )

    try:
        return AESGCM(key).decrypt(
            sealed.nonce, sealed.ciphertext, aad(org_id, kind)
        ).decode()
    except InvalidTag as exc:
        raise SecretUndecryptable(
            "the stored secret did not authenticate: the wrong master key, a corrupted "
            "row, or a row belonging to a different organisation"
        ) from exc


def hint_for(plaintext: str) -> str:
    """The last few characters, for a settings page.

    Four, not eight. An OpenRouter key is high-entropy and four characters
    narrow nothing usefully, while being enough for an owner to tell two of
    their own keys apart — which is the only job this field has.
    """
    return plaintext[-4:] if len(plaintext) >= 8 else ""
