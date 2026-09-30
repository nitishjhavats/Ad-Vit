"""An unknown input must not become a number.

``t_advit.compute_blended_daily`` is the product's economics engine — the
dashboard, the facts block the model is told is authoritative, and the scaling
verdict all read what it writes. It had five places where a missing input became
a value instead of an absence.

The sharpest was reproduced against the running database with ``metrics_daily``
empty, which it is, because that table still has no writer::

    30 delivered orders, no ingested spend  ->  blended_cac_inr = 0.0000

A CAC of zero makes acquisition look free, so every scaling verdict becomes
maximally permissive. And the same function already knew how to say *unknown* —
``mer`` correctly returned NULL when spend was zero. It knew, and did not, for
CAC.

The distinction these tests exist to protect: ``sum()`` over zero rows is NULL,
over rows it is a number. A workspace that genuinely spent nothing on a day it
has metrics for is not the same as a workspace with no metrics at all, and the
schema must hold both.
"""

from __future__ import annotations

import pytest

WORKSPACE = "00000000-0000-4000-8000-000000000050"
# Seeded in 03_advit_workspace.sql; see the comment in spend() below.
CONNECTED_ACCOUNT = "1000000000000001"
DATE = "2026-08-20"


@pytest.fixture
def clean(conn):
    """A day with no truth, no metrics and no catalogue, so each test states its
    own preconditions rather than inheriting the seed's."""
    with conn.cursor() as cur:
        for table in ("blended_daily", "business_truth", "metrics_daily"):
            cur.execute(
                f"delete from t_advit.{table} where workspace_id = %s and date = %s::date",
                (WORKSPACE, DATE),
            )
        cur.execute("delete from t_advit.catalog_products where workspace_id = %s", (WORKSPACE,))
    yield
    conn.rollback()


def truth(conn, **cols):
    keys = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    with conn.cursor() as cur:
        cur.execute(
            f"insert into t_advit.business_truth (date, workspace_id, {keys}) "
            f"values (%s::date, %s, {marks})",
            (DATE, WORKSPACE, *cols.values()),
        )


def spend(conn, amount):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.metrics_daily
              -- `source` has no DEFAULT, on purpose: a writer that forgets it
              -- would otherwise get 'meta' - the one claim that must always be
              -- made deliberately. These rows are a fixture and say so.
              (date, workspace_id, level, entity_id, ad_account_id, source, spend_inr)
            -- A real connected account, not 'act_1'. metrics_daily now carries a
            -- composite FK to meta_connections and a CHECK that an
            -- account-level row's two identifiers agree, so an invented id is
            -- refused - which is the point of the constraint and the reason
            -- this fixture had to name a real one.
            values (%s::date, %s, 'account', %s, %s, 'fixture', %s)
            """,
            (DATE, WORKSPACE, CONNECTED_ACCOUNT, CONNECTED_ACCOUNT, amount),
        )


def product(conn, *, margin=None, fulfilment=None, freight=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.catalog_products
              (workspace_id, sku, name, price_inr, margin_rate,
               fulfilment_cost_inr, return_freight_inr)
            values (%s, 'SKU-1', 'Test product', 1000, %s, %s, %s)
            """,
            (WORKSPACE, margin, fulfilment, freight),
        )


def compute(conn):
    with conn.cursor() as cur:
        cur.execute("select t_advit.compute_blended_daily(%s, %s::date)", (WORKSPACE, DATE))
        cur.execute(
            "select blended_cac_inr, contribution_margin_inr, confirm_rate, rto_rate, "
            "       delivered_aov_inr, mer, gaps "
            "  from t_advit.blended_daily where workspace_id = %s and date = %s::date",
            (WORKSPACE, DATE),
        )
        row = cur.fetchone()
    return dict(zip(
        ("cac", "margin", "confirm_rate", "rto_rate", "aov", "mer", "gaps"), row
    ))


# ---------------------------------------------------------------------------
# The finding
# ---------------------------------------------------------------------------


def test_an_un_ingested_spend_does_not_become_a_zero_cac(conn, clean):
    """The reproduction. Thirty delivered orders and nothing ingested used to
    write a CAC of exactly zero — as a fact, into the row the dashboard reads
    and the model is told not to recompute."""
    truth(conn, total_orders=50, confirmed_orders=40, cancelled_orders=5,
          delivered_orders=30, revenue_inr=90000, delivered_revenue_inr=72000)
    r = compute(conn)
    assert r["cac"] is None, "acquisition cost is unknown, not free"
    assert "spend_not_ingested" in r["gaps"]


def test_a_measured_zero_spend_day_still_reports_zero(conn, clean):
    """The other side, and the reason the fix is `sum()` without a coalesce
    rather than a NULL check: a workspace that genuinely spent nothing on a day
    it *has* metrics for did measure zero, and must not be told otherwise."""
    truth(conn, total_orders=10, confirmed_orders=8, delivered_orders=5,
          delivered_revenue_inr=5000)
    spend(conn, 0)
    r = compute(conn)
    assert r["cac"] == 0, "a measured zero is a fact"
    assert "spend_not_ingested" not in r["gaps"]


