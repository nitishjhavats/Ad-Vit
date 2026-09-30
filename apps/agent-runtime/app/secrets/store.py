"""Reading and writing an organisation's own credentials.

On the SERVICE connection, always. `core.org_secrets` has no tenant-facing
SELECT on its ciphertext columns at all — `core.org_secret_status` is what a
settings page reads, and it carries the hint and never the key.

The cache below is the part worth explaining. A model call that first made a
database round trip to fetch a key would put the credential lookup on the hot
path of every single completion. The key changes when an owner rotates it, which
is rare, so it is cached for a short TTL and the rotation path clears it. The TTL
is short rather than absent because a revoked key must stop working within
minutes without anyone restarting a process.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from app.db.pools import service_conn
from app.secrets.envelope import (
    Sealed,
    SecretsNotConfigured,
    SecretUndecryptable,
    hint_for,
    open_sealed,
    seal,
)

OPENROUTER = "openrouter_api_key"

# Short. A key an owner has just revoked in OpenRouter's dashboard should stop
# being used by this system within a couple of minutes, and the only mechanism
# for that is the cache expiring.
CACHE_TTL_S = 120


class NoKeyForOrganisation(LookupError):
    """This organisation has supplied no key.

    Not an error to swallow. The product is sold on the customer bringing their
    own key, so the honest response to its absence is to tell them - not to fall
    back to a platform key, which would silently bill this business for their
    usage and hide the configuration gap until the invoice.
    """


@dataclass(frozen=True, slots=True)
class _Entry:
    value: str
    at: float


_cache: dict[tuple[str, str], _Entry] = {}
_lock = threading.Lock()


def store(org_id: str, plaintext: str, *, kind: str = OPENROUTER) -> dict[str, Any]:
    """Encrypt and save. Returns what is safe to show back."""
    plaintext = plaintext.strip()
    if not plaintext:
        raise ValueError("an empty key is not a key")

    sealed = seal(plaintext, org_id=org_id, kind=kind)
    hint = hint_for(plaintext)

    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into core.org_secrets
              (org_id, kind, nonce, ciphertext, key_version, hint)
            values (%s::uuid, %s, %s, %s, %s, %s)
            on conflict (org_id, kind) do update set
              nonce       = excluded.nonce,
              ciphertext  = excluded.ciphertext,
              key_version = excluded.key_version,
              hint        = excluded.hint,
              -- A new key has not been verified yet, and the previous key's
              -- verification says nothing about this one. Clearing both is what
              -- stops a settings page reporting "working" about a key that has
              -- never been used.
              last_verified_at = null,
              last_error       = null
            returning hint, key_version, created_at
            """,
            (org_id, kind, sealed.nonce, sealed.ciphertext, sealed.key_version, hint),
        )
        row = cur.fetchone()
        conn.commit()

    forget(org_id, kind)
    return {"kind": kind, "hint": row["hint"], "key_version": row["key_version"]}


def forget(org_id: str, kind: str = OPENROUTER) -> None:
    with _lock:
        _cache.pop((org_id, kind), None)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def resolve(org_id: str, *, kind: str = OPENROUTER) -> str:
    """The plaintext key for this organisation, or an explanation.

    Raises rather than returning None. A caller that got None would have to
    decide what to do, and the only two options are "fall back to a platform
    key" - which bills the wrong party - and "fail" - which is this.
    """
    now = time.monotonic()
    with _lock:
        cached = _cache.get((org_id, kind))
        if cached and now - cached.at < CACHE_TTL_S:
            return cached.value

    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select nonce, ciphertext, key_version
              from core.org_secrets
             where org_id = %s::uuid and kind = %s
            """,
            (org_id, kind),
        )
        row = cur.fetchone()

    if row is None:
        raise NoKeyForOrganisation(
            f"organisation {org_id} has supplied no {kind}. Every model call is billed "
            "to the customer's own OpenRouter account, so there is nothing to fall back to."
        )

    plaintext = open_sealed(
        Sealed(
            nonce=bytes(row["nonce"]),
            ciphertext=bytes(row["ciphertext"]),
            key_version=row["key_version"],
        ),
        org_id=org_id,
        kind=kind,
    )

    with _lock:
        _cache[(org_id, kind)] = _Entry(value=plaintext, at=now)
    return plaintext


def record_outcome(org_id: str, *, kind: str = OPENROUTER, error: str | None = None) -> None:
    """Whether the key worked the last time it was used.

    "Never worked" and "stopped working" are different problems for the owner -
    the first is a typo at setup, the second is a revocation or a spend limit -
    and a settings page that says only "invalid" cannot tell them apart.
    """
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            update core.org_secrets
               set last_verified_at = case when %s::text is null then now()
                                           else last_verified_at end,
                   last_error       = %s
             where org_id = %s::uuid and kind = %s
            """,
            (error, error, org_id, kind),
        )
        conn.commit()


def status(org_id: str) -> list[dict[str, Any]]:
    """What a settings page may see. Never the key."""
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select kind, hint, key_version, created_at, rotated_at,
                   last_verified_at, last_error,
                   (last_verified_at is not null) as is_working
              from core.org_secrets where org_id = %s::uuid order by kind
            """,
            (org_id,),
        )
        return [dict(r) for r in cur.fetchall()]


__all__ = [
    "OPENROUTER",
    "NoKeyForOrganisation",
    "SecretsNotConfigured",
    "SecretUndecryptable",
    "clear_cache",
    "forget",
    "record_outcome",
    "resolve",
    "status",
    "store",
]
