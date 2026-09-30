"""Shared fixtures for the runtime suite.

The one thing worth explaining here is which credential these tests run on.

They open the pools against `advit_tenant` and `advit_service` — the credentials
the API actually holds — rather than against the superuser DSN. That is the
point: on the superuser connection RLS is not weakened, it is absent, so an
integration test that passed there proved only that the SQL was syntactically
valid. Run over the real service role, the same tests also prove the grant
matrix in 20260911000007 is sufficient for every query the runtime makes.

`DSN` below stays the superuser connection and is used only for fixture setup —
disabling a trigger, inserting a row a tenant is not permitted to insert,
reading `pg_catalog`. Test *setup* may be privileged; the code under test may
not.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager

import jwt
import pytest

from app.config import get_settings
from app.db.pools import bind_tenant, close_pools, open_pools, tenant_tx

TENANT_DSN = os.environ.get(
    "TENANT_DATABASE_URL",
    "postgresql://advit_tenant:advit_tenant_local@127.0.0.1:54322/postgres",
)
SERVICE_DSN = os.environ.get(
    "SERVICE_DATABASE_URL",
    "postgresql://advit_service:advit_service_local@127.0.0.1:54322/postgres",
)


@pytest.fixture(scope="session", autouse=True)
def _hs256_tokens():
    """Pin the suite to the HS256 path, whatever `.env` says.

    The running local stack issues ES256 tokens from a JWKS endpoint, and that
    is what `.env` points the runtime at - correctly. But these tests have to
    produce tokens an auth server will never issue (expired, wrong audience,
    service-role, `alg: none`), which means holding the signing key, which means
    HMAC. Inheriting `.env` here would make the whole suite depend on a live
    GoTrue and on a private key we do not have.

    `test_a_token_signed_hs256_against_the_published_public_key_is_refused` and
    `test_an_es256_token_from_the_published_jwks_is_accepted` cover the
    asymmetric path explicitly, with a key pair generated in-process.
    """
    settings = get_settings()
    previous = (settings.supabase_jwks_url, settings.supabase_jwt_secret)
    settings.supabase_jwks_url = ""
    settings.supabase_jwt_secret = (
        settings.supabase_jwt_secret
        or "super-secret-jwt-token-with-at-least-32-characters-long"
    )
    try:
        yield
    finally:
        settings.supabase_jwks_url, settings.supabase_jwt_secret = previous


@pytest.fixture(scope="session", autouse=True)
def _pools():
    """Session-scoped and autouse.

    Autouse because almost everything here eventually reaches a database
    adapter, and a test that forgot the fixture would fail with `open_pools()
    has not run` rather than with whatever it was actually asserting.

    Session-scoped because a pool per test would open and tear down TCP
    connections faster than the tests do useful work.
    """
    open_pools(tenant_dsn=TENANT_DSN, service_dsn=SERVICE_DSN)
    try:
        yield
    finally:
        close_pools()


# ---------------------------------------------------------------------------
# Principals and tokens
#
# The fixture identities from supabase/seeds/01_core.sql. They are here rather
# than duplicated per test file because auth turned "which workspace" into
# "which workspace AND whose session", and those two facts must not be allowed
# to drift apart.
# ---------------------------------------------------------------------------

SUPERADMIN = "00000000-0000-4000-8000-000000000001"
OWNER = "00000000-0000-4000-8000-000000000002"        # Broadmate Global, owner
MEMBER = "00000000-0000-4000-8000-000000000003"       # Broadmate, workspace member
OUTSIDER = "00000000-0000-4000-8000-000000000004"     # Rival Wellness, owner
ANALYST = "00000000-0000-4000-8000-000000000005"      # Broadmate org member, NO workspace grant

BROADMATE_WORKSPACE = "00000000-0000-4000-8000-000000000050"
RIVAL_WORKSPACE = "00000000-0000-4000-8000-000000000051"

# Who may act on which workspace. A test that runs a workspace under the wrong
# principal now gets a 404 rather than a silent cross-tenant read, which is the
# behaviour being asserted everywhere else - so the mapping is written down once.
OWNER_OF = {
    BROADMATE_WORKSPACE: OWNER,
    RIVAL_WORKSPACE: OUTSIDER,
}


def claims_for(user_id: str) -> dict:
    """Exactly the shape app/auth/scope.py forwards onto the connection: role
    and subject, and nothing else."""
    return {"role": "authenticated", "sub": user_id}


@contextmanager
def acting_as(user_id: str):
    """Bind a tenant transaction factory for code that reads through
    ``current_tenant_tx`` — the orchestrator, mainly.

    A factory rather than an open transaction, because a chat turn makes model
    calls between reads and holding one transaction across them would pin an
    idle-in-transaction snapshot.
    """
    with bind_tenant(lambda: tenant_tx(claims_for(user_id))):
        yield


def token_for(user_id: str, **overrides) -> str:
    """Mint a token the runtime will accept, signed with the local HS256 secret.

    Minting rather than calling GoTrue: these tests need to produce tokens that
    are deliberately WRONG - expired, wrong audience, wrong key, service-role -
    and an auth server will not issue those. The verifier is the thing under
    test, so the token has to be an input.
    """
    settings = get_settings()
    now = int(time.time())
    payload = {
        "sub": user_id,
        "aud": "authenticated",
        "role": "authenticated",
        "iss": settings.supabase_jwt_issuer or f"{settings.supabase_url.rstrip('/')}/auth/v1",
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    key = overrides.pop("_key", None) or settings.supabase_jwt_secret
    return jwt.encode(payload, key, algorithm=overrides.pop("_alg", "HS256"))


def auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(user_id)}"}
