"""Business-truth intake and the economics derived from it (PRD 12.1, 12.4).

This is the product's heartbeat. Meta reports leads and purchases; an Indian COD
business lives or dies on confirmed, delivered, non-RTO sales - a number Meta
never sees. Without this intake the OS is another dashboard.

Two deliberate properties:

* No model touches the arithmetic. Parsing is regex, validation is comparison,
  and every rupee and ratio is computed here or in SQL (PRD 17.7). This removes
  an entire class of confident numerical error and is a cost decision as much as
  a correctness one.

* Gaps are marked, never interpolated. A learning derived from a window with
  missing ground truth carries reduced confidence, so an absent day must stay
  visibly absent rather than being smoothed away.

Note what this does NOT require: a pixel, a dataset, or any Meta signal at all.
The owner is the source. An account with broken tracking can still run the
closed loop on this path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from enum import Enum
from collections.abc import Mapping, Sequence
from typing import Any


class Severity(str, Enum):
    ERROR = "error"      # refuses the entry
    CHALLENGE = "challenge"  # accepted, but questioned back to the owner
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class Issue:
    severity: Severity
    field: str
    message: str
    question: str | None = None


@dataclass(slots=True)
class BusinessTruth:
    total_orders: int | None = None
    confirmed_orders: int | None = None
    cancelled_orders: int | None = None
    rto_orders: int | None = None
    delivered_orders: int | None = None
    revenue_inr: float | None = None
    delivered_revenue_inr: float | None = None
    leads_received: int | None = None
    leads_contacted: int | None = None
    avg_response_min: int | None = None
    sales_feedback: str | None = None
    business_issues: str | None = None

    def as_dict(self) -> dict[str, Any]:
        # slots=True dataclasses carry no __dict__, so read the field list.
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) is not None
        }


@dataclass(slots=True)
class IntakeResult:
    truth: BusinessTruth
    issues: list[Issue] = field(default_factory=list)
    parsed_from: str = "structured"

    @property
    def accepted(self) -> bool:
        return not any(i.severity is Severity.ERROR for i in self.issues)

    @property
    def questions(self) -> list[str]:
        return [i.question for i in self.issues if i.question]


# ---------------------------------------------------------------------------
# Natural-language intake
#
# "42 order aaye, 28 confirm, 9 cancel, 61k" is a valid input (PRD 12.1). The
# owner is on a phone at 20:30; whichever form is lowest-friction for them wins,
# and a structured card is not always it.
# ---------------------------------------------------------------------------

# Indian grouping puts the last three digits together and then pairs off, so
# 12,50,000 is twelve lakh fifty thousand. A spreadsheet export uses Western
# grouping instead, so both have to be read. The grouped alternative is tried
# first: `\d+` would otherwise claim only the leading digits and leave the rest
# of the number in the string for another pattern to misread as a second field.
#
# A comma is never a decimal separator here. Treating it as one is what made
# "12,50,000" parse as 1250.
_NUM = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"

# Ordered: the first pattern that claims a number wins, so more specific
# phrasings are listed before the generic ones.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("delivered_orders", re.compile(rf"{_NUM}\s*(?:deliver(?:ed|y)?|pahunch\w*|mila)", re.I)),
    ("rto_orders", re.compile(rf"{_NUM}\s*(?:rto|return(?:ed)?|wapas|vapas)", re.I)),
    ("cancelled_orders", re.compile(rf"{_NUM}\s*(?:cancel\w*|reject\w*|mana)", re.I)),
    ("confirmed_orders", re.compile(rf"{_NUM}\s*(?:confirm\w*|pakka|ok\b)", re.I)),
    ("leads_contacted", re.compile(rf"{_NUM}\s*(?:contact\w*|call(?:ed)?\s*kiy\w*)", re.I)),
    ("leads_received", re.compile(rf"{_NUM}\s*(?:leads?|enquir\w*|puchtach)", re.I)),
    ("total_orders", re.compile(rf"{_NUM}\s*(?:total\s*)?(?:orders?|aaye|aye)", re.I)),
    ("avg_response_min", re.compile(rf"{_NUM}\s*(?:min(?:ute)?s?)\s*(?:response|mein|me)?", re.I)),
]

# Revenue: 61k / 61,000 / Rs 61000 / 1.2 lakh / 61 hazaar
_REVENUE = re.compile(
    r"(?:rs\.?|inr|₹)?\s*"
    rf"{_NUM}\s*"
    r"(k\b|thousand|hazaar|hazar|lakh|lac|l\b|cr\b|crore)?"
    r"(?=[^%]*$|.*(?:revenue|sale|rupay|rupees|bika))",
    re.I,
)

_MULTIPLIER = {
    "k": 1_000, "thousand": 1_000, "hazaar": 1_000, "hazar": 1_000,
    "lakh": 100_000, "lac": 100_000, "l": 100_000,
    "cr": 10_000_000, "crore": 10_000_000,
}


def parse_natural_language(text: str) -> IntakeResult:
    """Extract a day's numbers from free text, in English or Hinglish."""
    truth = BusinessTruth()
    issues: list[Issue] = []
    consumed: list[tuple[int, int]] = []

    def overlaps(span: tuple[int, int]) -> bool:
        return any(not (span[1] <= s or span[0] >= e) for s, e in consumed)

    for attr, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            if overlaps(match.span()):
                continue
            raw = match.group(1).replace(",", "")
            try:
                value = int(float(raw))
            except ValueError:
                continue
            if getattr(truth, attr) is None:
                setattr(truth, attr, value)
                consumed.append(match.span())
                break

    # Revenue last: it is the most ambiguous token, so it only claims numbers
    # nothing else took.
    for match in _REVENUE.finditer(text):
        if overlaps(match.span()):
            continue
        raw = match.group(1).replace(",", "")
        suffix = (match.group(2) or "").lower()
        try:
            amount = float(raw) * _MULTIPLIER.get(suffix, 1)
        except ValueError:
            continue
        # A bare small number with no unit is far more likely to be a count
        # that the field patterns missed than a rupee figure.
        if not suffix and amount < 1000:
            continue
        truth.revenue_inr = amount
        consumed.append(match.span())
        break

    if not truth.as_dict():
        issues.append(
            Issue(
                Severity.ERROR,
                "input",
                "No numbers could be read from that message.",
                question="Could you send it as: orders, confirmed, cancelled, revenue?",
            )
        )

    return IntakeResult(truth=truth, issues=issues, parsed_from="natural_language")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrailingStats:
    """Trailing 30-day distribution used for range checks. Absent for a new
    account, in which case outlier checks are skipped rather than guessed."""

    days: int = 0
    mean_total_orders: float | None = None
    mean_confirm_rate: float | None = None


