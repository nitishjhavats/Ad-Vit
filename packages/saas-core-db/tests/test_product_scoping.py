"""An entitlement answer must name the product it is about.

``core.access_mode(p_org)`` resolved ``from core.subscriptions where s.org_id =
p_org and s.cancelled_at is null limit 1``. The missing product predicate is only
half of it: the function had **no product parameter**, so it was structurally
incapable of answering "is this organisation's ad-vit subscription live" while
``core.subscriptions`` is keyed per ``(org_id, product_id)``.

``limit 1`` with no ORDER BY then picked by physical heap order under a Seq
Scan — and UPDATE moves a row to the end of the heap, so a routine status change
on the ad-vit subscription handed the answer to the neighbouring product. Not a
stable coin flip; one that re-flips when you touch it.

None of it is reachable while ``core.products`` holds one row, which is why it
survived two audits. The severity is about **when**, not whether — and the point
of doing the caller edits now is that the day somebody inserts a second product
is a non-event rather than an outage.

Every test here creates that second product inside the transaction and rolls it
back, so the suite still runs against a single-product database.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import ORG_BROADMATE, OWNER, SUPERADMIN, rows_as, scalar_as

WORKSPACE = "00000000-0000-4000-8000-000000000050"

OTHER_PRODUCT = "00000000-0000-4000-8000-0000000000ff"
OTHER_PLAN = "00000000-0000-4000-8000-0000000000fe"
OTHER_SUB = "00000000-0000-4000-8000-0000000000fd"


@pytest.fixture
def second_product(conn):
    """A neighbouring product on the same control plane, as the shared cluster
    already has: 20260911000007 says out loud that this database hosts an
    unrelated HRMS."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into core.products (id, key, name) values (%s, 'hrms', 'HRMS')",
            (OTHER_PRODUCT,),
        )
        cur.execute(
            """insert into core.plans (id, product_id, key, name, price_inr, billing_period)
               values (%s, %s, 'standard', 'HRMS Standard', 999, 'monthly')""",
            (OTHER_PLAN, OTHER_PRODUCT),
        )
        cur.execute(
            """insert into core.subscriptions
                 (id, org_id, product_id, plan_id, status,
                  current_period_start, current_period_end)
               values (%s, %s, %s, %s, 'active', now(), now() + interval '30 days')""",
            (OTHER_SUB, ORG_BROADMATE, OTHER_PRODUCT, OTHER_PLAN),
        )
    yield OTHER_PRODUCT
    conn.rollback()


def cancel_advit(conn):
    with conn.cursor() as cur:
        cur.execute(
            """update core.subscriptions set status = 'expired', cancelled_at = now()
                where org_id = %s
                  and product_id = (select id from core.products where key = 'advit')""",
            (ORG_BROADMATE,),
        )


# ---------------------------------------------------------------------------
# The leak
# ---------------------------------------------------------------------------


def test_a_live_neighbour_does_not_keep_a_cancelled_account_writable(conn, second_product):
    """The finding, in the direction that costs the customer money.

    ``pipeline.py`` gates every Meta-mutating tool on exactly
    ``access_mode != 'full'``, so this returning 'full' means a churned customer
    keeps full write access and real spend continues on automation they
    cancelled — with nothing anywhere showing the account as lapsed, because the
    dashboard reads the same function.
    """
    cancel_advit(conn)
    mode = scalar_as(
        conn,
        SUPERADMIN,
        "select core.access_mode(%s, t_advit.product_id())::text",
        (ORG_BROADMATE,),
    )
    assert mode == "denied", f"a cancelled ad-vit subscription resolved to {mode!r}"


def test_the_agents_drop_to_l0_when_this_product_lapses(conn, second_product):
    cancel_advit(conn)
    autonomy = scalar_as(
        conn, SUPERADMIN, "select t_advit.effective_autonomy(%s::uuid)", (WORKSPACE,)
    )
    assert autonomy == 0


def test_the_scheduler_stops_waking_for_a_lapsed_account(conn, second_product):
    """``workspaces_due`` exists partly to stop this: its own comment says
    running the agents to produce proposals nobody may act on spends model budget
    on an account that is not paying. It was reading the neighbour's
    subscription."""
    cancel_advit(conn)

    # Called on the plain connection rather than through `rows_as`, which wears
    # `authenticated`. workspaces_due is revoked from `authenticated` on purpose
    # - it is the unattended scheduler's function and no tenant has any business
    # asking which accounts are due - so asking it as a tenant tests the grant,
    # not the product predicate.
    with conn.cursor() as cur:
        cur.execute(
            "select workspace_id::text from t_advit.workspaces_due('daily_brief', 0, 0)"
        )
        due = {row[0] for row in cur.fetchall()}

    assert WORKSPACE not in due


