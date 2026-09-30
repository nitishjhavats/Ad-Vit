"""T1 -> T2 promotion must actually be gated.

Regression suite for an audit finding. PRD 5.1 requires a pattern to have been
observed in at least 3 distinct workspaces under at least 2 distinct owners
before it becomes industry truth. The constraint meant to enforce that was:

    check (status <> 'active' or array_length(evidence_workspaces, 1) >= 3)

with `evidence_workspaces text[] not null default '{}'`. `array_length` of an
empty array is NULL, `NULL >= 3` is NULL, and a CHECK constraint passes on NULL.
So the default made the gate unconditionally true - and `status` defaulted to
'active', so an unevidenced row was also the path of least resistance.

This is the highest-stakes machinery in the product. A wrong T2 record does not
mislead one account; it misleads every account in that industry at once, with
the system's full confidence behind it.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from conftest import SUPERADMIN

AYURVEDA = "ayurveda"


def insert_pattern(conn, *, status, workspaces, owners=(), approved=True, evidence_n=0):
    """Insert one industry pattern, returning its id. Raises on constraint
    violation, which is what most of these tests are asserting."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.industry_patterns
              (industry_key, pattern_type, statement, status,
               evidence_workspaces, evidence_owners, evidence_n, approved_by, approved_at)
            values (%s, 'hook', %s, %s::t_advit.learning_status,
                    %s, %s, %s, %s, case when %s then now() end)
            returning id
            """,
            (
                AYURVEDA,
                f"test pattern {uuid.uuid4().hex[:8]}",
                status,
                list(workspaces),
                list(owners),
                evidence_n,
                SUPERADMIN if approved else None,
                approved,
            ),
        )
        return cur.fetchone()[0]


W = [f"w{n}" for n in range(1, 6)]
O = [f"o{n}" for n in range(1, 4)]


# ---------------------------------------------------------------------------
# The bug
# ---------------------------------------------------------------------------


def test_an_unevidenced_pattern_cannot_be_active(conn):
    """The finding, exactly as it was: empty arrays and status 'active'."""
    with pytest.raises(psycopg.errors.CheckViolation, match="independence"):
        insert_pattern(conn, status="active", workspaces=[], owners=[])
    conn.rollback()


def test_the_empty_array_null_trick_is_closed(conn):
    """array_length('{}', 1) is NULL and NULL >= 3 is NULL, which a CHECK
    constraint accepts. coalesce(..., 0) is what closes it."""
    assert conn.execute(
        "select t_advit.distinct_count('{}'::text[]), t_advit.distinct_count(null)"
    ).fetchone() == (0, 0)


def test_two_workspaces_are_not_enough(conn):
    with pytest.raises(psycopg.errors.CheckViolation, match="independence"):
        insert_pattern(conn, status="active", workspaces=W[:2], owners=O[:2])
    conn.rollback()


def test_repeating_one_workspace_does_not_count_as_three(conn):
    """array_length counted entries, not distinct ones, so {w1, w1, w1} would
    have satisfied 'three distinct workspaces'."""
    with pytest.raises(psycopg.errors.CheckViolation, match="independence"):
        insert_pattern(conn, status="active", workspaces=["w1", "w1", "w1"], owners=O[:2])
    conn.rollback()


def test_three_workspaces_under_one_owner_are_not_independent(conn):
    """The half of the gate the original constraint never expressed at all.

    Three workspaces belonging to one agency running one playbook are one
    observation repeated, not three - and that is the single most likely way a
    coincidence gets promoted to an industry law.
    """
    with pytest.raises(psycopg.errors.CheckViolation, match="independence"):
        insert_pattern(conn, status="active", workspaces=W[:3], owners=["o1"])
    conn.rollback()


def test_the_gate_opens_when_it_is_genuinely_satisfied(conn):
    """The fix must not make promotion impossible, only earned."""
    assert insert_pattern(conn, status="active", workspaces=W[:3], owners=O[:2])
    conn.rollback()


# ---------------------------------------------------------------------------
# Everything the gate must not block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["contested", "historical"])
def test_a_non_active_pattern_needs_no_evidence(conn, status):
    """A contested or retired record is not being asserted as true, so it is
    not gated. Blocking it would stop contradictions from being recorded, which
    is the mechanism that resolves a bad pattern."""
    assert insert_pattern(conn, status=status, workspaces=[], owners=[], approved=False)
    conn.rollback()


def test_promotion_still_requires_a_steward(conn):
    """The independence gate is additional to the approval gate, not a
    replacement for it (PRD 5.1: a human approves every promotion)."""
    with pytest.raises(psycopg.errors.CheckViolation, match="requires_approval"):
        insert_pattern(conn, status="active", workspaces=W[:3], owners=O[:2], approved=False)
    conn.rollback()


def test_status_has_no_default(conn):
    """'active' was the column default, so the most dangerous state was the one
    you got by not thinking about it. Promotion is a decision and must be
    stated."""
    default = conn.execute(
        """
        select column_default from information_schema.columns
         where table_schema = 't_advit' and table_name = 'industry_patterns'
           and column_name = 'status'
        """
    ).fetchone()[0]
    assert default is None


def test_the_denormalised_count_cannot_understate_the_evidence(conn):
    """evidence_n is a convenience column that can drift from the array it
    describes. The array is authoritative."""
    with pytest.raises(psycopg.errors.CheckViolation, match="evidence_n_matches"):
        insert_pattern(conn, status="active", workspaces=W[:3], owners=O[:2], evidence_n=1)
    conn.rollback()
