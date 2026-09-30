"""Bring-your-own-key, and the model tier per function.

The product is sold on the customer supplying their own OpenRouter key: they see
their own spend, they set their own limits, and this business does not resell
tokens. None of it existed — `ModelRouter` read ONE key from `Settings`, so every
organisation's calls were billed to whichever key the process started with, which
is both the wrong commercial model and one revoked key away from taking every
tenant down at once.

The tests that matter most are the two about the AAD binding. Without it, write
access to `core.org_secrets` would be enough to make tenant B's runs spend tenant
A's OpenRouter credit, and every individual value in the copied row would look
correct.
"""

from __future__ import annotations

import base64
import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.router import RoutingConfig
from app.secrets import store as secrets
from app.secrets.envelope import (
    SecretsNotConfigured,
    SecretUndecryptable,
    aad,
    current_version,
    hint_for,
    open_sealed,
    seal,
)
from conftest import BROADMATE_WORKSPACE as WORKSPACE
from conftest import MEMBER, OWNER, RIVAL_WORKSPACE, auth

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
ORG = "00000000-0000-4000-8000-000000000010"
RIVAL_ORG = "00000000-0000-4000-8000-000000000011"
KEY = "sk-or-v1-0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    """A master key for the duration of the test, so the suite does not depend
    on one being configured in the environment it runs in."""
    monkeypatch.setenv("ADVIT_SECRETS_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    secrets.clear_cache()
    yield
    secrets.clear_cache()


@pytest.fixture
def clean_secrets():
    def wipe():
        with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("delete from core.org_secrets")
            cur.execute("delete from t_advit.model_preferences")

    wipe()
    yield
    wipe()


@pytest.fixture
def client() -> TestClient:
    c = TestClient(app)
    c.headers.update(auth(OWNER))
    return c


# ---------------------------------------------------------------------------
# The cryptography
# ---------------------------------------------------------------------------


def test_a_key_round_trips():
    sealed = seal(KEY, org_id=ORG, kind=secrets.OPENROUTER)
    assert open_sealed(sealed, org_id=ORG, kind=secrets.OPENROUTER) == KEY
    assert sealed.key_version == current_version()


def test_a_row_moved_to_another_organisation_does_not_decrypt():
    """THE property. The AAD is derived from (org_id, kind) and is never stored,
    so copying a row between organisations produces a ciphertext that does not
    authenticate — rather than one that decrypts into the wrong tenant's
    runtime and quietly spends their OpenRouter credit."""
    sealed = seal(KEY, org_id=ORG, kind=secrets.OPENROUTER)
    with pytest.raises(SecretUndecryptable):
        open_sealed(sealed, org_id=RIVAL_ORG, kind=secrets.OPENROUTER)


def test_a_row_reused_for_another_kind_does_not_decrypt():
    """The same binding, one axis over. An OpenRouter key presented as a Meta
    token would be sent to the wrong provider in clear."""
    sealed = seal(KEY, org_id=ORG, kind=secrets.OPENROUTER)
    with pytest.raises(SecretUndecryptable):
        open_sealed(sealed, org_id=ORG, kind="meta_access_token")


def test_the_aad_is_not_stored_anywhere():
    """If it were, moving a row would mean moving the AAD with it, and the
    binding would bind nothing."""
    sealed = seal(KEY, org_id=ORG, kind=secrets.OPENROUTER)
    binding = aad(ORG, secrets.OPENROUTER)
    assert binding not in sealed.ciphertext
    assert binding not in sealed.nonce


def test_two_seals_of_one_value_differ():
    """GCM's failure mode on nonce reuse is not degraded security, it is
    catastrophic: two messages under one key and nonce leak their XOR and the
    authentication key itself."""
    first = seal(KEY, org_id=ORG, kind=secrets.OPENROUTER)
    second = seal(KEY, org_id=ORG, kind=secrets.OPENROUTER)
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext


def test_a_tampered_ciphertext_is_refused():
    sealed = seal(KEY, org_id=ORG, kind=secrets.OPENROUTER)
    flipped = bytearray(sealed.ciphertext)
    flipped[0] ^= 0x01
    with pytest.raises(SecretUndecryptable):
        open_sealed(
            type(sealed)(nonce=sealed.nonce, ciphertext=bytes(flipped),
                         key_version=sealed.key_version),
            org_id=ORG, kind=secrets.OPENROUTER,
        )


def test_without_a_master_key_storing_refuses_rather_than_writing_in_clear(monkeypatch):
    """The one behaviour that would make this module worse than not having it."""
    monkeypatch.delenv("ADVIT_SECRETS_MASTER_KEY", raising=False)
    with pytest.raises(SecretsNotConfigured):
        seal(KEY, org_id=ORG, kind=secrets.OPENROUTER)


def test_a_master_key_of_the_wrong_length_is_refused(monkeypatch):
    monkeypatch.setenv("ADVIT_SECRETS_MASTER_KEY", base64.b64encode(os.urandom(16)).decode())
    with pytest.raises(SecretsNotConfigured, match="AES-256"):
        seal(KEY, org_id=ORG, kind=secrets.OPENROUTER)


def test_rotation_keeps_earlier_rows_readable(monkeypatch):
    """Without versioning, rotating means re-encrypting every row inside one
    migration that must not be interrupted."""
    old = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("ADVIT_SECRETS_MASTER_KEY", old)
    sealed_v1 = seal(KEY, org_id=ORG, kind=secrets.OPENROUTER)
    assert sealed_v1.key_version == 1

    # The new key arrives alongside the old one, which is what a rotation looks
    # like for as long as it takes to re-encrypt.
    monkeypatch.setenv("ADVIT_SECRETS_MASTER_KEY_2", base64.b64encode(os.urandom(32)).decode())
    assert current_version() == 2
    assert open_sealed(sealed_v1, org_id=ORG, kind=secrets.OPENROUTER) == KEY

    sealed_v2 = seal(KEY, org_id=ORG, kind=secrets.OPENROUTER)
    assert sealed_v2.key_version == 2


def test_the_hint_is_short_enough_to_be_useless_to_an_attacker():
    """Four characters of a high-entropy key narrow nothing, and are enough for
    an owner to tell two of their own keys apart — which is the only job the
    field has."""
    assert hint_for(KEY) == KEY[-4:]
    assert len(hint_for(KEY)) == 4
    assert hint_for("short") == ""


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def test_storing_and_resolving_a_key(clean_secrets):
    stored = secrets.store(ORG, KEY)
    assert stored["hint"] == KEY[-4:]
    assert secrets.resolve(ORG) == KEY


def test_an_organisation_with_no_key_is_told_so_rather_than_given_one(clean_secrets):
    """No fallback to a platform key. That would silently bill this business for
    the customer's usage and hide the configuration gap until the invoice."""
    with pytest.raises(secrets.NoKeyForOrganisation):
        secrets.resolve(RIVAL_ORG)