def validate(truth: BusinessTruth, trailing: TrailingStats | None = None) -> list[Issue]:
    issues: list[Issue] = []

    for name in (
        "total_orders", "confirmed_orders", "cancelled_orders", "rto_orders",
        "delivered_orders", "leads_received", "leads_contacted",
    ):
        value = getattr(truth, name)
        if value is not None and value < 0:
            issues.append(Issue(Severity.ERROR, name, f"{name} cannot be negative"))

    if truth.revenue_inr is not None and truth.revenue_inr < 0:
        issues.append(Issue(Severity.ERROR, "revenue_inr", "revenue cannot be negative"))

    # Internal consistency (PRD 12.1, FR-037).
    t, c, x = truth.total_orders, truth.confirmed_orders, truth.cancelled_orders
    if None not in (t, c, x) and (c + x) > t:
        issues.append(
            Issue(
                Severity.ERROR,
                "confirmed_orders",
                f"confirmed ({c}) + cancelled ({x}) = {c + x} exceeds total orders ({t})",
                question="Which of those three is right?",
            )
        )

    if truth.confirmed_orders is not None and truth.delivered_orders is not None:
        if truth.delivered_orders > truth.confirmed_orders:
            issues.append(
                Issue(
                    Severity.ERROR,
                    "delivered_orders",
                    f"delivered ({truth.delivered_orders}) exceeds confirmed "
                    f"({truth.confirmed_orders})",
                )
            )

    if truth.confirmed_orders is not None and truth.rto_orders is not None:
        if truth.rto_orders > truth.confirmed_orders:
            issues.append(
                Issue(
                    Severity.ERROR,
                    "rto_orders",
                    f"RTO ({truth.rto_orders}) exceeds confirmed orders "
                    f"({truth.confirmed_orders})",
                )
            )

    # The bottleneck may be the sales team rather than the ads, and the OS must
    # say so instead of optimising harder (PRD 12.1).
    if truth.leads_received is not None and truth.leads_contacted is not None:
        if truth.leads_contacted > truth.leads_received:
            issues.append(
                Issue(
                    Severity.ERROR,
                    "leads_contacted",
                    f"contacted ({truth.leads_contacted}) exceeds received "
                    f"({truth.leads_received})",
                )
            )
        elif truth.leads_received > 0:
            uncontacted = truth.leads_received - truth.leads_contacted
            if uncontacted / truth.leads_received > 0.2:
                issues.append(
                    Issue(
                        Severity.CHALLENGE,
                        "leads_contacted",
                        f"{uncontacted} of {truth.leads_received} leads were not contacted. "
                        "The bottleneck looks like throughput, not ad performance - "
                        "buying more leads would not help.",
                        question="Was the team short-staffed, or are leads arriving faster "
                        "than they can be called?",
                    )
                )

    # Contact within 5 minutes converts roughly 9x better than after an hour.
    if truth.avg_response_min is not None and truth.avg_response_min > 60:
        issues.append(
            Issue(
                Severity.CHALLENGE,
                "avg_response_min",
                f"average response time is {truth.avg_response_min} minutes. Contact within "
                "five minutes converts far better; past an hour most of the value is gone.",
                question="Is that a staffing gap or a routing delay?",
            )
        )

    # Outliers are challenged, not silently accepted (FR-037).
    if trailing and trailing.days >= 7 and trailing.mean_total_orders:
        if truth.total_orders is not None and trailing.mean_total_orders > 0:
            ratio = truth.total_orders / trailing.mean_total_orders
            if ratio > 3 or ratio < 0.33:
                direction = "above" if ratio > 1 else "below"
                issues.append(
                    Issue(
                        Severity.CHALLENGE,
                        "total_orders",
                        f"{truth.total_orders} orders is well {direction} the trailing "
                        f"average of {trailing.mean_total_orders:.0f}.",
                        question="Is that right, or was something different about the day?",
                    )
                )

    return issues


