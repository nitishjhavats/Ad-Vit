"""A NULL in one column must not disable a check about two others.

``business_truth_confirmed_within_total`` guarded itself on
``cancelled_orders is null``, a column its comparison does not need. So the
check only fired when the owner happened to report all three numbers, and
confirming more orders than were placed — which is impossible — was storable.

The consequence was not a wrong number. ``blended_daily.confirm_rate`` is
``numeric(6,5)`` and holds at most 9.99999, so a confirm rate of 10.0 overflows
it: a typo at 20:30 produced a scheduled job that dies, every day, on a row
nothing will clean up, taking the day's economics with it.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import rows_as, SUPERADMIN

WORKSPACE = "00000000-0000-4000-8000-000000000050"
DATE = "2026-08-15"


def insert_truth(conn, **cols):
    keys = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    with conn.cursor() as cur:
        cur.execute(
            f"insert into t_advit.business_truth (date, workspace_id, {keys}) "
            f"values (%s::date, %s, {marks})",
            (DATE, WORKSPACE, *cols.values()),
        )


# ---------------------------------------------------------------------------
# The finding
# ---------------------------------------------------------------------------


def test_confirmed_cannot_exceed_total_when_cancelled_is_unreported(conn):
    """The finding itself: 100 confirmed against 10 placed, accepted, because
    the cancellation count happened to be absent."""
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        insert_truth(conn, total_orders=10, confirmed_orders=100)
    conn.rollback()
    assert "confirmed_within_total" in str(exc.value)


def test_confirmed_plus_cancelled_still_cannot_exceed_total(conn):
    """The case the original constraint did catch must keep working."""
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_truth(conn, total_orders=42, confirmed_orders=40, cancelled_orders=5)
    conn.rollback()


def test_a_real_days_numbers_are_accepted(conn):
    """28 confirmed and 9 cancelled of 42 placed — the shape of an ordinary
    evening report. A constraint that rejects this is worse than none."""
    insert_truth(conn, total_orders=42, confirmed_orders=28, cancelled_orders=9)
    conn.rollback()


def test_an_evening_report_without_cancellations_is_accepted(conn):
    """Cancellations often are not known yet at 20:30. Requiring them would
    push the owner into inventing a number, which is worse than a NULL."""
    insert_truth(conn, total_orders=42, confirmed_orders=28)
    conn.rollback()


def test_the_boundary_is_inclusive(conn):
    """Every order placed was confirmed. Unusual, not impossible."""
    insert_truth(conn, total_orders=42, confirmed_orders=42, cancelled_orders=0)
    conn.rollback()


# ---------------------------------------------------------------------------
# The consequence the constraint exists to prevent
# ---------------------------------------------------------------------------


def test_the_daily_economics_job_survives_a_days_numbers(conn):
    """compute_blended_daily raised NumericValueOutOfRange on the row the
    constraint used to allow. With the row unstorable the overflow is
    unreachable, but the job is asserted here rather than assumed — it runs on
    a schedule where a raise is invisible until someone notices the dashboard
    has stopped moving."""
    insert_truth(
        conn,
        total_orders=42,
        confirmed_orders=28,
        cancelled_orders=9,
        rto_orders=4,
        delivered_orders=20,
        revenue_inr=61000,
        delivered_revenue_inr=48000,
    )
    with conn.cursor() as cur:
        cur.execute("select t_advit.compute_blended_daily(%s, %s::date)", (WORKSPACE, DATE))
        cur.execute(
            "select confirm_rate, rto_rate from t_advit.blended_daily "
            " where workspace_id = %s and date = %s::date",
            (WORKSPACE, DATE),
        )
        confirm_rate, rto_rate = cur.fetchone()
    assert 0 <= float(confirm_rate) <= 1
    assert 0 <= float(rto_rate) <= 1
    conn.rollback()


# ---------------------------------------------------------------------------
# The same class, one column over
# ---------------------------------------------------------------------------


def test_returns_cannot_exceed_confirmed_orders(conn):
    """rto_orders had no constraint at all, and compute_blended_daily divides
    by confirmed to get a rate. An RTO count above the confirmed count produces
    a rate above 1, which feeds contribution_margin_per_delivered_order and
    reports a margin that never existed."""
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        insert_truth(conn, confirmed_orders=28, rto_orders=40)
    conn.rollback()
    assert "rto_within_confirmed" in str(exc.value)


def test_returns_equal_to_confirmed_are_allowed(conn):
    """A day where every confirmed order came back. Grim, and real."""
    insert_truth(conn, total_orders=30, confirmed_orders=28, rto_orders=28)
    conn.rollback()