def test_one_organisation_cannot_resolve_anothers_key(clean_secrets):
    secrets.store(ORG, KEY)
    with pytest.raises(secrets.NoKeyForOrganisation):
        secrets.resolve(RIVAL_ORG)


def test_replacing_a_key_clears_the_previous_verification(clean_secrets):
    """A new key has not been verified, and the previous key's verification says
    nothing about it. Otherwise a settings page reports "working" about a key
    that has never been used."""
    secrets.store(ORG, KEY)
    secrets.record_outcome(ORG)
    assert secrets.status(ORG)[0]["is_working"] is True

    secrets.store(ORG, "sk-or-v1-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")
    assert secrets.status(ORG)[0]["is_working"] is False


def test_a_failure_is_recorded_distinctly_from_never_having_worked(clean_secrets):
    """"Never worked" is a typo at setup; "stopped working" is a revocation or a
    spend limit. A page that says only "invalid" cannot tell an owner which of
    their afternoons to spend on it."""
    secrets.store(ORG, KEY)
    secrets.record_outcome(ORG, error="401 from OpenRouter")
    status = secrets.status(ORG)[0]
    assert status["is_working"] is False
    assert "401" in status["last_error"]


def test_rotating_a_key_clears_the_cache(clean_secrets):
    """The cache exists so a credential lookup is not on the hot path of every
    completion. A rotation that did not clear it would leave the old key in use
    for as long as the TTL."""
    secrets.store(ORG, KEY)
    assert secrets.resolve(ORG) == KEY

    replacement = "sk-or-v1-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"
    secrets.store(ORG, replacement)
    assert secrets.resolve(ORG) == replacement