def intake(
    payload: dict[str, Any] | str, trailing: TrailingStats | None = None
) -> IntakeResult:
    """Accept either a structured card or a natural-language message."""
    if isinstance(payload, str):
        result = parse_natural_language(payload)
    else:
        known = {f for f in BusinessTruth.__annotations__}
        result = IntakeResult(
            truth=BusinessTruth(**{k: v for k, v in payload.items() if k in known}),
            parsed_from="structured",
        )

    result.issues.extend(validate(result.truth, trailing))
    return result


# ---------------------------------------------------------------------------
# Economics (PRD 12.4, founder decision D6)
#
# ROAS is the wrong north star for COD Ayurveda. A 4.0 ROAS business with 22%
# cancellation and 26% RTO is running at roughly 15% return on ad spend, not
# 300%. The OS optimises RTO-adjusted contribution margin and derives the CAC
# ceiling from the account's own margin structure rather than accepting a
# target the owner guessed.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitEconomics:
    aov_inr: float
    # None means the workspace has not told us its margin. Not 0.5, which was
    # the previous default and is a fabrication in either direction - it feeds
    # the CAC ceiling, which decides whether the account is told it may scale.
    gross_margin_rate: float | None
    fulfilment_cost_inr: float = 0.0
    return_freight_inr: float = 0.0
    target_profit_share: float = 0.0


