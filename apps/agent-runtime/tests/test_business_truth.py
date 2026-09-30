"""Business-truth intake and the economics derived from it.

The worked example in PRD 12.4 (founder decision D6) is the centrepiece: a
business showing a 4.0 ROAS that is actually running at roughly 15% return on
ad spend once cancellation and RTO are applied. If this module cannot reproduce
that arithmetic, the product's central claim does not hold.
"""

from __future__ import annotations

import pytest

from app.agents.business_truth import (
    BusinessTruth,
    Severity,
    TrailingStats,
    UnitEconomics,
    compute_economics,
    intake,
    match_spend_to_truth,
    parse_natural_language,
    scaling_verdict,
    validate,
)


# ---------------------------------------------------------------------------
# Natural-language intake
# ---------------------------------------------------------------------------


def test_the_prd_example_message_parses():
    """'42 order aaye, 28 confirm, 9 cancel, 61k' is specified as valid input
    (PRD 12.1). The owner is on a phone at 20:30."""
    result = parse_natural_language("42 order aaye, 28 confirm, 9 cancel, 61k")

    assert result.truth.total_orders == 42
    assert result.truth.confirmed_orders == 28
    assert result.truth.cancelled_orders == 9
    assert result.truth.revenue_inr == 61_000
    assert result.accepted


def test_english_phrasing_parses():
    result = parse_natural_language(
        "35 orders today, 30 confirmed, 5 cancelled, 4 RTO, revenue 48000"
    )
    assert result.truth.total_orders == 35
    assert result.truth.confirmed_orders == 30
    assert result.truth.cancelled_orders == 5
    assert result.truth.rto_orders == 4
    assert result.truth.revenue_inr == 48_000


@pytest.mark.parametrize(
    "text,expected",
    [
        ("revenue 1.2 lakh", 120_000),
        ("61k revenue", 61_000),
        ("sale 75 hazaar", 75_000),
        ("revenue Rs 45,000", 45_000),
    ],
)
def test_indian_revenue_notations(text, expected):
    assert parse_natural_language(text).truth.revenue_inr == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # Indian grouping: last three digits, then pairs. This is how every
        # Indian bank statement, invoice and accounting package writes money,
        # so it is what an owner types at 20:30 without thinking about it.
        ("revenue 1,00,000", 100_000),
        ("sale 12,50,000", 1_250_000),
        ("sale 2,00,00,000", 20_000_000),
        ("revenue 1,23,45,678", 12_345_678),
        # Western grouping, because a spreadsheet export produces it.
        ("revenue 1,000,000", 1_000_000),
        ("revenue 45,000", 45_000),
        # Grouping and a decimal together.
        ("revenue 1,50,000.50", 150_000.5),
    ],
)
def test_grouped_thousands_are_read_at_full_value(text, expected):
    r"""Regression: the number pattern used to stop at the first group.

    `\d+(?:[.,]\d+)?` read "12,50,000" as "12,50", stripped the comma, and
    returned 1250 - a thousandfold understatement that would have made every
    downstream CAC, ROAS and scaling decision wrong in the same direction. It
    read "1,00,000" as 100, which the bare-small-number guard then discarded
    entirely, so a lakh day was recorded as no revenue at all.

    Both failures are silent: the owner sees their message accepted.
    """
    assert parse_natural_language(text).truth.revenue_inr == expected


def test_a_grouped_number_is_not_split_into_two_fields():
    """The 1000x bug had a second face: the digits the pattern did not claim
    were left in the string for the next pattern to find."""
    result = parse_natural_language("42 order aaye, revenue 12,50,000")
    assert result.truth.total_orders == 42
    assert result.truth.revenue_inr == 1_250_000


def test_lead_throughput_is_parsed():
    result = parse_natural_language("120 leads aaye, 80 contacted, 25 min response")
    assert result.truth.leads_received == 120
    assert result.truth.leads_contacted == 80
    assert result.truth.avg_response_min == 25


def test_unparseable_message_is_refused_with_a_question():
    """Silently accepting nothing would look identical to a zero day."""
    result = parse_natural_language("aaj kuch nahi hua yaar")
    assert not result.accepted
    assert result.questions


