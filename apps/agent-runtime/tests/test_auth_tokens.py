"""The vulnerability, written as tests.

Until this commit every route on this API was reachable with no credential at
all, and every one that took a ``workspace_id`` acted on whatever tenant the
caller named. These tests are the two halves of that: *who is calling*, and
*what they are allowed to name*.

The failure modes they cover are ordered by how quietly they fail:

  * no token at all — loud, and the one everybody remembers to test
  * a valid token for the wrong tenant — silent, and the actual product bug
  * an organisation member who is not a workspace member — silent, and invisible
    unless you know `workspaces_select` and `is_workspace_member` disagree
  * a service-role key pasted into a client — silent, and it re-opens from HTTP
    the hole 20260910000003 closed in SQL
"""

from __future__ import annotations

import time
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient

from app.auth.tokens import KeyServerUnavailable, TokenRejected, reset_jwks_cache, verify
from app.config import get_settings
from app.main import app
from conftest import (
    ANALYST,
    BROADMATE_WORKSPACE,
    MEMBER,
    OUTSIDER,
    OWNER,
    RIVAL_WORKSPACE,
    auth,
    token_for,
)


@pytest.fixture
def anon() -> TestClient:
    return TestClient(app)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# No credential
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", f"/api/workspaces/{BROADMATE_WORKSPACE}/dashboard"),
        ("get", f"/api/workspaces/{BROADMATE_WORKSPACE}/approvals"),
        ("get", f"/api/workspaces/{BROADMATE_WORKSPACE}/connections/health"),
        ("post", f"/api/workspaces/{BROADMATE_WORKSPACE}/chat"),
        ("post", f"/api/workspaces/{BROADMATE_WORKSPACE}/daily-truth"),
        ("post", "/api/compliance/check"),
        ("post", "/api/economics"),
        ("get", "/api/health/detail"),
        ("get", "/api/audit/account/1000000000000003"),
    ],
)
def test_a_request_without_a_bearer_token_is_refused(anon, method, path):
    """The live vulnerability, as a test. Every one of these returned data to an
    anonymous caller on the public internet."""
    kwargs = {"json": {}} if method == "post" else {}
    resp = getattr(anon, method)(path, **kwargs)
    assert resp.status_code == 401, f"{method.upper()} {path} answered an anonymous caller"


@pytest.mark.parametrize("header", ["", "Bearer", "Basic abc", "Bearer ", "token abc"])
def test_a_malformed_authorization_header_is_refused(anon, header):
    resp = anon.get(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/dashboard",
        headers={"Authorization": header} if header else {},
    )
    assert resp.status_code == 401


def test_the_public_routes_are_still_public(anon):
    """The counterpart. A container health check cannot hold a session, and the
    brand lock-up is published on purpose — so if these started requiring a
    token the deployment would look unhealthy for a reason nobody could see."""
    assert anon.get("/health").status_code == 200
    assert anon.get("/api/brand").status_code == 200


def test_the_public_health_route_still_publishes_nothing_operational(anon):
    """It used to return `write_allowlist` — the ad accounts this process may
    spend money on — and the raw database error, which carries the DSN and
    internal hostnames. Those moved behind authentication."""
    body = anon.get("/health").json()
    assert "write_allowlist" not in body
    assert "meta_driver" not in body
    assert "error" not in str(body)


# ---------------------------------------------------------------------------
# A valid token, the wrong tenant
# ---------------------------------------------------------------------------


def test_a_valid_token_for_one_tenant_cannot_read_another(anon):
    """The actual product bug, and the one that fails silently.

    404 rather than 403, deliberately: a 403 confirms the workspace exists,
    which rebuilds exactly the enumeration oracle 20260910000001 was written to
    close.
    """
    resp = anon.get(
        f"/api/workspaces/{RIVAL_WORKSPACE}/dashboard", headers=auth(OWNER)
    )
    assert resp.status_code == 404

    mine = anon.get(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/dashboard", headers=auth(OWNER)
    )
    assert mine.status_code == 200, "the owner can no longer read their own workspace"


