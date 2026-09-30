"""Onboarding: the operator creates an organisation, and a real customer can
exist without anybody writing SQL.

What is asserted is the DATABASE'S view after the call - the organisation, the
subscription on its plan, the workspace on its industry, the owner's
membership, and that ``t_advit.is_workspace_member`` says yes for them - not
the route's return value alone. The refusals are asserted the other way round:
after each one the marker finds nothing, because a half-made organisation is
worse than none.

GoTrue is never called. The one network call in ``app.auth.invite`` is replaced
with a fake that does what GoTrue would do - insert the auth user, which fires
the trigger that provisions ``core.platform_users`` - and returns a user id,
its own link and the token hash. That is the whole contract the route depends
on, and it is the part a real GoTrue cannot be asked to perform on a test's
schedule. What the route hands back is asserted to be OUR link - the web app's
``/auth/confirm`` carrying that hash - because GoTrue's own is the one that
lands nowhere.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.auth import invite
from app.config import get_settings
from conftest import ANALYST, OWNER, SUPERADMIN, auth

SUPERUSER_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
MARK = "onboarding-test"
ANALYST_EMAIL = "analyst@broadmate.local"   # supabase/seeds/01_core.sql
STRANGER_EMAIL = f"{MARK}-stranger@example.invalid"


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def superuser():
    return psycopg.connect(SUPERUSER_DSN)


@pytest.fixture(autouse=True)
def scrub_marked_organisations():
    """Everything this file creates carries the marker in its slug or its
    email, and is removed before and after, as superuser.

    The audit trail is the awkward part. ``core.audit_log.org_id`` is ``on
    delete set null``, and the append-only trigger refuses that UPDATE, so an
    organisation with a trail cannot be deleted at all - which is right for a
    customer and wrong for a fixture. The rows about the test organisations
    are removed first, with the delete trigger held open for exactly that
    statement; nothing else in the trail is touched.
    """

    def _scrub():
        with superuser() as conn, conn.cursor() as cur:
            cur.execute("alter table core.audit_log disable trigger audit_log_no_delete")
            cur.execute(
                "delete from core.audit_log where org_id in "
                "(select id from core.organisations where slug like %s)",
                (MARK + "%",),
            )
            cur.execute("alter table core.audit_log enable trigger audit_log_no_delete")
            # Organisations cascade to subscriptions, members and workspaces;
            # auth.users cascades to platform_users and its memberships.
            cur.execute("delete from core.organisations where slug like %s", (MARK + "%",))
            cur.execute("delete from auth.users where email like %s", (MARK + "%",))
            conn.commit()

    _scrub()
    yield
    _scrub()


@pytest.fixture
def no_service_key(monkeypatch):
    """The production state as of the deployment guide (docs/deployment.md): the runtime holds no
    service-role key, so it cannot invite."""
    monkeypatch.setattr(get_settings(), "supabase_service_role_key", "")


@pytest.fixture
def no_web_base_url(monkeypatch):
    """A runtime with the key but no WEB_BASE_URL: it could create the user
    and would have nowhere to point the link. It must refuse, not default."""
    monkeypatch.setattr(get_settings(), "supabase_service_role_key", f"{MARK}-service-key")
    monkeypatch.setattr(get_settings(), "web_base_url", "")


HASHED_TOKEN = f"{MARK}-hashed-token"
GOTRUE_LINK = f"http://127.0.0.1:54321/auth/v1/verify?token={HASHED_TOKEN}&type=invite&redirect_to=http://127.0.0.1:3000"
WEB_BASE_URL = f"http://web.{MARK}.invalid"


def _gotrue_user(email: str) -> str:
    """What GoTrue does on its own connection: create the auth user. The
    trigger provisions ``core.platform_users`` in the same statement."""
    user_id = str(uuid.uuid4())
    with superuser() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into auth.users (instance_id, id, aud, role, email, encrypted_password,
                                    created_at, updated_at, raw_app_meta_data, raw_user_meta_data,
                                    confirmation_token, recovery_token, email_change_token_new, email_change)
            values ('00000000-0000-0000-0000-000000000000', %s::uuid, 'authenticated', 'authenticated',
                    %s, '', now(), now(), '{"provider":"email","providers":["email"]}'::jsonb,
                    '{}'::jsonb, '', '', '', '')
            """,
            (user_id, email),
        )
        conn.commit()
    return user_id