def test_the_status_view_never_carries_the_key(clean_secrets):
    secrets.store(ORG, KEY)
    rendered = str(secrets.status(ORG))
    assert KEY not in rendered
    assert KEY[:20] not in rendered


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def config() -> RoutingConfig:
    from app.config import get_settings

    return RoutingConfig.load(get_settings().routing_config_path)


def test_every_function_offers_three_options_and_marks_one(config):
    for tier in config.tiers.values():
        if tier.fixed:
            continue
        assert set(tier.options) == {"best", "value", "cheap"}, tier.key
        assert tier.recommended in tier.options, tier.key
        assert tier.why_recommended, f"{tier.key} recommends a tier and does not say why"


def test_every_tier_resolves_to_a_class_that_exists(config):
    """A tier naming a class that was renamed would fall back to the role
    default silently, and the customer's choice would stop applying with nothing
    to show for it."""
    for tier in config.tiers.values():
        for name, class_name in tier.options.items():
            assert class_name in config.classes, f"{tier.key}.{name} -> {class_name}"
            assert config.classes[class_name].chain(), f"{tier.key}.{name} resolves to no model"


def test_no_choice_means_the_recommendation(config):
    for tier in config.tiers.values():
        expected = config.classes[tier.options[tier.recommended]] if tier.options else None
        resolved = config.class_for_choice(tier.role, None)
        if expected is not None:
            assert resolved.name == expected.name, tier.key


def test_a_choice_the_catalogue_no_longer_offers_falls_back_to_the_recommendation(config):
    """Removing a tier must not break every organisation that had selected it,
    at the moment of the deploy, on every call."""
    assert config.class_for_choice("strategy", "platinum").name == (
        config.tiers["strategy"].options[config.tiers["strategy"].recommended]
    )


def test_compliance_adjudication_is_not_a_customer_choice(config):
    """Offering a cheaper model for the check that keeps an advertiser legal is
    offering them a discount on their own compliance, and "you selected the
    cheap tier" is not an answer this business can give."""
    tier = config.tiers["compliance_judge"]
    assert tier.fixed is True
    assert tier.options == {}
    assert config.class_for_choice("compliance_judge", "cheap").name == "judgement"


def test_the_cheap_tier_is_an_open_weight_model(config):
    """The customer asked for open-source options considered. `cheap` resolves
    to the bulk class, whose primary is open-weight - which is what makes the
    tier genuinely cheap rather than a smaller frontier model."""
    cheap = config.classes[config.tiers["strategy"].options["cheap"]]
    assert cheap.primary is not None
    assert cheap.primary.startswith("moonshotai/"), cheap.primary


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def test_the_settings_page_never_receives_the_key(client, clean_secrets):
    client.put(
        f"/api/workspaces/{WORKSPACE}/settings/byok", json={"api_key": KEY}
    ).raise_for_status()

    body = client.get(f"/api/workspaces/{WORKSPACE}/settings/byok").text
    assert KEY not in body
    assert KEY[-4:] in body


def test_a_member_cannot_store_a_key(clean_secrets):
    """The write is on the SERVICE connection, because the ciphertext must not
    be tenant-writable — so RLS cannot enforce this and a written check does."""
    member = TestClient(app)
    member.headers.update(auth(MEMBER))
    response = member.put(
        f"/api/workspaces/{WORKSPACE}/settings/byok", json={"api_key": KEY}
    )
    assert response.status_code == 403


