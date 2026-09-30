"""An edge is a row about two nodes, so it is readable only when both are.

``kg_edges_select`` was one EXISTS correlated by ``n.id = kg_edges.src_id``.
``dst_id`` was named by no predicate anywhere in the policy, and the edge's own
``tier`` column was read by nothing. So visibility of ONE endpoint was taken as
authority over a row that names TWO — and in the destination direction the policy
did not guard weakly, it did not guard at all.

What leaks is the edge ROW, not the far node's content: ``kg_nodes_select`` still
hides the destination's own row. But the edge carries ``effect_size``,
``confidence``, ``evidence_n`` and a free-form ``props_json``, and the edges that
matter here are the provenance ones the promotion pipeline writes — industry
pattern → the account node that evidenced it. Being able to enumerate and count
those is exactly what ``industry_patterns.evidence_workspaces`` and the
three-workspace independence gate exist to prevent.

``kg_nodes`` and ``kg_edges`` are empty and nothing writes them yet. That makes
this a live hole in a boundary with nothing behind it, which is the right time to
close it rather than a reason to wait.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import ANALYST, MEMBER, OUTSIDER, OWNER, SUPERADMIN, rows_as

BROADMATE_WORKSPACE = "00000000-0000-4000-8000-000000000050"
RIVAL_WORKSPACE = "00000000-0000-4000-8000-000000000051"

N_IND = "00000000-0000-4000-8000-00000000e001"     # industry tier, no workspace
N_GLOBAL = "00000000-0000-4000-8000-00000000e002"  # global tier, no workspace
N_BROAD = "00000000-0000-4000-8000-00000000e003"   # account tier, Broadmate
N_BROAD2 = "00000000-0000-4000-8000-00000000e004"  # account tier, Broadmate
N_RIVAL = "00000000-0000-4000-8000-00000000e005"   # account tier, Rival

# src, dst, edge tier
EDGES = {
    "E1": (N_IND, N_RIVAL, "industry"),    # shared source -> foreign account
    "E2": (N_BROAD, N_RIVAL, "account"),   # my account -> foreign account
    "E4": (N_BROAD, N_BROAD2, "account"),  # account-internal
    "E5": (N_IND, N_GLOBAL, "industry"),   # shared -> shared
    "E6": (N_BROAD, N_IND, "account"),     # my account -> industry
    "E8": (N_IND, N_GLOBAL, "account"),    # account-tier edge with NO account endpoint
}


@pytest.fixture
def graph(conn):
    """Nodes and edges inside the rolled-back transaction, written as the owner
    of the tables so the fixtures themselves are not subject to the policy under
    test."""
    with conn.cursor() as cur:
        for node_id, tier, workspace in (
            (N_IND, "industry", None),
            (N_GLOBAL, "global", None),
            (N_BROAD, "account", BROADMATE_WORKSPACE),
            (N_BROAD2, "account", BROADMATE_WORKSPACE),
            (N_RIVAL, "account", RIVAL_WORKSPACE),
        ):
            cur.execute(
                """insert into t_advit.kg_nodes (id, tier, workspace_id, node_type, props_json)
                   values (%s, %s::t_advit.knowledge_tier, %s, 'probe', %s::jsonb)""",
                (node_id, tier, workspace, '{"label": "probe node"}'),
            )

        # E8 is the edge the trigger now refuses, so it goes in with the trigger
        # disabled - the READ policy has to be correct on its own, not only
        # because the write side happens to be closed.
        cur.execute("alter table t_advit.kg_edges disable trigger kg_edges_tier_guard")
        for name, (src, dst, tier) in EDGES.items():
            cur.execute(
                """insert into t_advit.kg_edges
                     (id, src_id, dst_id, edge_type, tier, effect_size, confidence, evidence_n)
                   values (gen_random_uuid(), %s, %s, %s, %s::t_advit.knowledge_tier,
                           0.42, 0.91, 17)""",
                (src, dst, name, tier),
            )
        cur.execute("alter table t_advit.kg_edges enable trigger kg_edges_tier_guard")
    yield
    conn.rollback()


def visible(conn, user) -> set[str]:
    return {
        row[0]
        for row in rows_as(conn, user, "select edge_type from t_advit.kg_edges", ())
    }


# ---------------------------------------------------------------------------
# The invariant, stated directly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user", [MEMBER, ANALYST, OUTSIDER, SUPERADMIN])
def test_no_visible_edge_points_at_an_invisible_node(conn, graph, user):
    """The finding, as the property rather than as a list of cases.

    Symmetric on purpose: it would also catch someone inverting the policy to
    guard the destination and forget the source. Before the fix this failed for
    the member (two edges) and for the analyst (one).
    """
    orphans = rows_as(
        conn,
        user,
        """
        select e.edge_type
          from t_advit.kg_edges e
         where not exists (select 1 from t_advit.kg_nodes n where n.id = e.src_id)
            or not exists (select 1 from t_advit.kg_nodes n where n.id = e.dst_id)
        """,
        (),
    )
    assert orphans == [], f"{user} sees edges into nodes it cannot read: {orphans}"


def test_a_shared_source_no_longer_exposes_a_foreign_account_destination(conn, graph):
    """Exposure (a). These are the provenance edges — industry pattern →the
    account that evidenced it — so being able to read them is being able to count
    and track the foreign evidence set behind an industry pattern."""
    assert "E1" not in visible(conn, MEMBER)
    assert "E1" not in visible(conn, ANALYST)


def test_my_own_node_linked_to_a_foreign_one_is_not_readable(conn, graph):
    """Exposure (b). The reader learns their node is linked to a node they cannot
    see, with the edge type, the effect size and the confidence."""
    assert "E2" not in visible(conn, MEMBER)


def test_an_account_edge_between_two_shared_nodes_belongs_to_nobody(conn, graph):
    """The hole the two-endpoint fix alone leaves open, and the reason there is a
    third conjunct.

    Both endpoints are shared-tier, so both EXISTS arms are satisfied and the row
    would go to the entire customer base. With no account endpoint and no
    workspace_id on the row, the guard has nothing to compare against — and was
    permitting, which is this repository's recurring defect in its purest form.
    """
    assert "E8" not in visible(conn, MEMBER)
    assert "E8" not in visible(conn, ANALYST)
    assert "E8" not in visible(conn, OUTSIDER)


# ---------------------------------------------------------------------------
# ...and the traffic that must survive
# ---------------------------------------------------------------------------


def test_account_internal_edges_still_work(conn, graph):
    """Over-tightening is the other way to break this. The ledger row's own
    suggestion — give kg_edges a workspace_id and scope on it — cannot work:
    kg_nodes_tier_scoping requires workspace_id IS NULL for every industry and
    global node, so a shared-to-shared edge has no workspace to carry, and
    scoping on one would make the entire shared tier unreadable. Cross-tenant
    shared learning is the product."""
    seen = visible(conn, MEMBER)
    assert "E4" in seen, "an account-internal edge was closed"
    assert "E6" in seen, "my-node-to-industry was closed"


def test_shared_tier_edges_stay_readable_by_everyone(conn, graph):
    """Including an organisation member with no workspace grant, who is exactly
    who the global and industry tiers exist for."""
    assert "E5" in visible(conn, ANALYST)


def test_the_owner_of_the_far_node_still_reads_the_edge(conn, graph):
    """The counterpart of E1 and E2: the rival tenant may read edges into their
    own node. If they could not, the fix would have closed the row rather than
    scoped it."""
    seen = visible(conn, OUTSIDER)
    assert "E1" in seen
    assert "E2" not in seen, "the rival can see an edge from a Broadmate node"


def test_a_superadmin_sees_every_edge(conn, graph):
    """core.is_superadmin() was hoisted out of the source EXISTS. With two
    subqueries it has to cover both arms, or the support console silently loses
    edges."""
    assert visible(conn, SUPERADMIN) == set(EDGES)


# ---------------------------------------------------------------------------
# The write side
# ---------------------------------------------------------------------------


def test_an_account_edge_touching_no_account_node_cannot_be_written(conn, graph):
    """kg_nodes has carried a tier-scoping check all along; kg_edges had a `tier`
    column with nothing constraining it. Since the policy now refuses to show
    such a row to anyone, being able to write one only accumulates rows nobody
    can read."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            cur.execute(
                """insert into t_advit.kg_edges
                     (id, src_id, dst_id, edge_type, tier)
                   values (gen_random_uuid(), %s, %s, 'probe', 'account')""",
                (N_IND, N_GLOBAL),
            )
    conn.rollback()
    assert exc.value.diag.message_hint == "edge_tier_unscoped"