@pytest.fixture
def web_base_url(monkeypatch):
    """A base URL no real web app answers on, so the assertion that the link
    points at it cannot pass by pointing at the default."""
    monkeypatch.setattr(get_settings(), "web_base_url", WEB_BASE_URL)
    return WEB_BASE_URL


@pytest.fixture
def fake_gotrue(monkeypatch, web_base_url):
    """A GoTrue that creates the auth user and hands back what generate_link
    does - action_link, hashed_token, verification_type, redirect_to at top
    level - without a network. Records what it was asked so the test can check
    the request shape - the admin endpoint, the type, both credential headers."""
    calls: list[dict] = []

    def _fake(url: str, key: str, payload: dict) -> dict:
        calls.append({"url": url, "key": key, "payload": payload})
        if payload["type"] == "recovery":
            # An existing user: GoTrue creates nothing and answers with the
            # user it has, or refuses when it has none.
            with superuser() as conn, conn.cursor() as cur:
                cur.execute("select id::text from auth.users where email = %s", (payload["email"],))
                row = cur.fetchone()
            if row is None:
                raise invite.InviteRefused("Supabase Auth refused: HTTP 422 User not found")
            user_id = row[0]
        else:
            user_id = _gotrue_user(payload["email"])
        return {
            "id": user_id,
            "email": payload["email"],
            "action_link": GOTRUE_LINK,
            "hashed_token": HASHED_TOKEN,
            "verification_type": payload["type"],
            "redirect_to": "http://127.0.0.1:3000",
        }

    monkeypatch.setattr(get_settings(), "supabase_service_role_key", f"{MARK}-service-key")
    monkeypatch.setattr(invite, "_call_gotrue", _fake)
    return calls


def _body(**overrides) -> dict:
    body = {
        "name": f"{MARK} Herbals {uuid.uuid4().hex[:6]}",
        "plan_key": "growth",
        "industry_key": "ayurveda",
        "workspace_name": "Herbals main",
        "owner_email": ANALYST_EMAIL,
    }
    body.update(overrides)
    return body


def _organisations_marked() -> int:
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from core.organisations where slug like %s", (MARK + "%",))
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Invisible to a tenant
# ---------------------------------------------------------------------------


def test_a_tenant_owner_cannot_create_an_organisation_or_list_industries(client):
    r = client.post("/api/admin/organisations", json=_body(), headers=auth(OWNER))
    assert r.status_code == 404
    assert client.get("/api/admin/industries", headers=auth(OWNER)).status_code == 404
    assert _organisations_marked() == 0


def test_the_industries_route_lists_every_pack_with_its_status(client):
    rows = {i["key"]: i for i in client.get("/api/admin/industries", headers=auth(SUPERADMIN)).json()}
    assert {"ayurveda", "general_d2c"} <= set(rows)
    assert rows["ayurveda"]["status"] == "active" and rows["ayurveda"]["display_name"]


# ---------------------------------------------------------------------------
# The happy path, with an owner who already has an account
# ---------------------------------------------------------------------------