def test_another_tenant_cannot_reach_this_organisations_settings(clean_secrets):
    from conftest import OUTSIDER

    outsider = TestClient(app)
    outsider.headers.update(auth(OUTSIDER))
    assert outsider.get(
        f"/api/workspaces/{WORKSPACE}/settings/byok"
    ).status_code == 404


def test_the_catalogue_names_the_recommendation_and_the_reason(client, clean_secrets):
    body = client.get(f"/api/workspaces/{WORKSPACE}/settings/models").json()
    functions = {f["key"]: f for f in body["functions"]}

    assert "strategy" in functions
    strategy = functions["strategy"]
    assert strategy["why_recommended"], "a choice offered without a reason is one made on price"
    assert any(o["recommended"] for o in strategy["options"])
    # Absent, not pre-filled with the recommendation: that is what lets the
    # recommendation be improved for everyone who has never opened this page.
    assert strategy["chosen"] is None


def test_choosing_a_tier_is_stored_and_says_what_it_resolves_to(client, clean_secrets):
    response = client.put(
        f"/api/workspaces/{WORKSPACE}/settings/models",
        json={"role": "strategy", "tier": "cheap"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "cheap"
    assert body["model"], "the page cannot tell the owner which model they just chose"

    again = client.get(f"/api/workspaces/{WORKSPACE}/settings/models").json()
    strategy = next(f for f in again["functions"] if f["key"] == "strategy")
    assert strategy["chosen"] == "cheap"


def test_the_fixed_function_refuses_a_choice_rather_than_ignoring_it(client, clean_secrets):
    """A settings page that appeared to save a choice the system then
    disregarded would be worse than one that says no."""
    response = client.put(
        f"/api/workspaces/{WORKSPACE}/settings/models",
        json={"role": "compliance_judge", "tier": "cheap"},
    )
    assert response.status_code == 422
    assert "not a customer choice" in response.json()["detail"]


def test_an_unknown_function_is_refused_with_the_list(client, clean_secrets):
    response = client.put(
        f"/api/workspaces/{WORKSPACE}/settings/models",
        json={"role": "telepathy", "tier": "best"},
    )
    assert response.status_code == 422
    assert "strategy" in response.json()["detail"]


def test_the_chosen_tier_reaches_the_router(client, clean_secrets):
    """End to end: the choice is not merely stored, it is what the next model
    call routes on."""
    from app.models.for_org import tier_choices

    client.put(
        f"/api/workspaces/{WORKSPACE}/settings/models",
        json={"role": "analytics", "tier": "best"},
    ).raise_for_status()

    assert tier_choices(ORG)["analytics"] == "best"


def test_every_function_the_settings_page_lists_names_the_role_the_put_resolves(client, clean_secrets):
    """The GET and the PUT must agree on the identifier. The PUT resolves a
    choice by role (tier_for_role); the "chat" tier's role is "orchestrator",
    so a page that only had the key sent "chat" and was refused. Each entry
    now carries its role, and the role is one the PUT would accept."""
    body = client.get(f"/api/workspaces/{WORKSPACE}/settings/models").json()
    for fn in body["functions"]:
        assert fn.get("role"), f"{fn['key']} carries no role"
        # The fixed function offers no options and refuses every choice; the
        # others must accept their own role.
        if fn["fixed"] or not fn["options"]:
            continue
        response = client.put(
            f"/api/workspaces/{WORKSPACE}/settings/models",
            json={"role": fn["role"], "tier": fn["options"][0]["tier"]},
        )
        assert response.status_code == 200, (fn["key"], fn["role"], response.text)


def test_a_member_saving_a_tier_is_refused_with_403_not_a_stack_trace(clean_secrets):
    """model_preferences_write refuses a non-admin by raising inside the
    INSERT; the route turns that into the 403 the page promises, rather than
    the 500 the first version produced."""
    member = TestClient(app)
    member.headers.update(auth(MEMBER))
    response = member.put(
        f"/api/workspaces/{WORKSPACE}/settings/models",
        json={"role": "strategy", "tier": "cheap"},
    )
    assert response.status_code == 403, response.text
    assert "owner or an admin" in response.text
