"""An entitlement that cannot be resolved must refuse, not authorise.

core.assert_entitled guarded its limit comparison with
``jsonb_typeof(v_value) = 'number'``, so a value that was not a number skipped
the check entirely and the function returned normally — which means authorised.
Nothing validated what could be stored either, so a superadmin typing ``"three"``
where ``3`` belongs was accepted silently.

Reproduced against the running database before the fix: an override of
``'"three"'`` on ``max_ad_accounts`` let ``assert_entitled(org, 'max_ad_accounts',
99)`` pass. A malformed limit granted more than a well-formed one.

The asymmetry made it worse rather than better. ``core.limit_int`` on the same
value raised ``22P02``, so one typo broke every read that touched the feature —
loudly, on every request — while the authorisation path failed open in silence.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import ORG_BROADMATE, OWNER, SUPERADMIN, rows_as, scalar_as


def override(conn, feature: str, value: str) -> None:
    """Write an entitlement override as the superadmin, who is the only role the
    policy admits."""
    with conn.cursor() as cur:
        cur.execute("select set_config('request.jwt.claims', %s, true)",
                    ('{"role":"authenticated","sub":"%s"}' % SUPERADMIN,))
        cur.execute("set local role authenticated")
        cur.execute(
            """
            insert into core.entitlement_overrides (org_id, feature_key, value_json, reason)
            values (%s, %s, %s::jsonb, 'fail-closed regression test')
            on conflict (org_id, feature_key) do update set value_json = excluded.value_json
            """,
            (ORG_BROADMATE, feature, value),
        )
        cur.execute("reset role")


# ---------------------------------------------------------------------------
# The write can no longer happen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,hint",
    [
        ('"three"', "value_type_mismatch"),
        ("true", "value_type_mismatch"),
        ("2.5", "value_not_integer"),
        ("-1", "value_out_of_range"),
    ],
)
def test_a_malformed_limit_cannot_be_stored(conn, value, hint):
    """core.feature_definitions.value_type always declared what each feature
    holds; nothing ever checked a written value against it."""
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        override(conn, "max_ad_accounts", value)
    conn.rollback()
    assert exc.value.diag.message_hint == hint


def test_a_well_formed_limit_is_still_accepted(conn):
    """The guard must not break the legitimate path — this is the mechanism the
    superadmin uses to give one organisation more ad accounts than its plan."""
    override(conn, "max_ad_accounts", "7")
    assert scalar_as(
        conn, SUPERADMIN, "select core.limit_int(%s, 'max_ad_accounts')", (ORG_BROADMATE,)
    ) == 7
    conn.rollback()


def test_an_undefined_feature_is_refused(conn):
    """An override naming a feature that does not exist resolves to nothing at
    read time, so it would silently never apply."""
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        override(conn, "max_unicorns", "3")
    conn.rollback()
    assert exc.value.diag.message_hint == "feature_undefined"


def test_the_plan_catalogue_is_guarded_too(conn):
    """A typo in core.plan_features is worse than one in an override: it applies
    to every organisation on the plan rather than to one."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                insert into core.plan_features (plan_id, feature_key, value_json)
                values ('00000000-0000-4000-8000-000000000030', 'max_seats', '"lots"'::jsonb)
                on conflict (plan_id, feature_key) do update set value_json = excluded.value_json
                """
            )
    conn.rollback()


# ---------------------------------------------------------------------------
# And if one is already stored, authorisation refuses rather than permitting
# ---------------------------------------------------------------------------


def _force_malformed(conn) -> None:
    """Bypass the trigger to simulate a value written before it existed. The
    fix has two halves and this exercises the second one on its own."""
    with conn.cursor() as cur:
        cur.execute("alter table core.entitlement_overrides disable trigger entitlement_overrides_value_guard")
        cur.execute(
            """
            insert into core.entitlement_overrides (org_id, feature_key, value_json, reason)
            values (%s, 'max_ad_accounts', '"three"'::jsonb, 'pre-existing malformed value')
            on conflict (org_id, feature_key) do update set value_json = excluded.value_json
            """,
            (ORG_BROADMATE,),
        )
        cur.execute("alter table core.entitlement_overrides enable trigger entitlement_overrides_value_guard")


def test_an_unresolvable_limit_refuses_the_request(conn):
    """The finding itself. 99 ad accounts against a limit of "three" was
    authorised, because the limit could not be parsed and the check was skipped."""
    _force_malformed(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        rows_as(
            conn, OWNER,
            "select core.assert_entitled(%s, 'max_ad_accounts', 99)", (ORG_BROADMATE,),
        )
    conn.rollback()
    assert exc.value.diag.message_hint == "limit_unresolvable"


def test_an_unresolvable_limit_refuses_even_a_small_request(conn):
    """Not a threshold question. One account is within any sane limit, but the
    limit is unknown — and an unknown allowance is not evidence of permission."""
    _force_malformed(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        rows_as(
            conn, OWNER,
            "select core.assert_entitled(%s, 'max_ad_accounts', 1)", (ORG_BROADMATE,),
        )
    conn.rollback()
    assert exc.value.diag.message_hint == "limit_unresolvable"


def test_the_read_path_degrades_instead_of_crashing(conn):
    """core.limit_int raised 22P02 on the same value, putting a hard failure
    inside every request that resolved the feature. NULL is the honest answer:
    an unknown allowance, which t_advit.effective_autonomy already floors to
    L0 via coalesce."""
    _force_malformed(conn)
    assert scalar_as(
        conn, OWNER, "select core.limit_int(%s, 'max_ad_accounts')", (ORG_BROADMATE,)
    ) is None
    conn.rollback()


def test_an_unresolvable_autonomy_ceiling_floors_the_workspace(conn):
    """The end-to-end consequence, and the reason NULL is safe: a broken
    entitlement must drop the workspace to L0 rather than leaving it at
    whatever the workspace asked for."""
    with conn.cursor() as cur:
        cur.execute("alter table core.entitlement_overrides disable trigger entitlement_overrides_value_guard")
        cur.execute(
            """
            insert into core.entitlement_overrides (org_id, feature_key, value_json, reason)
            values (%s, 'max_autonomy_level', '"four"'::jsonb, 'pre-existing malformed value')
            on conflict (org_id, feature_key) do update set value_json = excluded.value_json
            """,
            (ORG_BROADMATE,),
        )
        cur.execute("alter table core.entitlement_overrides enable trigger entitlement_overrides_value_guard")
        cur.execute(
            "update t_advit.workspaces set autonomy_level = 3 where org_id = %s", (ORG_BROADMATE,)
        )
        cur.execute(
            "select t_advit.effective_autonomy(id) from t_advit.workspaces where org_id = %s",
            (ORG_BROADMATE,),
        )
        assert cur.fetchone()[0] == 0, "an unresolvable ceiling must not leave autonomy standing"
    conn.rollback()


def test_a_granted_limit_still_authorises_within_it(conn):
    """The fix must not turn a working limit into a refusal."""
    override(conn, "max_ad_accounts", "5")
    rows_as(conn, OWNER, "select core.assert_entitled(%s, 'max_ad_accounts', 5)", (ORG_BROADMATE,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        rows_as(conn, OWNER, "select core.assert_entitled(%s, 'max_ad_accounts', 6)", (ORG_BROADMATE,))
    conn.rollback()
    assert exc.value.diag.message_hint == "limit_exceeded"