def test_an_existing_user_becomes_the_owner_of_a_new_organisation(client, no_service_key):
    r = client.post("/api/admin/organisations", json=_body(name=f"{MARK} Herbals"), headers=auth(SUPERADMIN))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == f"{MARK}-herbals", "derived from the name"
    assert body["owner_user_id"] == ANALYST
    assert body["invited"] is False and body["action_link"] is None

    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select status::text, activated_at, created_by::text, billing_email::text "
                    "from core.organisations where id = %s::uuid", (body["org_id"],))
        status, activated_at, created_by, billing_email = cur.fetchone()
        assert status == "active" and activated_at is not None
        assert created_by == SUPERADMIN and billing_email == ANALYST_EMAIL

        cur.execute("select p.key, s.status::text, s.trial_ends_at, s.current_period_start, s.current_period_end "
                    "from core.subscriptions s join core.plans p on p.id = s.plan_id "
                    "where s.id = %s::uuid and s.org_id = %s::uuid", (body["subscription_id"], body["org_id"]))
        plan_key, sub_status, trial_ends_at, period_start, period_end = cur.fetchone()
        assert plan_key == "growth" and sub_status == "trialing"
        # growth carries 14 trial days (20260917000002); monthly billing.
        assert timedelta(days=13) < trial_ends_at - period_start < timedelta(days=15)
        assert timedelta(days=27) < period_end - period_start < timedelta(days=32)

        cur.execute("select org_id::text, industry_key, name, autonomy_level, daily_cap_inr, monthly_cap_inr "
                    "from t_advit.workspaces where id = %s::uuid", (body["workspace_id"],))
        org_id, industry_key, name, autonomy, daily, monthly = cur.fetchone()
        assert org_id == body["org_id"] and industry_key == "ayurveda" and name == "Herbals main"
        assert autonomy == 1, "PRD D4: a new workspace starts at L1"
        assert monthly >= daily > 0

        cur.execute("select role::text, invited_by::text from core.organisation_members "
                    "where org_id = %s::uuid and user_id = %s::uuid", (body["org_id"], ANALYST))
        assert cur.fetchone() == ("owner", SUPERADMIN)
        cur.execute("select t_advit.is_workspace_member(%s::uuid, %s::uuid)", (body["workspace_id"], ANALYST))
        assert cur.fetchone()[0] is True

        # The pack's starting hypotheses landed in the tenant table, labelled
        # with the source the industries migration promised.
        cur.execute("select count(*) from t_advit.industry_context_defaults where industry_key = 'ayurveda'")
        expected = cur.fetchone()[0]
        cur.execute("select count(*), bool_and(source = 'industry_pack') from t_advit.account_context "
                    "where workspace_id = %s::uuid", (body["workspace_id"],))
        assert cur.fetchone() == (expected, True) and expected > 0

        cur.execute("select actor_type::text, actor_id::text, payload_json from core.audit_log "
                    "where event = 'organisation.created' and org_id = %s::uuid", (body["org_id"],))
        actor_type, actor_id, payload = cur.fetchone()
        assert actor_type == "superadmin" and actor_id == SUPERADMIN
        assert payload == {"plan_key": "growth", "industry_key": "ayurveda", "workspace_id": body["workspace_id"],
                           "owner_email": ANALYST_EMAIL, "invited": False}

    # The console can show the result, and the owner can use the product:
    # the workspace resolver is the same predicate asserted above.
    detail = client.get(f"/api/admin/organisations/{body['org_id']}", headers=auth(SUPERADMIN)).json()
    assert detail["plan_key"] == "growth" and detail["subscription_status"] == "trialing"
    assert [m["user_id"] for m in detail["members"]] == [ANALYST]
    assert [w["industry_key"] for w in detail["workspaces"]] == ["ayurveda"]
    r = client.get(f"/api/workspaces/{body['workspace_id']}/connections/health", headers=auth(ANALYST))
    assert r.status_code == 200, r.text


def test_without_a_trial_the_subscription_is_active_from_today(client, no_service_key):
    r = client.post("/api/admin/organisations", json=_body(trial=False), headers=auth(SUPERADMIN))
    assert r.status_code == 201, r.text
    assert r.json()["subscription_status"] == "active" and r.json()["trial_ends_at"] is None


# ---------------------------------------------------------------------------
# Refusals, each leaving nothing behind
# ---------------------------------------------------------------------------


def test_an_unknown_plan_is_refused_by_name(client, no_service_key):
    r = client.post("/api/admin/organisations", json=_body(plan_key="platinum"), headers=auth(SUPERADMIN))
    assert r.status_code == 422 and "platinum" in r.text and "plan_unknown" in r.text
    assert _organisations_marked() == 0


def test_an_unknown_industry_is_refused_by_name(client, no_service_key):
    r = client.post("/api/admin/organisations", json=_body(industry_key="crypto"), headers=auth(SUPERADMIN))
    assert r.status_code == 422 and "crypto" in r.text and "industry_unknown" in r.text
    assert _organisations_marked() == 0


def test_a_duplicate_slug_is_a_conflict(client, no_service_key):
    first = client.post("/api/admin/organisations", json=_body(name=f"{MARK} Twice"), headers=auth(SUPERADMIN))
    assert first.status_code == 201, first.text
    again = client.post("/api/admin/organisations", json=_body(name=f"{MARK} twice", slug=f"{MARK}-twice"),
                        headers=auth(SUPERADMIN))
    assert again.status_code == 409 and "slug_taken" in again.text
    assert _organisations_marked() == 1