def test_a_bare_small_number_is_not_read_as_revenue():
    """'9 cancel' must not also claim 9 rupees of revenue."""
    result = parse_natural_language("9 cancel")
    assert result.truth.cancelled_orders == 9
    assert result.truth.revenue_inr is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_confirmed_plus_cancelled_cannot_exceed_total():
    issues = validate(BusinessTruth(total_orders=40, confirmed_orders=30, cancelled_orders=20))
    error = next(i for i in issues if i.severity is Severity.ERROR)
    assert "exceeds total orders" in error.message
    assert error.question


def test_delivered_cannot_exceed_confirmed():
    issues = validate(BusinessTruth(confirmed_orders=10, delivered_orders=12))
    assert any(i.severity is Severity.ERROR for i in issues)


def test_rto_cannot_exceed_confirmed():
    issues = validate(BusinessTruth(confirmed_orders=10, rto_orders=11))
    assert any(i.severity is Severity.ERROR for i in issues)


def test_uncontacted_leads_are_challenged_as_a_throughput_problem():
    """If contacted < received the bottleneck is the team, not the ads, and the
    OS must say so instead of optimising harder (PRD 12.1)."""
    issues = validate(BusinessTruth(leads_received=100, leads_contacted=50))
    issue = next(i for i in issues if i.field == "leads_contacted")
    assert issue.severity is Severity.CHALLENGE
    assert "throughput, not ad performance" in issue.message


def test_slow_response_is_challenged():
    issues = validate(BusinessTruth(avg_response_min=180))
    issue = next(i for i in issues if i.field == "avg_response_min")
    assert issue.severity is Severity.CHALLENGE


def test_outliers_are_challenged_not_silently_accepted():
    trailing = TrailingStats(days=30, mean_total_orders=40)
    issues = validate(BusinessTruth(total_orders=200), trailing)
    issue = next(i for i in issues if i.field == "total_orders")
    assert issue.severity is Severity.CHALLENGE
    assert issue.question


def test_outlier_checks_are_skipped_without_history():
    """A new account has no distribution to compare against, so the check is
    skipped rather than guessed."""
    assert validate(BusinessTruth(total_orders=200), TrailingStats(days=0)) == []


def test_a_challenge_does_not_reject_the_entry():
    result = intake({"leads_received": 100, "leads_contacted": 50})
    assert result.accepted is True
    assert result.questions


# ---------------------------------------------------------------------------
# PRD 12.4 - the worked example that justifies founder decision D6
# ---------------------------------------------------------------------------


def test_the_four_roas_business_is_actually_running_at_fifteen_percent():
    """Rs 1,00,000 spend, 250 orders at Rs 1,600 AOV reads as a 4.0 ROAS.
    Apply 22% cancellation and 26% RTO at 62% gross margin, and the business is
    making roughly Rs 15,000 - about 15% return on ad spend, not 300%."""
    truth = BusinessTruth(
        total_orders=250,
        confirmed_orders=195,     # 22% cancelled
        cancelled_orders=55,
        rto_orders=51,            # 26% of dispatched
        revenue_inr=400_000,
    )
    unit = UnitEconomics(
        aov_inr=1600,
        gross_margin_rate=0.62,
        fulfilment_cost_inr=100,
        return_freight_inr=80,
    )

    e = compute_economics(truth, unit, spend_inr=100_000)

    assert e.roas == pytest.approx(4.0, abs=0.01)
    assert e.delivered_orders == 144
    assert e.cancel_rate == pytest.approx(0.22, abs=0.01)
    assert e.rto_rate == pytest.approx(0.26, abs=0.01)

    # The number that actually matters, and it is nothing like 300%.
    assert 5_000 < e.contribution_margin_inr < 30_000
    assert e.true_return_on_ad_spend_pct < 30
    assert e.roas / (e.true_return_on_ad_spend_pct / 100) > 10