@dataclass(frozen=True, slots=True)
class Economics:
    spend_inr: float
    total_orders: int
    confirmed_orders: int
    delivered_orders: int
    cancel_rate: float | None
    rto_rate: float | None
    confirm_rate: float | None
    delivered_revenue_inr: float | None
    cm_per_delivered_order_inr: float | None
    contribution_margin_inr: float | None
    blended_cac_inr: float | None
    cac_ceiling_inr: float | None
    roas: float | None
    true_return_on_ad_spend_pct: float | None

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# ---------------------------------------------------------------------------
# Matching the window
#
# Economics only mean anything when the numerator and the denominator cover the
# same dates. Dividing a month of spend by a single day of delivered orders
# reports a blended CAC up to thirty times the real one - and it errs in the
# direction that talks a healthy account out of scaling, which is the expensive
# direction to be wrong in.
#
# Business truth arrives from a person at 20:30 and will have gaps; spend
# arrives from Meta and generally will not. So the only honest window is the
# intersection - the dates that have both - and the days on either side of it
# are reported as gaps rather than filled in (PRD 12.1).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MatchedWindow:
    """Business truth and spend summed over exactly the same dates."""

    truth: BusinessTruth
    spend_inr: float
    days: int
    first_date: Any = None
    last_date: Any = None
    truth_days_without_spend: int = 0
    spend_days_without_truth: int = 0
    spend_inr_outside_window: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "first_date": str(self.first_date) if self.first_date is not None else None,
            "last_date": str(self.last_date) if self.last_date is not None else None,
            "spend_inr": self.spend_inr,
            "truth_days_without_spend": self.truth_days_without_spend,
            "spend_days_without_truth": self.spend_days_without_truth,
            "spend_inr_outside_window": self.spend_inr_outside_window,
        }


_SUMMED_COUNTS = (
    "total_orders",
    "confirmed_orders",
    "cancelled_orders",
    "rto_orders",
    "delivered_orders",
    "leads_received",
    "leads_contacted",
)
_SUMMED_MONEY = ("revenue_inr", "delivered_revenue_inr")


def match_spend_to_truth(
    truth_rows: "Sequence[Mapping[str, Any]]",
    spend_rows: "Sequence[Mapping[str, Any]]",
) -> MatchedWindow:
    """Sum both sides over the dates that have both, and count what was left out.

    Rows are whatever the driver returns - anything mapping-like with a ``date``
    key. Rows without a date are ignored rather than silently dated today.
    """
    spend_by_date: dict[Any, float] = {}
    for row in spend_rows:
        d = row.get("date")
        if d is None:
            continue
        spend_by_date[d] = spend_by_date.get(d, 0.0) + float(row.get("spend_inr") or 0)

    truth_by_date: dict[Any, Mapping[str, Any]] = {
        row["date"]: row for row in truth_rows if row.get("date") is not None
    }

    shared = sorted(set(truth_by_date) & set(spend_by_date))

    truth = BusinessTruth()
    if shared:
        for name in _SUMMED_COUNTS:
            values = [truth_by_date[d].get(name) for d in shared]
            present = [int(v) for v in values if v is not None]
            if present:
                setattr(truth, name, sum(present))
        for name in _SUMMED_MONEY:
            values = [truth_by_date[d].get(name) for d in shared]
            present = [float(v) for v in values if v is not None]
            if present:
                setattr(truth, name, sum(present))

        # An unweighted mean across the days that reported one. Weighting by
        # lead volume would be defensible too, but response time is a property
        # of the shift rather than of the lead, and a plain mean is the one a
        # reader can reproduce from the daily numbers they were shown.
        responses = [
            int(truth_by_date[d]["avg_response_min"])
            for d in shared
            if truth_by_date[d].get("avg_response_min") is not None
        ]
        if responses:
            truth.avg_response_min = int(round(sum(responses) / len(responses)))

    spend_in_window = sum(spend_by_date[d] for d in shared)
    return MatchedWindow(
        truth=truth,
        spend_inr=spend_in_window,
        days=len(shared),
        first_date=shared[0] if shared else None,
        last_date=shared[-1] if shared else None,
        truth_days_without_spend=len(set(truth_by_date) - set(spend_by_date)),
        spend_days_without_truth=len(set(spend_by_date) - set(truth_by_date)),
        spend_inr_outside_window=sum(spend_by_date.values()) - spend_in_window,
    )