def test_an_unknown_owner_with_no_service_key_is_refused_and_nothing_is_created(client, no_service_key):
    r = client.post("/api/admin/organisations", json=_body(owner_email=STRANGER_EMAIL), headers=auth(SUPERADMIN))
    assert r.status_code == 422, r.text
    assert "owner_unknown" in r.text and "Supabase Studio" in r.text
    assert _organisations_marked() == 0
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from core.platform_users where email = %s", (STRANGER_EMAIL,))
        assert cur.fetchone()[0] == 0
        cur.execute("select count(*) from core.audit_log where event = 'organisation.created' "
                    "and payload_json->>'owner_email' = %s", (STRANGER_EMAIL,))
        assert cur.fetchone()[0] == 0, "a refusal leaves no trail claiming a creation"


# ---------------------------------------------------------------------------
# The GoTrue path
# ---------------------------------------------------------------------------


def test_an_unknown_owner_is_invited_and_the_link_is_returned_for_the_operator_to_send(client, fake_gotrue):
    r = client.post("/api/admin/organisations", json=_body(owner_email=STRANGER_EMAIL), headers=auth(SUPERADMIN))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["invited"] is True
    # The link to send is OURS: the web app's confirm page, carrying
    # GoTrue's token hash and the type it must be verified as. GoTrue's own
    # link - the one that lands nowhere - is not in the response at all: the
    # operator is handed exactly one thing to send.
    parsed = urlsplit(body["action_link"])
    assert f"{parsed.scheme}://{parsed.netloc}" == WEB_BASE_URL
    assert parsed.path == "/auth/confirm"
    assert parse_qs(parsed.query) == {"token_hash": [HASHED_TOKEN], "type": ["invite"]}
    assert "gotrue_link" not in body
    assert GOTRUE_LINK not in r.text
    assert "no email was sent" in body["note"] and "sign-up page" in body["note"]

    # One call, to the admin endpoint, as an invitation, with both headers'
    # worth of credential - the shape GoTrue's generate_link expects.
    assert len(fake_gotrue) == 1
    call = fake_gotrue[0]
    assert call["url"].endswith("/auth/v1/admin/generate_link")
    assert call["key"] == f"{MARK}-service-key"
    assert call["payload"] == {"type": "invite", "email": STRANGER_EMAIL}

    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select id::text from core.platform_users where email = %s", (STRANGER_EMAIL,))
        user_id = cur.fetchone()[0]
        assert user_id == body["owner_user_id"], "the trigger provisioned the row GoTrue's user id names"
        cur.execute("select role::text from core.organisation_members where org_id = %s::uuid and user_id = %s::uuid",
                    (body["org_id"], user_id))
        assert cur.fetchone() == ("owner",)
        cur.execute("select t_advit.is_workspace_member(%s::uuid, %s::uuid)", (body["workspace_id"], user_id))
        assert cur.fetchone()[0] is True
        cur.execute("select payload_json->>'invited' from core.audit_log "
                    "where event = 'organisation.created' and org_id = %s::uuid", (body["org_id"],))
        assert cur.fetchone()[0] == "true"


def test_a_gotrue_answer_without_a_token_hash_is_refused_and_creates_nothing(client, monkeypatch, web_base_url):
    """GoTrue's own link cannot be handed out (it lands nowhere), so an answer
    that carries only it is an invitation nobody could accept. Refused with
    the reason, and no organisation is written for the retry to trip over."""
    monkeypatch.setattr(get_settings(), "supabase_service_role_key", f"{MARK}-service-key")

    def _no_hash(url: str, key: str, payload: dict) -> dict:
        return {"id": _gotrue_user(payload["email"]), "email": payload["email"], "action_link": GOTRUE_LINK}

    monkeypatch.setattr(invite, "_call_gotrue", _no_hash)
    r = client.post("/api/admin/organisations", json=_body(owner_email=STRANGER_EMAIL), headers=auth(SUPERADMIN))
    assert r.status_code == 502, r.text
    assert "invite_refused" in r.text and "hashed_token" in r.text
    assert _organisations_marked() == 0
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from core.organisation_members m join core.platform_users u on u.id = m.user_id "
                    "where u.email = %s", (STRANGER_EMAIL,))
        assert cur.fetchone()[0] == 0, "the auth user GoTrue made owns nothing"
        cur.execute("select count(*) from core.audit_log where event = 'organisation.created' "
                    "and payload_json->>'owner_email' = %s", (STRANGER_EMAIL,))
        assert cur.fetchone()[0] == 0


def test_the_confirm_link_query_encodes_the_hash_so_a_url_character_cannot_change_what_is_read(web_base_url):
    link = invite.confirm_link("a+b&c=d/e", "invite")
    parsed = urlsplit(link)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{WEB_BASE_URL}/auth/confirm"
    assert parse_qs(parsed.query) == {"token_hash": ["a+b&c=d/e"], "type": ["invite"]}