def test_the_refusal_looks_the_same_as_a_workspace_that_does_not_exist(anon):
    """If the two differed, the difference would be an existence oracle: point
    it at a UUID space and it tells you which tenants are real."""
    real_other = anon.get(
        f"/api/workspaces/{RIVAL_WORKSPACE}/dashboard", headers=auth(OWNER)
    )
    imaginary = anon.get(
        f"/api/workspaces/{uuid.uuid4()}/dashboard", headers=auth(OWNER)
    )
    assert real_other.status_code == imaginary.status_code == 404
    assert real_other.json() == imaginary.json()


def test_an_org_member_who_is_not_a_workspace_member_cannot_drive_the_pipeline(anon):
    """The intra-organisation boundary, which is the one nobody sees coming.

    ANALYST is a plain organisation `member` with no `workspace_members` row.
    `workspaces_select` uses the WIDER core.is_org_member, so this account CAN
    read the workspace row — and if the resolver treated "the SELECT returned a
    row" as proof of ownership, this request would succeed and everything
    downstream would then run on the SERVICE connection with the workspace id
    "already proved".
    """
    resp = anon.post(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/chat",
        headers=auth(ANALYST),
        json={"message": "Budget badha do"},
    )
    assert resp.status_code == 404


def test_a_workspace_member_can_still_use_their_workspace(anon):
    """The counterpart, so the check above cannot be satisfied by refusing
    everyone."""
    resp = anon.get(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/approvals", headers=auth(MEMBER)
    )
    assert resp.status_code == 200


def test_the_rollback_route_resolves_its_workspace_from_the_action_row(anon):
    """The highest-consequence route in the product: it takes a bare UUID, and
    what it does with the workspace it finds is mutate Meta.

    Before the resolver, any signed-in user who learned an action id could roll
    back another tenant's spend.
    """
    resp = anon.post(f"/api/actions/{uuid.uuid4()}/rollback", headers=auth(OUTSIDER))
    assert resp.status_code == 404


def test_the_ad_account_audit_resolves_ownership_from_meta_connections(anon):
    """`/api/audit/account/{id}` takes an ad account with no workspace at all
    and calls the Meta driver directly. A stranger who knows an account id used
    to get a full structural audit of somebody else's advertising."""
    resp = anon.get("/api/audit/account/1000000000000003", headers=auth(OUTSIDER))
    assert resp.status_code == 404

    owned = anon.get("/api/audit/account/1000000000000003", headers=auth(OWNER))
    assert owned.status_code == 200


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


def test_an_expired_token_is_refused():
    now = int(time.time())
    with pytest.raises(TokenRejected, match="expired"):
        verify(token_for(OWNER, iat=now - 7200, exp=now - 3600))


def test_a_token_one_second_past_the_leeway_is_refused():
    """Thirty seconds of leeway, not five minutes: clock skew on a single VPS is
    sub-second, and every extra second of leeway is an extra second of life for
    a token that should be dead."""
    now = int(time.time())
    with pytest.raises(TokenRejected):
        verify(token_for(OWNER, exp=now - 31))


def test_a_token_signed_with_the_wrong_key_is_refused():
    with pytest.raises(TokenRejected):
        verify(token_for(OWNER, _key="not-the-signing-key-but-long-enough-to-pass"))


def test_a_token_with_the_wrong_audience_is_refused():
    with pytest.raises(TokenRejected):
        verify(token_for(OWNER, aud="anon"))


def test_a_token_from_another_issuer_is_refused():
    """A token minted by a different Supabase project is a perfectly valid JWT.
    It is just not one this deployment ever issued."""
    with pytest.raises(TokenRejected):
        verify(token_for(OWNER, iss="https://someone-elses-project.supabase.co/auth/v1"))