def test_a_neighbours_plan_cannot_raise_this_products_limit(conn, second_product):
    """``core.feature_definitions.product_id`` existed and was populated all
    along; the plan-grant arm simply never consulted it. Measured before the fix:
    ``max_ad_accounts`` resolved to 99 from another product's plan while ad-vit's
    own plan granted 3, and ``assert_entitled(…, 50)`` passed."""
    with conn.cursor() as cur:
        # The trigger refuses this outright now, which is the belt. Disable it to
        # exercise the braces - the resolver must be correct on its own terms.
        cur.execute(
            "alter table core.plan_features disable trigger plan_features_product_guard"
        )
        cur.execute(
            """insert into core.plan_features (plan_id, feature_key, value_json)
               values (%s, 'max_ad_accounts', '99'::jsonb)""",
            (OTHER_PLAN,),
        )
        cur.execute(
            "alter table core.plan_features enable trigger plan_features_product_guard"
        )

    value = scalar_as(
        conn, SUPERADMIN, "select core.entitlement(%s, 'max_ad_accounts')::text", (ORG_BROADMATE,)
    )
    assert value != "99", "another product's plan raised this product's ad-account cap"


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def test_the_one_argument_form_refuses_rather_than_choosing(conn):
    """Replaced in place rather than dropped. Every caller lives in a
    dollar-quoted function body, so PostgreSQL tracks no dependency: a DROP
    succeeds under the default RESTRICT, the migration applies cleanly, the reset
    comes up green, and the first real request fails with 42883."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        rows_as(conn, SUPERADMIN, "select core.access_mode(%s)", (ORG_BROADMATE,))
    conn.rollback()
    assert exc.value.diag.message_hint == "product_required"


def test_a_null_product_refuses_rather_than_picking_one(conn):
    """The house rule, at the one place this function can run out of things to
    compare against. ``t_advit.product_id()`` returns NULL if ad-vit is not an
    active product, and that must not resolve to somebody else's subscription."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        rows_as(conn, SUPERADMIN, "select core.access_mode(%s, null)", (ORG_BROADMATE,))
    conn.rollback()
    assert exc.value.diag.message_hint == "product_required"


def test_a_subscription_cannot_name_another_products_plan(conn, second_product):
    """Promoted from "optional hardening" to mandatory by measurement: with the
    resolver fixed but this constraint absent, re-pointing the ad-vit
    subscription at a neighbouring plan still returned that plan's limits. The
    predicate in the resolver does not close it on its own."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                """update core.subscriptions set plan_id = %s
                    where org_id = %s
                      and product_id = (select id from core.products where key = 'advit')""",
                (OTHER_PLAN, ORG_BROADMATE),
            )
    conn.rollback()


def test_a_plan_cannot_grant_another_products_feature(conn, second_product):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            cur.execute(
                """insert into core.plan_features (plan_id, feature_key, value_json)
                   values (%s, 'max_ad_accounts', '99'::jsonb)""",
                (OTHER_PLAN,),
            )
    conn.rollback()
    assert exc.value.diag.message_hint == "feature_product_mismatch"


# ---------------------------------------------------------------------------
# ...and everything that must keep working on a single-product database
# ---------------------------------------------------------------------------


def test_the_ordinary_case_is_unchanged(conn):
    assert (
        scalar_as(
            conn,
            SUPERADMIN,
            "select core.access_mode(%s, t_advit.product_id())::text",
            (ORG_BROADMATE,),
        )
        == "full"
    )
    assert scalar_as(conn, SUPERADMIN, "select t_advit.effective_autonomy(%s::uuid)", (WORKSPACE,)) == 1


def test_can_returns_false_for_an_undefined_feature_rather_than_raising(conn):
    """``core.can`` had to become plpgsql for this. SQL does not promise to
    evaluate the arms of an ``and`` left to right, so the undefined-feature case
    would have reached ``core.access_mode`` with a NULL product and RAISED where
    the soft form must return a boolean."""
    assert scalar_as(conn, OWNER, "select core.can(%s, 'feature.nonexistent')", (ORG_BROADMATE,)) is False


def test_can_still_answers_a_real_feature(conn):
    result = scalar_as(conn, OWNER, "select core.can(%s, 'feature.compliance_gate')", (ORG_BROADMATE,))
    assert result is True


def test_org_entitlements_still_reports_plan_provenance(conn):
    """The superadmin screen and the tenant's own plan page read this. A
    mis-granted value reported as source='plan' makes a wrong number look
    authoritative, so the same three-way join went into its EXISTS branch."""
    rows = rows_as(
        conn,
        SUPERADMIN,
        "select feature_key, source from core.org_entitlements(%s) order by feature_key",
        (ORG_BROADMATE,),
    )
    sources = {key: source for key, source in rows}
    assert sources, "org_entitlements returned nothing"
    assert set(sources.values()) <= {"override", "plan", "default"}
    assert "plan" in sources.values(), "no feature resolves from the plan any more"