def test_a_gotrue_refusal_creates_nothing(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "supabase_service_role_key", f"{MARK}-service-key")

    def _refuse(url: str, key: str, payload: dict) -> dict:
        raise invite.InviteRefused("Supabase Auth refused the invitation: HTTP 422 email is invalid")

    monkeypatch.setattr(invite, "_call_gotrue", _refuse)
    r = client.post("/api/admin/organisations", json=_body(owner_email=STRANGER_EMAIL), headers=auth(SUPERADMIN))
    assert r.status_code == 502 and "invite_refused" in r.text
    assert _organisations_marked() == 0


def test_the_invite_module_refuses_without_a_key_before_any_network(monkeypatch, no_service_key):
    def _never(url: str, key: str, payload: dict) -> dict:
        raise AssertionError("a GoTrue call was made with no credential to make it")

    monkeypatch.setattr(invite, "_call_gotrue", _never)
    assert invite.invites_are_possible() is False
    with pytest.raises(invite.InviteUnavailable):
        invite.generate_invite(STRANGER_EMAIL)


def test_without_a_web_base_url_the_runtime_refuses_to_invite_rather_than_default_to_localhost(
    client, monkeypatch, no_web_base_url
):
    """The production failure this guards: the key is set, WEB_BASE_URL is
    forgotten, and a default of http://localhost:3000 would have handed the
    owner a link to a laptop. Nothing is written, no GoTrue call is made, and
    the refusal names the variable."""
    def _never(url: str, key: str, payload: dict) -> dict:
        raise AssertionError("a GoTrue call was made with nowhere for the link to point")

    monkeypatch.setattr(invite, "_call_gotrue", _never)
    assert invite.missing_invite_configuration() == ("WEB_BASE_URL",)
    assert invite.invites_are_possible() is False
    with pytest.raises(invite.InviteUnavailable, match="WEB_BASE_URL"):
        invite.generate_invite(STRANGER_EMAIL)

    r = client.post("/api/admin/organisations", json=_body(owner_email=STRANGER_EMAIL), headers=auth(SUPERADMIN))
    assert r.status_code == 422, r.text
    assert "owner_unknown" in r.text and "WEB_BASE_URL" in r.text
    assert "SUPABASE_SERVICE_ROLE_KEY" not in r.text, "the key is set; the message names only what is missing"
    assert _organisations_marked() == 0
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from core.platform_users where email = %s", (STRANGER_EMAIL,))
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# A fresh link for an account that exists
# ---------------------------------------------------------------------------


def _created(client, fake_gotrue) -> dict:
    r = client.post("/api/admin/organisations", json=_body(owner_email=STRANGER_EMAIL), headers=auth(SUPERADMIN))
    assert r.status_code == 201, r.text
    return r.json()


