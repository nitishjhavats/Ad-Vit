"""Entitlement resolution and the access-mode ladder.

Entitlements are the authorization spine, not a billing ornament: the same
resolution decides whether a feature is sold AND whether an agent may spend
money. These tests pin the resolution order and the subscription-status
degradation the rest of the system depends on.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import (
    ORG_BROADMATE,
    ORG_RIVAL,
    OWNER,
    PRODUCT_MARKETING,
    SUPERADMIN,
    SUB_BROADMATE,
    acting_as,
    scalar_as,
)


def set_status(conn, status: str, org: str = ORG_BROADMATE) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update core.subscriptions set status = %s where org_id = %s",
            (status, org),
        )


def set_org_status(conn, status: str, org: str = ORG_BROADMATE) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update core.organisations set status = %s, suspension_reason = %s where id = %s",
            (status, "test" if status == "suspended" else None, org),
        )


# ---------------------------------------------------------------------------
# Resolution order: override -> plan -> definition default
# ---------------------------------------------------------------------------


def test_plan_grant_beats_the_feature_default(conn):
    """max_autonomy_level defaults to 1 (PRD D4 starts everyone at L1); the
    standard plan grants 3."""
    default_value = scalar_as(
        conn, OWNER, "select default_json from core.feature_definitions where key = 'max_autonomy_level'"
    )
    resolved = scalar_as(
        conn, OWNER, "select core.limit_int(%s, 'max_autonomy_level')", (ORG_BROADMATE,)
    )
    assert int(default_value) == 1
    assert resolved == 3


def test_override_beats_the_plan_grant(conn):
    with conn.cursor() as cur:
        cur.execute(
            "insert into core.entitlement_overrides (org_id, feature_key, value_json, reason) "
            "values (%s, 'max_autonomy_level', '4'::jsonb, 'design partner')",
            (ORG_BROADMATE,),
        )
    assert (
        scalar_as(conn, OWNER, "select core.limit_int(%s, 'max_autonomy_level')", (ORG_BROADMATE,))
        == 4
    )


def test_expired_override_is_ignored(conn):
    """A time-boxed grant must lapse on its own, not linger until someone
    remembers to delete it."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into core.entitlement_overrides "
            "(org_id, feature_key, value_json, reason, expires_at) "
            "values (%s, 'max_autonomy_level', '4'::jsonb, 'trial bump', now() - interval '1 day')",
            (ORG_BROADMATE,),
        )
    assert (
        scalar_as(conn, OWNER, "select core.limit_int(%s, 'max_autonomy_level')", (ORG_BROADMATE,))
        == 3
    )


def test_conservative_features_stay_off_on_the_standard_plan(conn):
    """competitor_intel is granted explicitly as false (PRD D5 defers the
    licensed provider to Phase 3), so it must not be usable."""
    assert (
        scalar_as(conn, OWNER, "select core.can(%s, 'feature.competitor_intel')", (ORG_BROADMATE,))
        is False
    )


def test_unknown_feature_resolves_to_null_not_true(conn):
    """Fail closed: a typo in a feature key must never read as 'permitted'."""
    assert (
        scalar_as(conn, OWNER, "select core.entitlement(%s, 'feature.nonexistent')", (ORG_BROADMATE,))
        is None
    )
    assert (
        scalar_as(conn, OWNER, "select core.can(%s, 'feature.nonexistent')", (ORG_BROADMATE,))
        is False
    )