def test_contribution_margin_is_withheld_when_spend_is_unknown(conn, clean):
    """Subtracting an unknown spend from a known margin yields a number that
    looks like profit and is not one."""
    truth(conn, total_orders=50, confirmed_orders=40, cancelled_orders=5,
          rto_orders=4, delivered_orders=30, delivered_revenue_inr=72000)
    product(conn, margin=0.6, fulfilment=40, freight=120)
    r = compute(conn)
    assert r["margin"] is None
    assert "spend_not_ingested" in r["gaps"]


# ---------------------------------------------------------------------------
# The other four coalesces
# ---------------------------------------------------------------------------


def test_unreported_returns_do_not_become_a_zero_rto_rate(conn, clean):
    """A 0% RTO flatters the contribution margin, which is exactly the direction
    that invites scaling."""
    truth(conn, total_orders=50, confirmed_orders=40, delivered_orders=30,
          delivered_revenue_inr=72000)
    spend(conn, 10000)
    r = compute(conn)
    assert r["rto_rate"] is None
    assert "rto_not_reported" in r["gaps"]


def test_a_reported_zero_rto_is_kept(conn, clean):
    truth(conn, total_orders=50, confirmed_orders=40, rto_orders=0,
          delivered_orders=30, delivered_revenue_inr=72000)
    spend(conn, 10000)
    r = compute(conn)
    assert r["rto_rate"] == 0
    assert "rto_not_reported" not in r["gaps"]


def test_an_unknown_margin_is_refused_not_assumed(conn, clean):
    """It used to become 50%. That is not an upper bound — it can err either
    way — and it feeds the CAC ceiling, which decides whether the account is
    told it may scale."""
    truth(conn, total_orders=50, confirmed_orders=40, rto_orders=4,
          delivered_orders=30, delivered_revenue_inr=72000)
    spend(conn, 10000)
    product(conn, margin=None, fulfilment=40, freight=120)
    r = compute(conn)
    assert r["margin"] is None
    assert "margin_rate_unknown" in r["gaps"]


def test_unreported_delivered_revenue_does_not_become_a_zero_mer(conn, clean):
    truth(conn, total_orders=50, confirmed_orders=40, rto_orders=4, delivered_orders=30)
    spend(conn, 10000)
    r = compute(conn)
    assert r["mer"] is None
    assert r["aov"] is None


def test_missing_costs_still_compute_a_margin_but_name_it_an_upper_bound(conn, clean):
    """Unlike the others, zero was not a wrong reading of the data here — the
    columns did not exist. A margin missing two cost lines is still
    directionally useful, so it is computed and labelled rather than withheld."""
    truth(conn, total_orders=50, confirmed_orders=40, rto_orders=10,
          delivered_orders=30, delivered_revenue_inr=72000)
    spend(conn, 10000)
    product(conn, margin=0.6, fulfilment=None, freight=None)
    r = compute(conn)
    assert r["margin"] is not None
    assert "costs_incomplete_margin_is_an_upper_bound" in r["gaps"]


def test_supplying_the_costs_lowers_the_margin_and_drops_the_caveat(conn, clean):
    """Quantifies why the caveat matters: at a realistic RTO the two cost lines
    are worth a double-digit percentage of margin per delivered order."""
    truth(conn, total_orders=50, confirmed_orders=40, rto_orders=10,
          delivered_orders=30, delivered_revenue_inr=72000)
    spend(conn, 10000)
    product(conn, margin=0.6, fulfilment=None, freight=None)
    without = compute(conn)["margin"]

    with conn.cursor() as cur:
        cur.execute(
            "update t_advit.catalog_products set fulfilment_cost_inr = 40, "
            "       return_freight_inr = 120 where workspace_id = %s",
            (WORKSPACE,),
        )
    r = compute(conn)
    assert r["margin"] < without, "real costs must reduce the reported margin"
    assert "costs_incomplete_margin_is_an_upper_bound" not in r["gaps"]


# ---------------------------------------------------------------------------
# The happy path still works
# ---------------------------------------------------------------------------


def test_a_fully_reported_day_carries_no_gaps(conn, clean):
    """A guard that always complains is as useless as one that never does."""
    truth(conn, total_orders=50, confirmed_orders=40, cancelled_orders=5,
          rto_orders=4, delivered_orders=30, revenue_inr=90000,
          delivered_revenue_inr=72000)
    spend(conn, 12000)
    product(conn, margin=0.6, fulfilment=40, freight=120)
    r = compute(conn)
    assert r["gaps"] == []
    assert r["cac"] == 400  # 12000 / 30
    assert r["margin"] is not None
    assert 0 < float(r["confirm_rate"]) <= 1
    assert 0 <= float(r["rto_rate"]) <= 1


def test_a_day_with_no_business_truth_says_so(conn, clean):
    """The pre-existing behaviour, now named rather than implied by an
    all-NULL row."""
    spend(conn, 5000)
    r = compute(conn)
    assert r["cac"] is None
    assert "business_truth_not_reported" in r["gaps"]