def test_return_freight_is_amortised_over_delivered_orders():
    """Failed deliveries load onto the orders that DID deliver, at
    rto/(1-rto), not at rto."""
    unit = UnitEconomics(aov_inr=1000, gross_margin_rate=0.5, return_freight_inr=100)

    no_rto = compute_economics(
        BusinessTruth(total_orders=100, confirmed_orders=100, rto_orders=0), unit, 1000
    )
    with_rto = compute_economics(
        BusinessTruth(total_orders=100, confirmed_orders=100, rto_orders=50), unit, 1000
    )

    assert no_rto.cm_per_delivered_order_inr == pytest.approx(500)
    # 50% RTO means one return for every delivery: a full Rs 100 of load.
    assert with_rto.cm_per_delivered_order_inr == pytest.approx(400)


def test_cac_ceiling_is_derived_not_accepted():
    """The ceiling comes from the account's own margin structure rather than a
    number the owner guessed (FR-039)."""
    unit = UnitEconomics(
        aov_inr=1299, gross_margin_rate=0.62, fulfilment_cost_inr=90,
        return_freight_inr=70, target_profit_share=0.30,
    )
    e = compute_economics(
        BusinessTruth(total_orders=100, confirmed_orders=78, rto_orders=20), unit, 50_000
    )
    expected_cm = 1299 * 0.62 - 90 - (20 / 78) / (1 - 20 / 78) * 70
    assert e.cm_per_delivered_order_inr == pytest.approx(expected_cm, abs=1)
    assert e.cac_ceiling_inr == pytest.approx(expected_cm * 0.70, abs=1)


def test_delivered_orders_are_projected_when_not_yet_reported():
    """Delivery lags by the courier cycle, so the number is projected rather
    than treated as known."""
    e = compute_economics(
        BusinessTruth(total_orders=100, confirmed_orders=80, rto_orders=20),
        UnitEconomics(aov_inr=1000, gross_margin_rate=0.5),
        spend_inr=10_000,
    )
    assert e.delivered_orders == 60


# ---------------------------------------------------------------------------
# Scaling
# ---------------------------------------------------------------------------


def test_scaling_is_refused_above_the_cac_ceiling():
    e = compute_economics(
        BusinessTruth(total_orders=50, confirmed_orders=40, rto_orders=10),
        UnitEconomics(aov_inr=1000, gross_margin_rate=0.4),
        spend_inr=40_000,
    )
    ok, why = scaling_verdict(e)
    assert ok is False
    assert "ceiling" in why


def test_scaling_is_refused_on_a_falling_confirm_rate():
    """A rising confirmed-order count with a falling confirm rate is not
    growth (PRD 10.3)."""
    e = compute_economics(
        BusinessTruth(total_orders=200, confirmed_orders=180, rto_orders=10),
        UnitEconomics(aov_inr=2000, gross_margin_rate=0.6),
        spend_inr=20_000,
    )
    ok, why = scaling_verdict(e, confirm_rate_trend=-0.05)
    assert ok is False
    assert "Confirm rate is falling" in why


def test_scaling_is_permitted_with_headroom_and_a_stable_confirm_rate():
    e = compute_economics(
        BusinessTruth(total_orders=200, confirmed_orders=180, rto_orders=10),
        UnitEconomics(aov_inr=2000, gross_margin_rate=0.6),
        spend_inr=20_000,
    )
    ok, why = scaling_verdict(e, confirm_rate_trend=0.01)
    assert ok is True
    assert "headroom" in why


def test_no_delivered_orders_means_no_verdict():
    e = compute_economics(
        BusinessTruth(total_orders=0, confirmed_orders=0),
        UnitEconomics(aov_inr=1000, gross_margin_rate=0.5),
        spend_inr=5_000,
    )
    ok, why = scaling_verdict(e)
    assert ok is False
    assert "no CAC to judge" in why


def test_economics_need_no_pixel_and_no_meta_signal():
    """The closed loop runs on owner-reported truth. An account with no dataset
    can still be optimised on this path - which is the whole thesis."""
    e = compute_economics(
        BusinessTruth(total_orders=42, confirmed_orders=28, cancelled_orders=9, rto_orders=6),
        UnitEconomics(aov_inr=1299, gross_margin_rate=0.62),
        spend_inr=5_000,
    )
    assert e.confirm_rate == pytest.approx(28 / 42, abs=0.01)
    assert e.blended_cac_inr is not None
    assert e.cac_ceiling_inr > 0