def test_an_account_edge_with_an_account_endpoint_is_still_writable(conn, graph):
    with conn.cursor() as cur:
        cur.execute(
            """insert into t_advit.kg_edges
                 (id, src_id, dst_id, edge_type, tier)
               values (gen_random_uuid(), %s, %s, 'probe-ok', 'account')""",
            (N_BROAD, N_IND),
        )
    conn.rollback()


def test_the_policy_uses_no_negative_destination_guard(conn):
    """Crude, and worth it, because this is the failure the repository keeps
    re-committing.

    The natural phrasing for a destination guard is "exclude the ones I cannot
    see" — ``and not exists (... and not is_workspace_member(...))``. kg_nodes
    RLS has already removed the foreign node from that subquery, so it matches
    zero rows, NOT EXISTS is vacuously true, and the edge is permitted. Measured:
    that phrasing produced a visible set byte-identical to the broken policy's.
    It is longer, it looks stricter, and it denies nothing.
    """
    qual = rows_as(
        conn,
        SUPERADMIN,
        "select qual from pg_policies "
        " where schemaname = 't_advit' and tablename = 'kg_edges'"
        "   and policyname = 'kg_edges_select'",
        (),
    )[0][0]
    assert "NOT EXISTS" not in qual.upper(), (
        "kg_edges_select contains a negative existence guard. Under RLS the rows "
        "you are trying to exclude are already gone from the subquery, so NOT "
        "EXISTS is vacuously true and the policy permits."
    )
