"""No campaign gets built until the owner has said where it sends people.

The destination - a phone call, a WhatsApp thread, a lead form, a landing page -
decides the whole funnel: what the sales operation has to be able to absorb,
what gets measured, what "a result" even means. A campaign built without that
decision has spent its first budget on a question nobody answered.

The prompt asks the strategy model to raise questions it cannot answer from the
facts. This does not rely on it remembering. After every proposal, in code:

  * if the recommended option would CREATE something - a campaign or an ad set
    draft - and no owner-asserted CTA is on file, the proposal is HELD. No
    decision is recorded, nothing reaches the approval gate, and the turn ends
    ASKED with one question that carries the CTA model's recommendation and its
    reasoning, so the owner is choosing between argued options rather than
    answering a blank. The hold outlives the turn: ``app.orchestrator.held``
    writes it to ``t_advit.held_proposals``, the Suggestions tab reads it from
    there, and the route that records the answer closes it.

  * if a CTA is on file, it is written into the action's parameters so the tool
    pipeline builds THAT, and the approval the owner signs names it.

  * budget changes, pauses and activations pass straight through. Those do not
    choose a destination; the campaign they act on already has one.

Where the answer lives: ``t_advit.account_context`` under
``(sales_operation, primary_cta)`` with ``source = 'owner_asserted'``. That table
is tenant-writable on purpose - this IS the owner's assertion, and the route that
records it runs on the tenant connection under ``auth.uid()``. The compliance
gate was once bitten by reading a ruleset out of this same table; the difference
is that a CTA is the owner's to choose and a statutory ruleset is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.cta_model import (
    DESTINATION_LABELS,
    AccountProfile,
    CTARecommendation,
    Destination,
    recommend_cta,
)
from app.policy.risk import Tool

# The tools that build something new and therefore need a destination.
BUILDS = frozenset({Tool.CREATE_CAMPAIGN_DRAFT.value, Tool.CREATE_AD_SET_DRAFT.value})

CTA_DIMENSION = "sales_operation"
CTA_KEY = "primary_cta"

# What an owner may say, and what the campaign builder calls it.
ACCEPTED: dict[str, Destination] = {
    "call": Destination.CALL,
    "click_to_call": Destination.CALL,
    "whatsapp": Destination.WHATSAPP,
    "click_to_whatsapp": Destination.WHATSAPP,
    "lead_form": Destination.INSTANT_FORM,
    "instant_form": Destination.INSTANT_FORM,
    "meta_instant_form": Destination.INSTANT_FORM,
    "landing_page": Destination.LANDING_PAGE,
    "website": Destination.LANDING_PAGE,
}


@dataclass(frozen=True, slots=True)
class GateResult:
    held: bool
    reason: str
    question: str | None = None
    recommendation: dict[str, Any] | None = None
    # The proposal, with the CTA written into the recommended action's params
    # when one was on file. Unchanged otherwise.
    proposal: dict[str, Any] = field(default_factory=dict)
    cta: str | None = None


def on_file(account_context: list[dict[str, Any]]) -> str | None:
    """The owner's asserted CTA, or None.

    Only ``owner_asserted``. The seed also carries an ``inferred`` row -
    "the ad account is named 'call ads', so probably calls" - and an inference
    is exactly the thing this gate exists to replace with a decision.
    """
    for row in account_context:
        if (
            row.get("dimension") == CTA_DIMENSION
            and row.get("key") == CTA_KEY
            and row.get("source") == "owner_asserted"
        ):
            value = row.get("value_json")
            if isinstance(value, str) and value.strip().lower() in ACCEPTED:
                return ACCEPTED[value.strip().lower()].value
    return None


def _profile(facts: dict[str, Any], account_context: list[dict[str, Any]]) -> AccountProfile:
    """Best effort from what is on file. What is not on file stays at the
    model's conservative default, and the model reports what it could not
    determine as qualifying questions - so the recommendation says what it
    does not know rather than pretending."""
    econ = facts.get("economics") or {}
    products = facts.get("products") or []
    product = products[0] if products else {}

    ctx: dict[tuple[str, str], Any] = {
        (r.get("dimension"), r.get("key")): r.get("value_json") for r in account_context
    }

    def ctx_get(dimension: str, key: str, default):
        value = ctx.get((dimension, key))
        return default if value is None else value

    aov = float(product.get("price_inr") or econ.get("aov_inr") or 0)
    margin = product.get("margin_rate")

    return AccountProfile(
        aov_inr=aov,
        gross_margin_rate=float(margin) if margin is not None else 0.0,
        is_cod=bool(ctx_get("sales_operation", "is_cod", True)),
        rto_rate=(float(econ["rto_rate"]) if econ.get("rto_rate") is not None else None),
        is_sensitive_category=bool(ctx_get("category", "is_sensitive", False)),
        sensitivity_reason=ctx_get("category", "sensitivity_reason", None),
        needs_explanation=bool(ctx_get("category", "needs_explanation", False)),
        considered_purchase=bool(ctx_get("category", "considered_purchase", True)),
        sales_agents=int(ctx_get("sales_operation", "sales_agents", 0) or 0),
        working_hours_per_day=float(ctx_get("sales_operation", "working_hours_per_day", 8.0) or 8.0),
        leads_per_day_capacity=ctx_get("sales_operation", "leads_per_day_capacity", None),
        current_leads_per_day=ctx_get("sales_operation", "current_leads_per_day", None),
        median_response_minutes=ctx_get("sales_operation", "median_response_minutes", None),
        has_whatsapp_bsp=bool(ctx_get("infrastructure", "has_whatsapp_bsp", False)),
        has_crm=bool(ctx_get("infrastructure", "has_crm", False)),
        has_landing_page=bool(ctx_get("infrastructure", "has_landing_page", False)),
        pixel_event_volume_ok=bool(ctx_get("infrastructure", "pixel_event_volume_ok", False)),
        dataset_present=bool(ctx_get("infrastructure", "dataset_present", False)),
    )


def _question(rec: CTARecommendation) -> str:
    label = DESTINATION_LABELS.get(rec.recommended, rec.recommended.value)
    why = "; ".join(rec.rationale[:2]) if rec.rationale else "based on the account's economics"
    alternatives = ", ".join(
        DESTINATION_LABELS.get(d, d.value) for d in rec.ranking if d is not rec.recommended
    )
    text = (
        f"Before this campaign is built: where should it send people? "
        f"I would recommend {label} - {why}."
    )
    if alternatives:
        text += f" The alternatives are {alternatives}."
    if rec.qualifying_questions:
        text += " To be surer I would need to know: " + " ".join(rec.qualifying_questions[:2])
    text += " Tell me which, and I will build it that way."
    return text


def _recommended_option(proposal: dict[str, Any]) -> dict[str, Any] | None:
    wanted = str(proposal.get("recommended") or "")
    for option in proposal.get("options") or []:
        if str(option.get("label") or "") == wanted:
            return option
    return None


def gate(proposal: dict[str, Any], *, account_context: list[dict[str, Any]],
         facts: dict[str, Any]) -> GateResult:
    option = _recommended_option(proposal)
    action = (option or {}).get("action") or {}
    tool = str(action.get("tool") or "")

    if tool not in BUILDS:
        return GateResult(held=False, reason="the recommended option builds nothing new",
                          proposal=proposal)

    chosen = on_file(account_context)
    if chosen is not None:
        # Written into the parameters so the pipeline builds THIS and the
        # approval binds to it. The proposal is copied rather than mutated: the
        # original is what the model said, and provenance should keep it.
        params = dict(action.get("params") or {})
        params["cta"] = chosen
        new_action = {**action, "params": params}
        new_option = {**option, "action": new_action}
        new_proposal = {
            **proposal,
            "options": [new_option if o is option else o for o in proposal.get("options") or []],
        }
        return GateResult(
            held=False,
            reason=f"the owner has chosen {chosen}; written into the action",
            proposal=new_proposal,
            cta=chosen,
        )

    rec = recommend_cta(_profile(facts, account_context))
    return GateResult(
        held=True,
        reason="the recommended option would build a campaign and no CTA is on file",
        question=_question(rec),
        recommendation=rec.as_dict(),
        proposal=proposal,
    )