# ---------------------------------------------------------------------------
# Window matching
# ---------------------------------------------------------------------------


def _day(n, **kw):
    """One business-truth row. `n` is a day number, not a real date - the
    matcher only requires that dates be hashable and comparable."""
    row = {"date": n, "total_orders": 10, "confirmed_orders": 8, "cancelled_orders": 2,
           "rto_orders": 1, "delivered_orders": 7, "revenue_inr": 10_000.0}
    row.update(kw)
    return row


def test_spend_is_summed_over_only_the_days_that_have_truth():
    """The 30x CAC bug.

    The facts node read thirty days of spend and the single most recent day of
    business truth, then divided one by the other. On an account reporting
    daily that overstates blended CAC thirtyfold, and it errs towards telling a
    profitable account to stop scaling - the expensive direction to be wrong in.
    """
    truth_rows = [_day(30)]                                  # one reported day
    spend_rows = [{"date": d, "spend_inr": 1_000.0} for d in range(1, 31)]

    w = match_spend_to_truth(truth_rows, spend_rows)

    assert w.days == 1
    assert w.spend_inr == 1_000.0, "spend outside the reported day leaked in"
    assert w.spend_inr_outside_window == 29_000.0
    assert w.spend_days_without_truth == 29
    # The number that actually reaches a decision.
    assert w.spend_inr / w.truth.delivered_orders == pytest.approx(1_000 / 7)


def test_a_full_window_sums_both_sides():
    truth_rows = [_day(d) for d in range(1, 11)]
    spend_rows = [{"date": d, "spend_inr": 500.0} for d in range(1, 11)]

    w = match_spend_to_truth(truth_rows, spend_rows)

    assert w.days == 10
    assert w.spend_inr == 5_000.0
    assert w.truth.total_orders == 100
    assert w.truth.delivered_orders == 70
    assert w.truth.revenue_inr == 100_000.0
    assert (w.first_date, w.last_date) == (1, 10)
    assert w.spend_inr_outside_window == 0.0


def test_a_missing_day_is_excluded_rather_than_interpolated():
    """Gaps are marked, not smoothed (PRD 12.1). A day the owner did not report
    must not have its spend charged against the days they did."""
    truth_rows = [_day(1), _day(3)]                          # day 2 not reported
    spend_rows = [{"date": d, "spend_inr": 400.0} for d in (1, 2, 3)]

    w = match_spend_to_truth(truth_rows, spend_rows)

    assert w.days == 2
    assert w.spend_inr == 800.0
    assert w.spend_days_without_truth == 1
    assert w.truth.total_orders == 20


def test_truth_without_spend_is_counted_too():
    """The mirror gap: a day reported before ingestion caught up would otherwise
    depress CAC by adding orders that no spend paid for."""
    truth_rows = [_day(1), _day(2)]
    spend_rows = [{"date": 1, "spend_inr": 900.0}]

    w = match_spend_to_truth(truth_rows, spend_rows)

    assert w.days == 1
    assert w.truth_days_without_spend == 1
    assert w.truth.total_orders == 10


def test_no_overlap_yields_an_empty_window_not_a_wrong_number():
    w = match_spend_to_truth([_day(1)], [{"date": 9, "spend_inr": 700.0}])

    assert w.days == 0
    assert w.spend_inr == 0.0
    assert w.truth.as_dict() == {}
    assert w.spend_inr_outside_window == 700.0


def test_absent_fields_stay_absent():
    """A field nobody reported must stay None, so compute_economics can tell
    'nothing delivered' from 'delivery not reported'."""
    truth_rows = [_day(1, delivered_orders=None), _day(2, delivered_orders=None)]
    spend_rows = [{"date": d, "spend_inr": 100.0} for d in (1, 2)]

    assert match_spend_to_truth(truth_rows, spend_rows).truth.delivered_orders is None