def test_entitlements_are_scoped_per_organisation(conn):
    """Rival is on the same plan but must resolve independently."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into core.entitlement_overrides (org_id, feature_key, value_json, reason) "
            "values (%s, 'max_ad_accounts', '99'::jsonb, 'test')",
            (ORG_BROADMATE,),
        )
    assert scalar_as(conn, SUPERADMIN, "select core.limit_int(%s, 'max_ad_accounts')", (ORG_BROADMATE,)) == 99
    assert scalar_as(conn, SUPERADMIN, "select core.limit_int(%s, 'max_ad_accounts')", (ORG_RIVAL,)) == 3


# ---------------------------------------------------------------------------
# Access mode: PRD 18 - degrade to read-only rather than fail open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("trialing", "full"),
        ("active", "full"),
        ("pending_payment", "full"),
        ("past_due", "read_only"),
        ("grace", "read_only"),
        ("expired", "denied"),
        ("suspended", "denied"),
    ],
)
def test_access_mode_ladder(conn, status, expected):
    set_status(conn, status)
    assert scalar_as(conn, SUPERADMIN, "select core.access_mode(%s, t_advit.product_id())", (ORG_BROADMATE,)) == expected


def test_grace_keeps_reads_alive(conn):
    """A lapsed payment must not strand live campaigns: the owner can still
    see the account, they just cannot change it."""
    set_status(conn, "grace")
    assert scalar_as(conn, SUPERADMIN, "select core.access_mode(%s, t_advit.product_id())", (ORG_BROADMATE,)) == "read_only"
    # The limit still resolves - it is the write path that is closed.
    assert scalar_as(conn, SUPERADMIN, "select core.limit_int(%s, 'max_autonomy_level')", (ORG_BROADMATE,)) == 3


def test_suspended_organisation_is_denied_regardless_of_subscription(conn):
    """Superadmin suspension outranks a healthy subscription."""
    set_status(conn, "active")
    set_org_status(conn, "suspended")
    assert scalar_as(conn, SUPERADMIN, "select core.access_mode(%s, t_advit.product_id())", (ORG_BROADMATE,)) == "denied"


def test_organisation_without_a_subscription_is_denied(conn):
    with conn.cursor() as cur:
        cur.execute("update core.subscriptions set cancelled_at = now() where org_id = %s", (ORG_BROADMATE,))
    assert scalar_as(conn, SUPERADMIN, "select core.access_mode(%s, t_advit.product_id())", (ORG_BROADMATE,)) == "denied"


def test_can_is_false_once_access_is_denied(conn):
    """A granted feature on a dead subscription is still not usable."""
    set_status(conn, "active")
    assert scalar_as(conn, SUPERADMIN, "select core.can(%s, 'feature.experiments')", (ORG_BROADMATE,)) is True
    set_status(conn, "expired")
    assert scalar_as(conn, SUPERADMIN, "select core.can(%s, 'feature.experiments')", (ORG_BROADMATE,)) is False


# ---------------------------------------------------------------------------
# assert_entitled - raises so a caller cannot ignore a false return
# ---------------------------------------------------------------------------


def assert_entitled_raises(conn, feature: str, requested=None, org: str = ORG_BROADMATE) -> str:
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        with acting_as(conn, SUPERADMIN):
            with conn.cursor() as cur:
                cur.execute(
                    "select core.assert_entitled(%s, %s, %s)", (org, feature, requested)
                )
    conn.rollback()
    return exc.value.diag.message_hint or ""


def test_assert_entitled_passes_within_the_limit(conn):
    with acting_as(conn, SUPERADMIN):
        with conn.cursor() as cur:
            cur.execute("select core.assert_entitled(%s, 'max_ad_accounts', 3)", (ORG_BROADMATE,))


def test_assert_entitled_rejects_beyond_the_limit(conn):
    assert assert_entitled_raises(conn, "max_ad_accounts", 4) == "limit_exceeded"


def test_assert_entitled_rejects_a_disabled_feature(conn):
    assert assert_entitled_raises(conn, "feature.competitor_intel") == "feature_disabled"


def test_assert_entitled_rejects_an_unknown_feature(conn):
    """`feature_undefined`, not `feature_not_granted`.

    Both refuse, so nothing about the gate got weaker — the hint got more
    precise. "Not granted" says the feature exists and this organisation lacks
    it; "undefined" says there is no such feature. Since 20260911000015 the
    product is resolved from `core.feature_definitions.product_id`, so an
    undefined feature is now caught one step earlier: it cannot name a product,
    and therefore there is no subscription to ask about.

    It also matches `core.guard_entitlement_value`, which has always used
    `feature_undefined` for this same condition on the write side. The two sides
    of the same question now give the same answer.
    """
    assert assert_entitled_raises(conn, "feature.nonexistent") == "feature_undefined"


def test_assert_entitled_rejects_a_denied_subscription(conn):
    set_status(conn, "expired")
    assert assert_entitled_raises(conn, "feature.experiments") == "subscription_denied"


# ---------------------------------------------------------------------------
# Provenance view backing the superadmin feature-control screen
# ---------------------------------------------------------------------------


def test_org_entitlements_reports_provenance(conn):
    """All three resolution sources must be distinguishable in one read.

    The seeded plan grants every defined feature explicitly, which is the right
    posture but leaves the default branch unexercised - so this registers a
    feature the plan says nothing about in order to cover it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "insert into core.entitlement_overrides (org_id, feature_key, value_json, reason) "
            "values (%s, 'max_seats', '25'::jsonb, 'enterprise pilot')",
            (ORG_BROADMATE,),
        )
        cur.execute(
            "insert into core.feature_definitions "
            "(key, product_id, name, value_type, default_json) "
            "values ('feature.ungranted', %s, 'Ungranted', 'boolean', 'false'::jsonb)",
            (PRODUCT_MARKETING,),
        )

    with acting_as(conn, SUPERADMIN):
        with conn.cursor() as cur:
            cur.execute(
                "select feature_key, source from core.org_entitlements(%s)", (ORG_BROADMATE,)
            )
            source = dict(cur.fetchall())

    assert source["max_seats"] == "override"
    assert source["max_autonomy_level"] == "plan"
    assert source["feature.competitor_intel"] == "plan"   # granted, explicitly false
    assert source["feature.ungranted"] == "default"


def test_compliance_gate_is_on_by_default_and_not_sold(conn):
    """The compliance gate exists as a feature key so it is visible and
    auditable - never so it can be withheld as an upsell (PRD 13, 4.5)."""
    assert scalar_as(conn, OWNER, "select core.can(%s, 'feature.compliance_gate')", (ORG_BROADMATE,)) is True

    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from core.plans p "
            "join core.plan_features pf on pf.plan_id = p.id "
            "where pf.feature_key = 'feature.compliance_gate' "
            "  and pf.value_json = 'false'::jsonb"
        )
        assert cur.fetchone()[0] == 0
