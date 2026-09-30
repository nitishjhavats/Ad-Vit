"""The CTA decision model (PRD 11.5).

The ordering of the rules is the substance of the model, so most of these tests
assert precedence rather than a single answer: sensitivity outranks a cheaper
CPL, capacity outranks both, and broken measurement outranks the destination
with the best signal.

The Demo Brand case is the centrepiece, because it is the account actually
connected and its ad account is literally named "call ads".
"""

from __future__ import annotations

import pytest

from app.agents.cta_model import AccountProfile, Destination, recommend_cta


def demo_account(**overrides) -> AccountProfile:
    """The connected account, as the seeded context describes it."""
    base = dict(
        aov_inr=1299,
        gross_margin_rate=0.62,
        is_cod=True,
        rto_rate=0.26,
        is_sensitive_category=True,
        sensitivity_reason=(
            "a buyer will not say a piles problem aloud to a stranger on the phone"
        ),
        considered_purchase=True,
        sales_agents=2,
        working_hours_per_day=8,
        median_response_minutes=10,
        has_whatsapp_bsp=True,
        has_crm=False,
        has_landing_page=False,
        pixel_event_volume_ok=False,
        dataset_present=False,
    )
    base.update(overrides)
    return AccountProfile(**base)


# ---------------------------------------------------------------------------
# The account as it stands
# ---------------------------------------------------------------------------


def test_sensitive_category_ranks_whatsapp_first_and_call_last():
    """This single factor outranks CPL (PRD 11.5). It is also the finding that
    matters most for an account currently running call ads."""
    rec = recommend_cta(demo_account())

    assert rec.recommended is Destination.WHATSAPP
    assert rec.ranking.index(Destination.WHATSAPP) < rec.ranking.index(Destination.CALL)
    assert any("sensitive category" in r for r in rec.rationale)


def test_click_to_call_is_rejected_with_a_reason_not_merely_ranked_lower():
    rec = recommend_cta(demo_account())
    reason = rec.rejected[Destination.CALL.value]
    assert "aloud" in reason


def test_the_72_hour_window_is_cited_as_the_economic_reason():
    """The single most important economic fact in the CTA model for India."""
    rec = recommend_cta(demo_account())
    assert any("72-hour" in r for r in rec.rationale)


def test_high_rto_prefers_a_human_confirmation_step():
    rec = recommend_cta(demo_account(rto_rate=0.26, rto_margin_tolerance=0.20))
    assert any("human confirmation step" in r for r in rec.rationale)
    assert rec.ranking.index(Destination.INSTANT_FORM) > rec.ranking.index(
        Destination.WHATSAPP
    )


def test_missing_dataset_pushes_the_landing_page_last():
    """A landing page gives the best signal when tracking is healthy and the
    worst when it is not - and this account has no dataset."""
    rec = recommend_cta(demo_account())
    assert rec.ranking[-1] is Destination.LANDING_PAGE
    assert "optimising blind" in rec.rejected[Destination.LANDING_PAGE.value]


def test_requirements_for_the_recommendation_are_listed():
    rec = recommend_cta(demo_account())
    items = {r.item for r in rec.requirements}
    assert any("BSP" in i for i in items)
    assert all(isinstance(r.satisfied, bool) for r in rec.requirements)


# ---------------------------------------------------------------------------
# Capacity outranks everything
# ---------------------------------------------------------------------------


def test_saturated_team_demotes_the_cheapest_destination():
    """Buying leads you cannot call is the most common way an Indian D2C
    account burns money, so the highest-volume destination is demoted exactly
    when it looks most attractive."""
    rec = recommend_cta(
        demo_account(sales_agents=2, leads_per_day_capacity=60, current_leads_per_day=140)
    )
    assert any("exceeds what the team can work" in w for w in rec.warnings)
    assert rec.ranking[-1] in (Destination.INSTANT_FORM, Destination.LANDING_PAGE)
    assert "saturated" in rec.rejected[Destination.INSTANT_FORM.value]


def test_capacity_is_estimated_from_headcount_when_not_supplied():
    rec = recommend_cta(
        demo_account(sales_agents=1, working_hours_per_day=8, current_leads_per_day=200)
    )
    assert any("exceeds what the team can work" in w for w in rec.warnings)


def test_slow_response_disqualifies_the_instant_form():
    """Form leads decay faster than any other type."""
    rec = recommend_cta(demo_account(median_response_minutes=90))
    assert rec.ranking.index(Destination.INSTANT_FORM) >= 2
    assert "decay" in rec.rejected[Destination.INSTANT_FORM.value]
    assert any("five minutes" in r for r in rec.rationale)


def test_fast_response_leaves_the_form_viable():
    rec = recommend_cta(
        demo_account(is_sensitive_category=False, median_response_minutes=5, has_crm=True)
    )
    assert Destination.INSTANT_FORM.value not in rec.rejected