def test_response_time_is_averaged_not_summed():
    truth_rows = [_day(1, avg_response_min=10), _day(2, avg_response_min=20)]
    spend_rows = [{"date": d, "spend_inr": 100.0} for d in (1, 2)]

    assert match_spend_to_truth(truth_rows, spend_rows).truth.avg_response_min == 15


def test_rows_without_a_date_are_ignored():
    """An undated row used to be impossible; it should not silently become today."""
    w = match_spend_to_truth([_day(1), {"total_orders": 999}],
                             [{"date": 1, "spend_inr": 100.0}, {"spend_inr": 5_000.0}])

    assert w.days == 1
    assert w.truth.total_orders == 10
    assert w.spend_inr == 100.0

# ---------------------------------------------------------------------------
# An unreported count is not a zero
# ---------------------------------------------------------------------------


def test_an_unreported_confirmation_count_is_not_a_zero_confirm_rate():
    """Every count used to be coerced with `or 0` before the rates were
    computed, so an unreported confirmation produced a confirm rate of 0.0 -
    which reads as "nobody confirmed", a catastrophe, when the truth is "nobody
    has told us yet". The SQL for the same day already returned NULL, so the two
    halves of the product disagreed about one number.
    """
    e = compute_economics(
        BusinessTruth(total_orders=100),
        UnitEconomics(aov_inr=1000, gross_margin_rate=0.6),
        spend_inr=5000,
    )
    assert e.confirm_rate is None
    assert e.cancel_rate is None
    assert e.rto_rate is None


def test_a_reported_zero_confirmation_count_is_kept():
    """The other side. A day where genuinely nothing confirmed is a fact, and a
    fix that erased it would be as wrong as the bug."""
    e = compute_economics(
        BusinessTruth(total_orders=100, confirmed_orders=0),
        UnitEconomics(aov_inr=1000, gross_margin_rate=0.6),
        spend_inr=5000,
    )
    assert e.confirm_rate == 0.0


def test_delivered_orders_are_not_projected_from_an_assumed_zero_rto():
    """Projecting delivery with an assumed 0% RTO overstates delivered orders,
    which understates CAC, which is the permissive direction."""
    e = compute_economics(
        BusinessTruth(total_orders=100, confirmed_orders=80),
        UnitEconomics(aov_inr=1000, gross_margin_rate=0.6),
        spend_inr=5000,
    )
    assert e.delivered_orders is None
    assert e.blended_cac_inr is None


def test_an_unknown_margin_yields_no_ceiling_and_no_verdict():
    """`or 0.5` used to stand in for an unknown margin. An invented 50% is not
    a conservative estimate - it can err in either direction - and it derives
    the CAC ceiling, which is what decides whether the owner is told they may
    scale. The honest answer is to refuse."""
    e = compute_economics(
        BusinessTruth(
            total_orders=100, confirmed_orders=80, rto_orders=8, delivered_orders=70,
            delivered_revenue_inr=70000,
        ),
        UnitEconomics(aov_inr=1000, gross_margin_rate=None),
        spend_inr=5000,
    )
    assert e.cac_ceiling_inr is None
    assert e.cm_per_delivered_order_inr is None
    assert e.contribution_margin_inr is None

    may_scale, why = scaling_verdict(e)
    assert may_scale is False
    assert "margin" in why.lower(), why


def test_a_genuine_zero_margin_is_not_rewritten():
    """`or 0.5` also rewrote a real margin of 0 into 0.5 - a loss-making product
    silently reported as a healthy one."""
    e = compute_economics(
        BusinessTruth(
            total_orders=100, confirmed_orders=80, rto_orders=0, delivered_orders=80,
            delivered_revenue_inr=80000,
        ),
        UnitEconomics(aov_inr=1000, gross_margin_rate=0.0),
        spend_inr=5000,
    )
    assert e.cm_per_delivered_order_inr == 0.0
    assert e.cac_ceiling_inr == 0.0
    may_scale, _ = scaling_verdict(e)
    assert may_scale is False, "a zero-margin product cannot profitably scale"
