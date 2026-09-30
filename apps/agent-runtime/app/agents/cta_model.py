"""The CTA decision model (PRD 11.5).

Which destination: click-to-call, click-to-WhatsApp, Meta instant form, or a
landing page. Economics and operations first, preference last.

The rules below are ordered, and the first one that fires decides. That
ordering is the substance of the model: category sensitivity outranks a cheaper
CPL, and sales-team capacity outranks both. Buying leads you cannot call is the
most common way an Indian D2C account burns money.

Deterministic by design. No model is consulted, so the recommendation is
reproducible, explainable line by line, and free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Destination(str, Enum):
    CALL = "click_to_call"
    WHATSAPP = "click_to_whatsapp"
    INSTANT_FORM = "meta_instant_form"
    LANDING_PAGE = "landing_page"


DESTINATION_LABELS: dict[Destination, str] = {
    Destination.CALL: "Click to Call",
    Destination.WHATSAPP: "Click to WhatsApp",
    Destination.INSTANT_FORM: "Meta Instant Form",
    Destination.LANDING_PAGE: "Landing Page",
}


@dataclass(frozen=True, slots=True)
class AccountProfile:
    """What the model needs. Every field comes from account context or the
    industry pack - none of it is guessed at decision time."""

    # Economics
    aov_inr: float
    gross_margin_rate: float
    is_cod: bool = True
    rto_rate: float | None = None
    rto_margin_tolerance: float = 0.25

    # Category
    is_sensitive_category: bool = False
    sensitivity_reason: str | None = None
    needs_explanation: bool = False
    considered_purchase: bool = True

    # Sales operation
    sales_agents: int = 0
    working_hours_per_day: float = 8.0
    leads_per_day_capacity: int | None = None
    current_leads_per_day: int | None = None
    median_response_minutes: int | None = None

    # Infrastructure
    has_whatsapp_bsp: bool = False
    has_crm: bool = False
    has_landing_page: bool = False
    pixel_event_volume_ok: bool = False
    dataset_present: bool = False

    # Audience
    audience_reads_comfortably: bool = True


@dataclass(frozen=True, slots=True)
class Requirement:
    item: str
    satisfied: bool
    detail: str


@dataclass(slots=True)
class CTARecommendation:
    recommended: Destination
    ranking: list[Destination]
    rationale: list[str] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    # Rejected ON MERIT - wrong for the category or the economics. These can
    # never be recommended. Distinct from merely not-yet-buildable, which makes
    # a destination a build task rather than a wrong answer.
    blocked_on_merit: list[str] = field(default_factory=list)
    qualifying_questions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recommended": self.recommended.value,
            "recommended_label": DESTINATION_LABELS[self.recommended],
            "ranking": [d.value for d in self.ranking],
            "rationale": self.rationale,
            "requirements": [
                {"item": r.item, "satisfied": r.satisfied, "detail": r.detail}
                for r in self.requirements
            ],
            "warnings": self.warnings,
            "rejected": self.rejected,
            "blocked_on_merit": self.blocked_on_merit,
            "qualifying_questions": self.qualifying_questions,
        }


# What each destination needs before it can work at all (PRD 11.5).
def _requirements(destination: Destination, p: AccountProfile) -> list[Requirement]:
    if destination is Destination.WHATSAPP:
        return [
            Requirement(
                "WhatsApp Business API via a BSP",
                p.has_whatsapp_bsp,
                "Required to receive and reply to click-to-WhatsApp conversations.",
            ),
            Requirement(
                "An agent or bot answering the thread",
                p.sales_agents > 0,
                f"{p.sales_agents} agent(s) on the team.",
            ),
            Requirement(
                "Approved template set and opt-in handling",
                p.has_whatsapp_bsp,
                "Templates are needed once the free window closes.",
            ),
        ]
    if destination is Destination.CALL:
        return [
            Requirement(
                "Call capacity during working hours",
                p.sales_agents > 0,
                f"{p.sales_agents} agent(s) across {p.working_hours_per_day:.0f} hours.",
            ),
            Requirement(
                "A number that is actually answered",
                p.sales_agents > 0,
                "An unanswered call is a lost lead with no recovery path.",
            ),
        ]
    if destination is Destination.INSTANT_FORM:
        return [
            Requirement(
                "CRM integration for fast follow-up",
                p.has_crm,
                "Form leads decay faster than any other type.",
            ),
            Requirement(
                "Higher Intent form with a qualifying question",
                True,
                "Choose Higher Intent over More Volume, and add one qualifying question.",
            ),
            Requirement(
                "DPDP-compliant consent notice and privacy policy URL",
                False,
                "Consent must be logged and withdrawable as easily as it was given.",
            ),
        ]
    return [
        Requirement(
            "A fast mobile landing page",
            p.has_landing_page,
            "Message match with the ad, and page speed under control.",
        ),
        Requirement(
            "Pixel and CAPI with usable event volume",
            p.pixel_event_volume_ok and p.dataset_present,
            "A landing page with broken tracking starves the algorithm.",
        ),
    ]


def recommend_cta(p: AccountProfile) -> CTARecommendation:
    rationale: list[str] = []
    warnings: list[str] = []
    rejected: dict[str, str] = {}
    questions: list[str] = []

    # Start from a neutral order and let the rules reorder it.
    ranking = [
        Destination.WHATSAPP,
        Destination.INSTANT_FORM,
        Destination.CALL,
        Destination.LANDING_PAGE,
    ]

    # --- Rule 1. Category sensitivity outranks a cheaper CPL --------------
    if p.is_sensitive_category:
        ranking = [
            Destination.WHATSAPP,
            Destination.INSTANT_FORM,
            Destination.CALL,
            Destination.LANDING_PAGE,
        ]
        reason = p.sensitivity_reason or "the buyer is unlikely to say the problem aloud"
        rationale.append(
            f"This is a sensitive category: {reason}. Text is private in a way a phone "
            "call is not, so WhatsApp ranks first and click-to-call last. This overrides "
            "a cheaper cost per lead."
        )
        rejected[Destination.CALL.value] = (
            "Click-to-call asks the buyer to speak a private problem aloud to a stranger. "
            "For this category that suppresses both volume and honesty, and the leads that "
            "do come through are harder to qualify."
        )

    # --- Rule 2. Capacity outranks everything -----------------------------
    capacity = p.leads_per_day_capacity
    if capacity is None and p.sales_agents:
        # ~12 minutes per lead including follow-up attempts.
        capacity = int(p.sales_agents * p.working_hours_per_day * 5)

    over_capacity = (
        capacity is not None
        and p.current_leads_per_day is not None
        and p.current_leads_per_day > capacity
    )
    if over_capacity:
        warnings.append(
            f"Lead volume ({p.current_leads_per_day}/day) already exceeds what the team can "
            f"work ({capacity}/day). Stop optimising for more leads: add a qualifying "
            "question, move to a higher-friction destination, or optimise for quality. "
            "Buying leads you cannot call is the most common way an Indian D2C account "
            "burns money."
        )
        if Destination.INSTANT_FORM in ranking:
            ranking.remove(Destination.INSTANT_FORM)
            ranking.append(Destination.INSTANT_FORM)
        rejected[Destination.INSTANT_FORM.value] = (
            "The instant form is the cheapest and highest-volume destination, which is "
            "exactly wrong when the team is already saturated."
        )

    # --- Rule 3. Response time gates the instant form ---------------------
    if p.median_response_minutes is not None and p.median_response_minutes > 15:
        if Destination.INSTANT_FORM in ranking:
            ranking.remove(Destination.INSTANT_FORM)
            ranking.append(Destination.INSTANT_FORM)
        rejected.setdefault(
            Destination.INSTANT_FORM.value,
            f"Median response is {p.median_response_minutes} minutes. Form leads decay "
            "faster than any other type - a cheap lead called tomorrow is a wasted lead.",
        )
        rationale.append(
            "Contact within five minutes converts far better than after an hour, so the "
            "instant form is not viable at the current response time."
        )

    # --- Rule 4. COD RTO prefers a human confirmation step ----------------
    if p.is_cod and p.rto_rate is not None and p.rto_rate > p.rto_margin_tolerance:
        rationale.append(
            f"RTO is {p.rto_rate:.0%}, above your {p.rto_margin_tolerance:.0%} tolerance. "
            "Destinations with a human confirmation step before dispatch - WhatsApp or a "
            "call - protect margin better than a frictionless form."
        )
        if Destination.INSTANT_FORM in ranking:
            ranking.remove(Destination.INSTANT_FORM)
            ranking.append(Destination.INSTANT_FORM)

    # --- Rule 5. Broken measurement prefers native platform events --------
    if not p.dataset_present or not p.pixel_event_volume_ok:
        if Destination.LANDING_PAGE in ranking:
            ranking.remove(Destination.LANDING_PAGE)
            ranking.append(Destination.LANDING_PAGE)
        rejected[Destination.LANDING_PAGE.value] = (
            "No usable dataset or pixel event volume. A landing page produces the best "
            "signal when tracking is healthy and the worst when it is not - the algorithm "
            "would be optimising blind."
        )
        rationale.append(
            "Until measurement is repaired, prefer destinations with native platform "
            "events (WhatsApp, instant form) over a landing page."
        )

    # --- Rule 6. Explanation-heavy products want a landing page -----------
    if p.needs_explanation and p.audience_reads_comfortably and p.dataset_present:
        ranking.remove(Destination.LANDING_PAGE)
        ranking.insert(0, Destination.LANDING_PAGE)
        rationale.append(
            "The product needs explanation and the audience reads comfortably, so a "
            "landing page can carry the argument - audited for message match first."
        )

    # --- Selection -------------------------------------------------------
    #
    # Two different kinds of "no", and conflating them produces contradictory
    # advice. A destination rejected ON MERIT - wrong for the category, wrong
    # for the economics - must never be recommended, however easy it is to
    # build. A destination that is merely NOT YET BUILDABLE is still the right
    # answer; it just needs building first.
    #
    # Recommending click-to-call to a piles advertiser because it happens to be
    # the only thing already wired up would be worse than useless: it is the
    # destination the model has just finished explaining is wrong.
    blocked_on_merit = set(rejected)

    def buildable(d: Destination) -> bool:
        blocking = [r for r in _requirements(d, p) if not r.satisfied]
        if d is Destination.WHATSAPP:
            return p.has_whatsapp_bsp and p.sales_agents > 0
        if d is Destination.CALL:
            return p.sales_agents > 0
        if d is Destination.LANDING_PAGE:
            return p.has_landing_page
        return not any(r.item.startswith("CRM") for r in blocking)

    on_merit = [d for d in ranking if d.value not in blocked_on_merit]
    if not on_merit:
        # Everything was rejected on merit. Fall back to the least-bad option
        # rather than inventing one, and say so plainly.
        on_merit = ranking
        warnings.append(
            "Every destination has a material objection for this account. The ranking "
            "below is least-bad, not good."
        )

    ready_now = [d for d in on_merit if buildable(d)]
    recommended = ready_now[0] if ready_now else on_merit[0]

    if not ready_now:
        missing = [r.item for r in _requirements(recommended, p) if not r.satisfied]
        warnings.append(
            f"{DESTINATION_LABELS[recommended]} is the right destination for this account, "
            "but it cannot run yet - missing: "
            + ", ".join(missing)
            + ". This is a recommendation to build, not to launch."
        )
        if buildable(Destination.CALL) and Destination.CALL.value in blocked_on_merit:
            warnings.append(
                "Click-to-call is the only destination already wired up, which is why the "
                "account is running it. It is not a reason to keep running it."
            )

    # Order the final ranking: recommended first, then the rest on merit, then
    # anything rejected on merit last.
    ordered = [recommended]
    ordered += [d for d in on_merit if d is not recommended]
    ordered += [d for d in ranking if d not in ordered]

    for d in ranking:
        if not buildable(d) and d.value not in rejected:
            missing = [r.item for r in _requirements(d, p) if not r.satisfied]
            rejected[d.value] = "Not available yet - missing: " + ", ".join(missing)

    # --- Economics note ---------------------------------------------------
    if recommended is Destination.WHATSAPP:
        rationale.append(
            "Click-to-WhatsApp opens a 72-hour window in which all message categories are "
            "free. For a considered purchase needing follow-up, that materially changes "
            "the economics of qualification."
        )
    # --- Questions asked only when the answer would change the ranking ----
    if p.rto_rate is None and p.is_cod:
        questions.append(
            "What share of dispatched COD orders come back RTO? It decides whether a human "
            "confirmation step is worth its cost per lead."
        )
    if p.current_leads_per_day is None and p.sales_agents:
        questions.append(
            "How many leads a day does the team handle now? If you are already at capacity, "
            "more leads are waste rather than growth."
        )

    # De-duplicate while preserving order: the build-first path and the
    # economics note can otherwise say the same thing twice.
    warnings = list(dict.fromkeys(warnings))

    return CTARecommendation(
        recommended=recommended,
        blocked_on_merit=sorted(blocked_on_merit),
        ranking=ordered,
        rationale=rationale,
        requirements=_requirements(recommended, p),
        warnings=warnings,
        rejected=rejected,
        qualifying_questions=questions,
    )