# ---------------------------------------------------------------------------
# Feasibility
# ---------------------------------------------------------------------------


def test_whatsapp_without_a_bsp_is_not_recommended_as_launchable():
    """The model must not recommend something that cannot be built today
    without saying so."""
    rec = recommend_cta(demo_account(has_whatsapp_bsp=False))
    assert rec.recommended is not Destination.WHATSAPP or rec.warnings


def test_an_infeasible_destination_states_what_is_missing():
    rec = recommend_cta(demo_account(has_whatsapp_bsp=False, sales_agents=2))
    assert "missing" in rec.rejected[Destination.WHATSAPP.value].lower()


def test_no_sales_team_rules_out_both_conversational_routes():
    rec = recommend_cta(
        demo_account(sales_agents=0, has_whatsapp_bsp=False, has_crm=True,
                  is_sensitive_category=False)
    )
    assert rec.recommended is not Destination.CALL
    assert rec.recommended is not Destination.WHATSAPP


# ---------------------------------------------------------------------------
# Other shapes of business
# ---------------------------------------------------------------------------


def test_explanation_heavy_product_with_healthy_tracking_prefers_a_landing_page():
    rec = recommend_cta(
        demo_account(
            is_sensitive_category=False,
            needs_explanation=True,
            audience_reads_comfortably=True,
            has_landing_page=True,
            dataset_present=True,
            pixel_event_volume_ok=True,
            rto_rate=0.05,
        )
    )
    assert rec.recommended is Destination.LANDING_PAGE
    assert any("needs explanation" in r for r in rec.rationale)


def test_non_sensitive_account_with_good_infrastructure_can_use_the_form():
    rec = recommend_cta(
        demo_account(
            is_sensitive_category=False,
            has_crm=True,
            has_whatsapp_bsp=False,
            median_response_minutes=4,
            rto_rate=0.05,
            dataset_present=True,
            pixel_event_volume_ok=True,
        )
    )
    assert rec.recommended in (Destination.INSTANT_FORM, Destination.LANDING_PAGE)


# ---------------------------------------------------------------------------
# Questions are asked only when the answer changes the ranking
# ---------------------------------------------------------------------------


def test_missing_rto_prompts_a_question_for_a_cod_business():
    rec = recommend_cta(demo_account(rto_rate=None))
    assert any("RTO" in q for q in rec.qualifying_questions)


def test_known_rto_prompts_no_rto_question():
    """The system never asks a question it can answer from its own data
    (PRD 5.3, FR-011)."""
    rec = recommend_cta(demo_account(rto_rate=0.26))
    assert not any("RTO" in q for q in rec.qualifying_questions)


def test_unknown_lead_volume_prompts_a_capacity_question():
    rec = recommend_cta(demo_account(current_leads_per_day=None))
    assert any("leads a day" in q for q in rec.qualifying_questions)


def test_recommendation_serialises_for_the_api():
    payload = recommend_cta(demo_account()).as_dict()
    assert payload["recommended"] == "click_to_whatsapp"
    assert payload["recommended_label"] == "Click to WhatsApp"
    assert isinstance(payload["ranking"], list)
    assert payload["rationale"]


def test_a_merit_rejected_destination_is_never_recommended():
    """The bug this pins: with no BSP, no CRM and no landing page, click-to-call
    was the only buildable option and won - while the same response listed it as
    ruled out for the category. Contradictory advice is worse than a wrong
    answer, because it destroys trust in every other line of the report."""
    rec = recommend_cta(demo_account(has_whatsapp_bsp=False, has_crm=False,
                                  has_landing_page=False))

    assert rec.recommended is not Destination.CALL
    assert Destination.CALL.value in rec.blocked_on_merit
    # WhatsApp does appear in `rejected`, but only as not-yet-buildable - which
    # is a build task, not a reason to prefer something harmful.
    assert rec.recommended.value not in rec.blocked_on_merit


def test_the_right_but_unbuilt_destination_is_recommended_as_build_first():
    """Being unbuildable makes something a build task, not a wrong answer."""
    rec = recommend_cta(demo_account(has_whatsapp_bsp=False))

    assert rec.recommended is Destination.WHATSAPP
    assert any("recommendation to build, not to launch" in w for w in rec.warnings)
    assert any("missing" in w for w in rec.warnings)


def test_it_names_why_the_account_is_on_call_ads_today():
    """Explaining the status quo is not endorsing it."""
    rec = recommend_cta(demo_account(has_whatsapp_bsp=False))
    assert any("not a reason to keep running it" in w for w in rec.warnings)


def test_a_buildable_and_meritorious_destination_needs_no_build_warning():
    rec = recommend_cta(demo_account(has_whatsapp_bsp=True))
    assert rec.recommended is Destination.WHATSAPP
    assert not any("recommendation to build" in w for w in rec.warnings)
