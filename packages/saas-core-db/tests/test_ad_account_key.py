"""The ad account is a key, and how many of them an organisation may hold.

`t_advit.metrics_daily` carried (workspace_id, date, level, entity_id) and
nothing else identifying; `t_advit.campaigns` carried (workspace_id, meta_id).
Neither said which ad account the row came from. That is survivable for a
workspace with one connection and wrong for the shape this product sells — up to
three accounts per organisation — because "spend by account" then cannot be
computed at all, and per-account spend cannot be reconciled against the
account-level total, which is the one check that catches an ingestion that
silently dropped a campaign.

The second half is the limit itself. `core.feature_definitions` held the floor,
`core.plan_features` held the plan's three, `core.entitlement_overrides` held the
per-org exception and `core.assert_entitled` knew how to enforce a limit —
and nothing connected any of it to `meta_connections`, so the limit was
documentation.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import ORG_BROADMATE, SUPERADMIN, rows_as, scalar_as

WORKSPACE = "00000000-0000-4000-8000-000000000050"
CONNECTED_ACCOUNT = "1000000000000001"
WRITABLE_ACCOUNT = "1000000000000003"


def connect(cur, workspace: str, ad_account_id: str) -> None:
    cur.execute(
        """
        insert into t_advit.meta_connections
          (workspace_id, business_id, ad_account_id, token_ref, currency)
        values (%s::uuid, 'biz-1', %s, gen_random_uuid(), 'INR')
        """,
        (workspace, ad_account_id),
    )


def sibling_workspace(cur) -> str:
    """A second workspace in the same organisation.

    The limit is per ORGANISATION, so demonstrating that needs two workspaces -
    counting per workspace would let one organisation connect three accounts per
    workspace and as many workspaces as it liked.
    """
    cur.execute(
        """
        insert into t_advit.workspaces
          (org_id, name, industry_key, timezone, daily_cap_inr, monthly_cap_inr)
        values (%s::uuid, 'Second team', 'ayurveda', 'Asia/Kolkata', 1000, 25000)
        returning id::text
        """,
        (ORG_BROADMATE,),
    )
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def test_a_metric_cannot_name_an_account_the_workspace_has_not_connected(conn):
    """A composite foreign key rather than a CHECK, because
    meta_connections(workspace_id, ad_account_id) is already unique. That makes
    "this figure came from an account we actually connected" a property the
    database enforces rather than one the ingestion code remembers."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                """
                insert into t_advit.metrics_daily
                  (date, workspace_id, level, entity_id, ad_account_id, source, spend_inr)
                values (current_date, %s::uuid, 'campaign', 'c-1', '9999999999', 'meta', 10)
                """,
                (WORKSPACE,),
            )
    conn.rollback()


def test_an_account_level_row_whose_two_ids_disagree_is_refused(conn):
    """It would describe two different accounts at once, and whichever one a
    later query picked, half its answers would be wrong."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                insert into t_advit.metrics_daily
                  (date, workspace_id, level, entity_id, ad_account_id, source, spend_inr)
                values (current_date, %s::uuid, 'account', %s, %s, 'meta', 10)
                """,
                (WORKSPACE, WRITABLE_ACCOUNT, CONNECTED_ACCOUNT),
            )
    conn.rollback()


def test_a_campaign_level_row_for_a_connected_account_is_accepted(conn):
    """The counterpart. A constraint that refuses everything is not a
    constraint, it is an outage."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.metrics_daily
              (date, workspace_id, level, entity_id, ad_account_id, source, spend_inr)
            values (current_date, %s::uuid, 'campaign', 'c-ok', %s, 'meta', 10)
            """,
            (WORKSPACE, WRITABLE_ACCOUNT),
        )
    conn.rollback()


def test_a_connection_cannot_be_deleted_while_its_metrics_exist(conn):
    """RESTRICT, not CASCADE and not SET NULL.

    A meta_connections row is the record that this account was ever connected.
    Deleting it while metrics exist would either destroy the provenance of every
    figure derived from that account or leave rows that cannot say where they
    came from. Disconnecting is a soft operation - `write_enabled = false` - and
    this makes that the only available one until somebody deliberately decides
    what should happen to the history.
    """
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "delete from t_advit.meta_connections where ad_account_id = %s",
                (CONNECTED_ACCOUNT,),
            )
    conn.rollback()


def test_every_metrics_row_names_an_account(conn):
    """NOT NULL, asserted over the seeded fixture set as well as declared, so a
    seed that forgets the column fails here rather than at the first report."""
    assert scalar_as(
        conn, SUPERADMIN,
        "select count(*) from t_advit.metrics_daily where ad_account_id is null",
    ) == 0


# ---------------------------------------------------------------------------
# The limit
# ---------------------------------------------------------------------------


def test_the_organisation_may_not_connect_more_accounts_than_its_plan_allows(conn):
    """The seeded organisation is on the `standard` plan, which grants three,
    and already holds three."""
    with conn.cursor() as cur:
        cur.execute("select core.count_org_ad_accounts(%s)", (ORG_BROADMATE,))
        assert cur.fetchone()[0] == 3

        with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
            connect(cur, WORKSPACE, "9999999999")
    conn.rollback()
    assert exc.value.diag.message_hint == "limit_exceeded"


def test_the_limit_is_per_organisation_and_not_per_workspace(conn):
    """Otherwise one organisation connects three accounts per workspace, and
    creates as many workspaces as it likes."""
    with conn.cursor() as cur:
        second = sibling_workspace(cur)
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
            connect(cur, second, "8888888888")
    conn.rollback()
    assert exc.value.diag.message_hint == "limit_exceeded"


def test_sharing_one_account_with_a_second_team_is_not_a_second_account(conn):
    """A shared ad account managed by two teams is one account, and charging for
    it twice is a bug the customer notices. `count_org_ad_accounts` counts
    DISTINCT ids for exactly this."""
    with conn.cursor() as cur:
        second = sibling_workspace(cur)
        connect(cur, second, WRITABLE_ACCOUNT)
        cur.execute("select core.count_org_ad_accounts(%s)", (ORG_BROADMATE,))
        assert cur.fetchone()[0] == 3, "a shared account was counted twice"
    conn.rollback()


def test_raising_the_entitlement_lets_another_account_through(conn):
    """The superadmin's lever, end to end: an override on the organisation, and
    the next connection is accepted. If this did not work the limit would be a
    hard ceiling rather than a default."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into core.entitlement_overrides (org_id, feature_key, value_json, reason)
            values (%s::uuid, 'max_ad_accounts', '5'::jsonb, 'customer asked for five')
            on conflict (org_id, feature_key) do update set value_json = excluded.value_json
            """,
            (ORG_BROADMATE,),
        )
        connect(cur, WORKSPACE, "9999999999")
        cur.execute("select core.count_org_ad_accounts(%s)", (ORG_BROADMATE,))
        assert cur.fetchone()[0] == 4
    conn.rollback()


def test_an_unresolvable_limit_refuses_the_connection(conn):
    """The rule 20260910000004 established, reaching a new caller: an allowance
    that cannot be resolved to a number is not evidence of permission."""
    with conn.cursor() as cur:
        cur.execute(
            "alter table core.entitlement_overrides disable trigger entitlement_overrides_value_guard"
        )
        cur.execute(
            """
            insert into core.entitlement_overrides (org_id, feature_key, value_json, reason)
            values (%s::uuid, 'max_ad_accounts', '"lots"'::jsonb, 'pre-existing bad value')
            on conflict (org_id, feature_key) do update set value_json = excluded.value_json
            """,
            (ORG_BROADMATE,),
        )
        cur.execute(
            "alter table core.entitlement_overrides enable trigger entitlement_overrides_value_guard"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
            connect(cur, WORKSPACE, "9999999999")
    conn.rollback()
    assert exc.value.diag.message_hint == "limit_unresolvable"


# ---------------------------------------------------------------------------
# Who changed the allowance
# ---------------------------------------------------------------------------


def test_set_by_comes_from_the_session_and_not_from_the_payload(conn):
    """`set_by` is a uuid on a row that decides how much an organisation may
    spend and how autonomous its agents may be, and it was whatever the caller
    typed. The payload below names somebody else on purpose."""
    impostor = "00000000-0000-4000-8000-0000000000ff"
    stored = rows_as(
        conn, SUPERADMIN,
        """
        insert into core.entitlement_overrides
          (org_id, feature_key, value_json, reason, set_by)
        values (%s::uuid, 'max_ad_accounts', '5'::jsonb, 'five please', %s::uuid)
        on conflict (org_id, feature_key) do update set value_json = excluded.value_json
        returning set_by::text
        """,
        (ORG_BROADMATE, impostor),
    )
    conn.rollback()
    assert stored[0][0] == SUPERADMIN, "the override is signed by whoever the caller named"


def test_changing_an_entitlement_leaves_a_trail(conn):
    """"Who raised this account's cap, and when" was unanswerable: the change
    wrote no audit row at all."""
    rows_as(
        conn, SUPERADMIN,
        """
        insert into core.entitlement_overrides (org_id, feature_key, value_json, reason)
        values (%s::uuid, 'max_ad_accounts', '5'::jsonb, 'customer asked for five')
        on conflict (org_id, feature_key) do update set value_json = excluded.value_json
        returning id::text
        """,
        (ORG_BROADMATE,),
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            select event, actor_id::text, payload_json->>'feature_key',
                   payload_json->>'reason'
              from core.audit_log
             where event like 'entitlement.' || '%%' and org_id = %s
             order by at desc limit 1
            """,
            (ORG_BROADMATE,),
        )
        row = cur.fetchone()
    conn.rollback()

    assert row is not None, "the entitlement changed and nothing recorded it"
    event, actor, feature, reason = row
    assert event in ("entitlement.granted", "entitlement.changed")
    assert actor == SUPERADMIN
    assert feature == "max_ad_accounts"
    assert reason == "customer asked for five"