@pytest.mark.parametrize("missing", ["exp", "iat", "sub", "aud", "iss"])
def test_a_token_missing_a_required_claim_is_refused(missing):
    """`options={"require": [...]}` rather than relying on each check to notice
    an absent claim — several of PyJWT's validators are no-ops when the claim is
    simply not there."""
    settings = get_settings()
    now = int(time.time())
    claims = {
        "sub": OWNER,
        "aud": "authenticated",
        "role": "authenticated",
        "iss": f"{settings.supabase_url.rstrip('/')}/auth/v1",
        "iat": now,
        "exp": now + 3600,
    }
    claims.pop(missing)
    token = jwt.encode(claims, settings.supabase_jwt_secret, algorithm="HS256")
    with pytest.raises(TokenRejected):
        verify(token)


def test_a_supabase_service_role_token_presented_over_http_is_refused():
    """The Supabase service-role key is a VALID JWT: role `service_role`, no
    `sub`, ten-year expiry.

    If one were pasted into a client and we forwarded its claims, `auth.uid()`
    would be null and `core.assert_org_visible` would take its BACKEND branch —
    re-opening from the HTTP side the exact hole 20260910000003 closed in SQL.
    """
    settings = get_settings()
    now = int(time.time())
    token = jwt.encode(
        {
            "aud": "authenticated",
            "role": "service_role",
            "iss": f"{settings.supabase_url.rstrip('/')}/auth/v1",
            "iat": now,
            "exp": now + 3600,
            "sub": OWNER,
        },
        settings.supabase_jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(TokenRejected, match="not a tenant principal"):
        verify(token)


def test_an_anonymous_session_is_refused():
    """`enable_anonymous_sign_ins = false` today, but a config flip must not
    silently start minting principals that RLS will then evaluate."""
    with pytest.raises(TokenRejected, match="anonymous"):
        verify(token_for(OWNER, is_anonymous=True))


def test_a_subject_that_is_not_a_uuid_is_refused():
    """It goes into `request.jwt.claims` and then into `auth.uid()::uuid`, where
    a malformed value is a 22P02 inside every policy rather than a refusal
    here."""
    with pytest.raises(TokenRejected, match="uuid"):
        verify(token_for(OWNER, sub="admin"))


def test_an_alg_none_token_is_refused():
    settings = get_settings()
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": OWNER,
            "aud": "authenticated",
            "role": "authenticated",
            "iss": f"{settings.supabase_url.rstrip('/')}/auth/v1",
            "iat": now,
            "exp": now + 3600,
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(TokenRejected):
        verify(token)


# ---------------------------------------------------------------------------
# Algorithm confusion, and the key server being down
# ---------------------------------------------------------------------------


def _hs256_by_hand(claims: dict, *, secret: str) -> str:
    """An HS256 token whose secret is a PEM public key."""
    import base64
    import hashlib
    import hmac
    import json as _json

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(_json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(_json.dumps(claims).encode())
    signing_input = header + b"." + body
    signature = b64(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return (signing_input + b"." + signature).decode()


class _FakeJWKS:
    def __init__(self, key):
        self._key = key

    def get_signing_key_from_jwt(self, token):
        class _Signing:
            pass

        signing = _Signing()
        signing.key = self._key
        return signing


def test_a_token_signed_hs256_against_the_published_public_key_is_refused(monkeypatch):
    """The classic algorithm-confusion forgery.

    Take the published RSA/EC **public** key, sign an HS256 token using it as
    the HMAC secret, and any verifier that reads `alg` from the token header to
    decide which key type to use will accept it — because the "secret" is
    public.

    This one cannot represent that mistake: when a JWKS is configured the
    algorithm list is the literal ``["ES256", "RS256", "EdDSA"]`` from our own
    settings, so an HS256 token is refused before the key is ever consulted.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    settings = get_settings()
    monkeypatch.setattr(settings, "supabase_jwks_url", "http://127.0.0.1:1/jwks.json")
    reset_jwks_cache()
    monkeypatch.setattr(
        "app.auth.tokens._jwks", lambda url: _FakeJWKS(private.public_key())
    )

    now = int(time.time())
    # Built by hand, because PyJWT refuses to ENCODE HS256 with a PEM key - a
    # good guardrail for honest callers and no obstacle at all to an attacker,
    # who is writing the bytes directly. The verifier is what is under test.
    forged = _hs256_by_hand(
        {
            "sub": OWNER,
            "aud": "authenticated",
            "role": "authenticated",
            "iss": f"{settings.supabase_url.rstrip('/')}/auth/v1",
            "iat": now,
            "exp": now + 3600,
        },
        secret=public_pem,          # the PUBLIC key, used as an HMAC secret
    )

    with pytest.raises(TokenRejected):
        verify(forged)

    reset_jwks_cache()


def test_an_unreachable_key_server_degrades_to_503_and_never_to_401(monkeypatch, anon):
    """Telling a user their credentials are bad when OUR key server is down
    sends them to reset a password that was never wrong, and buries an outage
    under a support queue."""
    from jwt.exceptions import PyJWKClientConnectionError

    settings = get_settings()
    monkeypatch.setattr(settings, "supabase_jwks_url", "http://127.0.0.1:1/jwks.json")
    reset_jwks_cache()

    def unreachable(url):
        raise PyJWKClientConnectionError("connection refused")

    monkeypatch.setattr("app.auth.tokens._jwks", unreachable)

    with pytest.raises(KeyServerUnavailable):
        verify(token_for(OWNER))

    resp = anon.get(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/dashboard", headers=auth(OWNER)
    )
    assert resp.status_code == 503

    reset_jwks_cache()


def test_the_refusal_message_does_not_say_which_check_failed(anon):
    """Which claim failed is exactly the feedback a forger uses to tune the next
    attempt. Expired, wrong key and wrong audience must be indistinguishable
    from outside."""
    now = int(time.time())
    bodies = set()
    for token in (
        token_for(OWNER, exp=now - 3600),
        token_for(OWNER, _key="a-different-key-that-is-long-enough-here"),
        token_for(OWNER, aud="anon"),
    ):
        resp = anon.get(
            f"/api/workspaces/{BROADMATE_WORKSPACE}/dashboard", headers=bearer(token)
        )
        assert resp.status_code == 401
        bodies.add(resp.text)
    assert len(bodies) == 1, f"the 401 body distinguishes failure modes: {bodies}"


def test_an_es256_token_from_the_published_jwks_is_accepted(monkeypatch):
    """The positive half of the algorithm-confusion test, and the path this
    deployment actually uses.

    `supabase status` still prints a JWT_SECRET and config.toml still has
    `signing_keys_path` commented out, so the plan for this work said HS256.
    The running stack disagrees: a real password grant comes back signed ES256
    with a `kid`, verified against GoTrue on 54321. Without this test the suite
    would pass entirely on a code path production does not take.
    """
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())

    settings = get_settings()
    monkeypatch.setattr(settings, "supabase_jwks_url", "http://127.0.0.1:1/jwks.json")
    reset_jwks_cache()
    monkeypatch.setattr(
        "app.auth.tokens._jwks", lambda url: _FakeJWKS(private.public_key())
    )

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": OWNER,
            "aud": "authenticated",
            "role": "authenticated",
            "iss": f"{settings.supabase_url.rstrip('/')}/auth/v1",
            "iat": now,
            "exp": now + 3600,
        },
        key=private,
        algorithm="ES256",
    )

    verified = verify(token)
    assert str(verified.subject) == OWNER

    reset_jwks_cache()


def test_the_hs256_fallback_is_not_consulted_when_a_jwks_is_configured(monkeypatch):
    """Precedence, asserted rather than assumed.

    If both are configured and the HMAC secret were still tried, an attacker who
    learned the (widely shared, often committed) local JWT_SECRET could forge a
    token against a deployment that had moved to asymmetric keys and believes it
    no longer has a shared secret to leak.
    """
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())

    settings = get_settings()
    monkeypatch.setattr(settings, "supabase_jwks_url", "http://127.0.0.1:1/jwks.json")
    reset_jwks_cache()
    monkeypatch.setattr(
        "app.auth.tokens._jwks", lambda url: _FakeJWKS(private.public_key())
    )

    # A perfectly good HS256 token, signed with the configured secret.
    with pytest.raises(TokenRejected):
        verify(token_for(OWNER))

    reset_jwks_cache()