def test_a_sign_in_link_is_a_recovery_link_for_the_member_and_is_audited_without_the_link(client, fake_gotrue):
    created = _created(client, fake_gotrue)
    r = client.post(f"/api/admin/organisations/{created['org_id']}/members/{created['owner_user_id']}/sign-in-link",
                    headers=auth(SUPERADMIN))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user_id"] == created["owner_user_id"] and body["email"] == STRANGER_EMAIL

    # Our confirm page, type recovery: signs in once, asks for a password.
    parsed = urlsplit(body["action_link"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{WEB_BASE_URL}/auth/confirm"
    assert parse_qs(parsed.query) == {"token_hash": [HASHED_TOKEN], "type": ["recovery"]}
    assert GOTRUE_LINK not in r.text

    # GoTrue was asked for a recovery link for exactly that address, and
    # created nobody: the second call is the link, the first was the invite.
    assert [c["payload"] for c in fake_gotrue] == [
        {"type": "invite", "email": STRANGER_EMAIL},
        {"type": "recovery", "email": STRANGER_EMAIL},
    ]
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from auth.users where email = %s", (STRANGER_EMAIL,))
        assert cur.fetchone()[0] == 1
        cur.execute("select payload_json from core.audit_log where event = 'organisation.sign_in_link_issued' "
                    "and org_id = %s::uuid", (created["org_id"],))
        trail = cur.fetchone()
    assert trail is not None, "issuing a sign-in link is an act on the organisation and leaves a row"
    payload = trail[0]
    assert payload["user_id"] == created["owner_user_id"] and payload["link_type"] == "recovery"
    assert HASHED_TOKEN not in json.dumps(payload) and "action_link" not in payload, \
        "the link is a credential; the trail says who, never what"


def test_a_sign_in_link_for_somebody_who_is_not_a_member_is_404_and_asks_gotrue_nothing(client, fake_gotrue):
    created = _created(client, fake_gotrue)
    calls_before = len(fake_gotrue)
    # A real platform user who is not a member of this organisation, and a
    # user id that exists nowhere: the same answer for both.
    for user_id in (OWNER, str(uuid.uuid4())):
        r = client.post(f"/api/admin/organisations/{created['org_id']}/members/{user_id}/sign-in-link",
                        headers=auth(SUPERADMIN))
        assert r.status_code == 404, r.text
    # And an organisation that does not exist, for a user who does.
    r = client.post(f"/api/admin/organisations/{uuid.uuid4()}/members/{created['owner_user_id']}/sign-in-link",
                    headers=auth(SUPERADMIN))
    assert r.status_code == 404
    assert len(fake_gotrue) == calls_before, "no link was minted for a refused request"


def test_a_tenant_owner_cannot_issue_a_sign_in_link(client, fake_gotrue):
    created = _created(client, fake_gotrue)
    calls_before = len(fake_gotrue)
    r = client.post(f"/api/admin/organisations/{created['org_id']}/members/{created['owner_user_id']}/sign-in-link",
                    headers=auth(OWNER))
    assert r.status_code == 404
    assert len(fake_gotrue) == calls_before


def test_a_sign_in_link_without_the_configuration_is_503_and_names_the_variable(client, fake_gotrue, monkeypatch):
    created = _created(client, fake_gotrue)
    monkeypatch.setattr(get_settings(), "supabase_service_role_key", "")
    r = client.post(f"/api/admin/organisations/{created['org_id']}/members/{created['owner_user_id']}/sign-in-link",
                    headers=auth(SUPERADMIN))
    assert r.status_code == 503 and "link_unavailable" in r.text and "SUPABASE_SERVICE_ROLE_KEY" in r.text
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from core.audit_log where event = 'organisation.sign_in_link_issued' "
                    "and org_id = %s::uuid", (created["org_id"],))
        assert cur.fetchone()[0] == 0, "a refusal leaves no trail claiming a link was issued"


def test_the_invite_module_refuses_a_link_type_it_does_not_issue(monkeypatch, web_base_url):
    monkeypatch.setattr(get_settings(), "supabase_service_role_key", f"{MARK}-service-key")

    def _never(url: str, key: str, payload: dict) -> dict:
        raise AssertionError("GoTrue was asked for a link type this product does not issue")

    monkeypatch.setattr(invite, "_call_gotrue", _never)
    with pytest.raises(ValueError, match="magiclink"):
        invite.generate_link(STRANGER_EMAIL, "magiclink")


def test_the_route_refuses_a_slug_that_could_not_have_been_derived(client, no_service_key):
    r = client.post("/api/admin/organisations", json=_body(slug="Not A Slug"), headers=auth(SUPERADMIN))
    assert r.status_code == 422 and "slug_invalid" in r.text
    r = client.post("/api/admin/organisations", json=_body(owner_email="nobody"), headers=auth(SUPERADMIN))
    assert r.status_code == 422 and "owner_email_invalid" in r.text
    assert _organisations_marked() == 0


def test_the_trial_end_is_computed_on_the_database_clock(client, no_service_key):
    """The response carries the timestamps the row holds, and they are the
    database's `now()`, not this process's - so two replicas agree."""
    r = client.post("/api/admin/organisations", json=_body(), headers=auth(SUPERADMIN))
    assert r.status_code == 201, r.text
    ends = datetime.fromisoformat(r.json()["trial_ends_at"])
    assert abs((ends - datetime.now(timezone.utc)) - timedelta(days=14)) < timedelta(minutes=5)


def test_the_legacy_plan_is_refused_for_a_new_organisation(client):
    """20260917000002 keeps `standard` active for the subscriptions already
    on it and says new sign-ups choose a PRD 19.3 tier. The route is where
    that rule lives - a form default only steers - and nothing is created."""
    before = _organisations_marked()
    r = client.post("/api/admin/organisations", json=_body(plan_key="standard"), headers=auth(SUPERADMIN))
    assert r.status_code == 422 and "plan_not_for_new_signups" in r.text
    assert _organisations_marked() == before
