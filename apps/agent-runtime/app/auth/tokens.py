"""Local verification of the Supabase JWT. GoTrue is never called per request.

A per-request call to ``/auth/v1/user`` would tie this API's availability to the
auth service, add a network hop to the hot path, and be rate-limited.

**The algorithm list is derived from OUR configuration, never from the token
header.** That is the mitigation for the classic algorithm-confusion forgery:
take the published RSA or EC *public* key, sign an HS256 token using it as the
HMAC secret, and a verifier that reads ``alg`` from the header to decide which
key type to use will accept it. This one cannot represent that mistake, because
the key and the algorithm list are chosen together from settings before the
token is looked at.

**Revocation, stated honestly.** Local verification cannot see a sign-out; a
token stays good until ``exp``. Two partial answers are in place — a shorter
``auth.jwt_expiry``, and a ``core.platform_users.is_active`` read folded into the
workspace resolver so a *deactivated* account is dead on its next request. Sign-out
itself is not covered. That is a JWT, not a session, and the design should say so
rather than imply otherwise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

from app.config import get_settings


class TokenRejected(Exception):
    """The reason goes to the log; the caller gets a flat message.

    Which claim failed is exactly the feedback a forger needs to tune the next
    attempt — "expired" versus "bad signature" versus "wrong audience" narrows
    the search with every request.
    """


class KeyServerUnavailable(Exception):
    """Our key source is down. 503, never 401.

    Telling a user their credentials are bad when OUR key server is unreachable
    sends them to reset a password that was never wrong, and buries the outage
    under a support queue.
    """


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    subject: uuid.UUID
    claims: dict


_jwks_client: PyJWKClient | None = None


def _jwks(url: str) -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        # `lifespan` IS the rotation window: a new signing key is picked up
        # within five minutes with no restart, and the last good key set stays
        # in memory so a brief GoTrue outage does not take this API down.
        _jwks_client = PyJWKClient(url, cache_keys=True, lifespan=300, timeout=3)
    return _jwks_client


def reset_jwks_cache() -> None:
    """For tests and for a deliberate re-read after a configuration change."""
    global _jwks_client
    _jwks_client = None


def verify(token: str) -> VerifiedToken:
    settings = get_settings()
    issuer = settings.supabase_jwt_issuer or f"{settings.supabase_url.rstrip('/')}/auth/v1"

    if settings.supabase_jwks_url:
        # Target state: supabase/config.toml `auth.signing_keys_path` enabled,
        # GoTrue publishing ES256/RS256 at /auth/v1/.well-known/jwks.json.
        algorithms = ["ES256", "RS256", "EdDSA"]
        try:
            keys = [_jwks(settings.supabase_jwks_url).get_signing_key_from_jwt(token).key]
        except jwt.exceptions.PyJWKClientConnectionError as exc:
            raise KeyServerUnavailable(str(exc)) from exc
        except jwt.exceptions.PyJWKClientError as exc:
            raise TokenRejected(f"unknown kid: {exc}") from exc
    elif settings.supabase_jwt_secret:
        # Today's shape: config.toml has signing_keys_path commented out, so
        # this deployment is HS256. `supabase_jwt_secret_previous` gives an HMAC
        # rotation a window in which both keys are tried.
        algorithms = ["HS256"]
        keys = [
            k
            for k in (settings.supabase_jwt_secret, settings.supabase_jwt_secret_previous)
            if k
        ]
    else:
        # A service that cannot verify a principal has no way to refuse one,
        # which is the bug this whole module exists to close. Refuse to start
        # rather than refusing to authenticate.
        raise RuntimeError(
            "neither SUPABASE_JWKS_URL nor SUPABASE_JWT_SECRET is configured, so no "
            "token can be verified and every request would have to be refused"
        )

    last: Exception | None = None
    claims: dict | None = None
    for key in keys:
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=algorithms,      # literal from config, never header-derived
                audience="authenticated",
                issuer=issuer,
                # Thirty seconds, not five minutes. Clock skew on a single VPS
                # is sub-second, and a generous leeway extends the life of every
                # revoked or expired token by exactly that amount.
                leeway=30,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
            break
        except jwt.ExpiredSignatureError as exc:
            # Distinguished from the loop: trying the previous HMAC key against
            # an expired token cannot make it un-expired, and would only produce
            # a more confusing message.
            raise TokenRejected("expired") from exc
        except jwt.InvalidTokenError as exc:
            last = exc

    if claims is None:
        raise TokenRejected(f"signature or claim rejected: {last}")

    # The Supabase service-role key is a VALID JWT: role `service_role`, no
    # `sub`, and a ten-year expiry. If one were pasted into a client and we
    # forwarded its claims, `auth.uid()` would be null and
    # `core.assert_org_visible` would take its BACKEND branch — reopening from
    # the HTTP side the exact hole 20260910000003 closed in SQL.
    if claims.get("role") != "authenticated":
        raise TokenRejected(f"role {claims.get('role')!r} is not a tenant principal")

    # `enable_anonymous_sign_ins = false` in config.toml today, but a config flip
    # must not silently start minting principals.
    if claims.get("is_anonymous"):
        raise TokenRejected("anonymous session")

    try:
        subject = uuid.UUID(claims["sub"])
    except (KeyError, ValueError, TypeError) as exc:
        # It is about to go into `request.jwt.claims` and then into
        # `auth.uid()::uuid`, where a malformed value is a 22P02 inside every
        # policy rather than a refusal here.
        raise TokenRejected("sub is not a uuid") from exc

    return VerifiedToken(subject=subject, claims=claims)