def compute_economics(
    truth: BusinessTruth, unit: UnitEconomics, spend_inr: float
) -> Economics:
    total = truth.total_orders
    confirmed = truth.confirmed_orders
    cancelled = truth.cancelled_orders
    rto = truth.rto_orders

    def rate(numerator: int | None, denominator: int | None) -> float | None:
        """None whenever either side is unreported.

        These used to be computed after coercing every count with `or 0`, so an
        unreported confirmation count produced a confirm rate of 0.0 - which
        reads as "nobody confirmed", a catastrophe, when the truth is "nobody
        has told us yet". The SQL for the same day already returned NULL, so the
        two halves of the product disagreed about one number.
        """
        if numerator is None or not denominator:
            return None
        return numerator / denominator

    cancel_rate = rate(cancelled, total)
    confirm_rate = rate(confirmed, total)
    rto_rate = rate(rto, confirmed)

    delivered = truth.delivered_orders
    if delivered is None:
        # Delivery lags, so project it from the rates rather than pretending the
        # number is known - but the projection needs both inputs. Projecting
        # with an assumed 0% RTO overstates delivered orders, which understates
        # CAC, which is the permissive direction.
        delivered = (
            int(round(confirmed * (1 - rto_rate)))
            if confirmed is not None and rto_rate is not None
            else None
        )

    # Return freight falls on the orders that DID deliver, so failed deliveries
    # amortise at rto/(1-rto), not at rto.
    rto_load = 0.0
    if rto_rate is not None and rto_rate < 1:
        rto_load = (rto_rate / (1 - rto_rate)) * unit.return_freight_inr

    cm_per_order = (
        unit.aov_inr * unit.gross_margin_rate - unit.fulfilment_cost_inr - rto_load
        if unit.gross_margin_rate is not None
        else None
    )

    delivered_revenue = truth.delivered_revenue_inr
    if delivered_revenue is None and delivered is not None:
        delivered_revenue = delivered * unit.aov_inr

    contribution_margin = (
        delivered * cm_per_order - spend_inr
        if delivered is not None and cm_per_order is not None
        else None
    )
    blended_cac = (spend_inr / delivered) if delivered else None
    cac_ceiling = (
        cm_per_order * (1 - unit.target_profit_share)
        if cm_per_order is not None
        else None
    )

    roas = (truth.revenue_inr / spend_inr) if (truth.revenue_inr and spend_inr) else None
    true_return = (
        contribution_margin / spend_inr * 100
        if spend_inr and contribution_margin is not None
        else None
    )

    return Economics(
        spend_inr=spend_inr,
        total_orders=total,
        confirmed_orders=confirmed,
        delivered_orders=delivered,
        cancel_rate=cancel_rate,
        rto_rate=rto_rate,
        confirm_rate=confirm_rate,
        delivered_revenue_inr=delivered_revenue,
        cm_per_delivered_order_inr=cm_per_order,
        contribution_margin_inr=contribution_margin,
        blended_cac_inr=blended_cac,
        cac_ceiling_inr=cac_ceiling,
        roas=roas,
        true_return_on_ad_spend_pct=true_return,
    )


def scaling_verdict(
    economics: Economics, confirm_rate_trend: float | None = None
) -> tuple[bool, str]:
    """Scale only while marginal CAC is under the ceiling AND confirm rate is
    not falling (PRD 12.4).

    A rising confirmed-order count with a falling confirm rate is not growth.
    """
    if economics.blended_cac_inr is None:
        return False, "No delivered orders yet, so there is no CAC to judge."

    if economics.cac_ceiling_inr is None:
        # The ceiling is derived from the product margin. Without it there is
        # nothing to compare against, and "no ceiling" must mean "cannot say"
        # rather than "no limit".
        return False, (
            "Your product margin is not on file, so there is no CAC ceiling to "
            "judge this against. Add the margin and I can tell you whether "
            "scaling is safe."
        )

    if economics.blended_cac_inr > economics.cac_ceiling_inr:
        return False, (
            f"Blended CAC is Rs {economics.blended_cac_inr:,.0f} against a ceiling of "
            f"Rs {economics.cac_ceiling_inr:,.0f} derived from your own margin and RTO. "
            "Scaling from here buys losses faster."
        )

    if confirm_rate_trend is not None and confirm_rate_trend < 0:
        return False, (
            "Confirm rate is falling. More volume at a declining confirm rate raises "
            "cancellations and RTO cost without adding delivered orders."
        )

    headroom = economics.cac_ceiling_inr - economics.blended_cac_inr
    return True, (
        f"Blended CAC is Rs {economics.blended_cac_inr:,.0f} against a ceiling of "
        f"Rs {economics.cac_ceiling_inr:,.0f} - Rs {headroom:,.0f} of headroom per "
        "delivered order, and confirm rate is not declining."
    )
