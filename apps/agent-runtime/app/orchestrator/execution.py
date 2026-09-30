"""From a proposed option to a typed tool request.

This is the join between the part of the system that reasons and the part that
spends money, and the whole module exists to make that join narrow.

A model proposing `{"tool": "update_budget", "target_entity_id": "…",
"params": {"daily_budget_inr": 20000}}` has made a **proposal**, not an
authorisation. Everything downstream still runs: the tool allow-list, the
autonomy matrix, the tenant check, the parameter validation, the guardrails, the
approval gate. What this module adds is the step before all of that - refusing to
construct a request at all out of anything it cannot type.

Deliberately pure: no database, no driver, no network. The failure mode it
guards against is a model inventing a tool name or a parameter, and that is
testable against a dictionary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.policy.risk import Tool

# What a proposal may ask for, and nothing else.
#
# Not `frozenset(Tool)`. A model must not be able to propose a READ - those have
# no approval gate and no verification step, so routing one through the
# proposal path would be a way to get work done with less scrutiny rather than
# more. Reads are what the analytics node already does, under its own budget.
PROPOSABLE_TOOLS: dict[str, Tool] = {
    Tool.UPDATE_BUDGET.value: Tool.UPDATE_BUDGET,
    Tool.PAUSE_ENTITY.value: Tool.PAUSE_ENTITY,
    Tool.ACTIVATE_ENTITY.value: Tool.ACTIVATE_ENTITY,
    Tool.CREATE_CAMPAIGN_DRAFT.value: Tool.CREATE_CAMPAIGN_DRAFT,
    Tool.CREATE_AD_SET_DRAFT.value: Tool.CREATE_AD_SET_DRAFT,
}

# Per tool, the parameters that may be carried through. An allow-list rather
# than a deny-list, because the cost of forgetting an entry is a refusal an
# owner will report, and the cost of forgetting to deny one is a parameter
# nobody reviewed reaching the Meta API.
ALLOWED_PARAMS: dict[Tool, frozenset[str]] = {
    Tool.UPDATE_BUDGET: frozenset({"daily_budget_inr"}),
    Tool.PAUSE_ENTITY: frozenset(),
    Tool.ACTIVATE_ENTITY: frozenset(),
    Tool.CREATE_CAMPAIGN_DRAFT: frozenset({"name", "objective"}),
    Tool.CREATE_AD_SET_DRAFT: frozenset(
        {"name", "campaign_id", "daily_budget_inr", "optimisation_event"}
    ),
}

# Tools that act ON an existing entity. Naming one without an entity is not a
# smaller request, it is an incoherent one.
NEEDS_TARGET: frozenset[Tool] = frozenset(
    {Tool.UPDATE_BUDGET, Tool.PAUSE_ENTITY, Tool.ACTIVATE_ENTITY}
)


class UnusableAction(ValueError):
    """The proposal cannot be turned into a request.

    Raised rather than returning None, because the reason is the useful part:
    it goes into the run's events so the owner is told the proposal could not be
    executed and why, instead of watching a turn quietly produce nothing.
    """


@dataclass(frozen=True, slots=True)
class ProposedAction:
    tool: Tool
    ad_account_id: str
    target_entity_id: str | None
    params: dict[str, Any]
    horizon_days: int


def action_from_option(
    option: dict[str, Any],
    *,
    default_ad_account_id: str | None,
    writable_ad_accounts: frozenset[str],
) -> ProposedAction:
    """Type the `action` block of a proposed option, or refuse.

    ``writable_ad_accounts`` comes from ``t_advit.meta_connections`` for this
    workspace. The pipeline's tenant check would refuse a foreign account
    anyway; checking here as well means the refusal names the proposal rather
    than surfacing as a denial three layers down, and it means a model cannot
    make the system attempt a cross-tenant call at all.
    """
    action = option.get("action")
    if not isinstance(action, dict):
        raise UnusableAction(
            f"option {option.get('label', '?')!r} carries no machine-readable action, "
            "so it can be discussed but not executed"
        )

    raw_tool = str(action.get("tool") or "").strip()
    tool = PROPOSABLE_TOOLS.get(raw_tool)
    if tool is None:
        raise UnusableAction(
            f"{raw_tool!r} is not a proposable tool; permitted: "
            + ", ".join(sorted(PROPOSABLE_TOOLS))
        )

    ad_account_id = str(action.get("ad_account_id") or default_ad_account_id or "").strip()
    if not ad_account_id:
        raise UnusableAction(
            "the proposal names no ad account and the workspace has no single "
            "writable one to default to"
        )
    if ad_account_id not in writable_ad_accounts:
        # Deliberately does not say whether the account exists elsewhere.
        raise UnusableAction(
            f"ad account {ad_account_id} is not a writable connection of this workspace"
        )

    target = action.get("target_entity_id")
    target_entity_id = str(target).strip() if target else None
    if tool in NEEDS_TARGET and not target_entity_id:
        raise UnusableAction(f"{tool.value} names no entity to act on")
    if tool not in NEEDS_TARGET and target_entity_id:
        raise UnusableAction(
            f"{tool.value} creates a new entity and cannot also target {target_entity_id}"
        )

    raw_params = action.get("params")
    params: dict[str, Any] = dict(raw_params) if isinstance(raw_params, dict) else {}
    allowed = ALLOWED_PARAMS[tool]
    unknown = sorted(set(params) - allowed)
    if unknown:
        # Not dropped silently. A parameter the model believed it was setting,
        # and that this system then ignored, produces an action that does not
        # match the proposal the owner approved.
        raise UnusableAction(
            f"{tool.value} does not accept {', '.join(unknown)}; permitted: "
            + (", ".join(sorted(allowed)) or "no parameters")
        )

    if "daily_budget_inr" in params:
        try:
            params["daily_budget_inr"] = float(params["daily_budget_inr"])
        except (TypeError, ValueError) as exc:
            raise UnusableAction(
                f"daily_budget_inr {params['daily_budget_inr']!r} is not a number"
            ) from exc

    horizon = option.get("horizon_days", 7)
    try:
        horizon_days = int(horizon)
    except (TypeError, ValueError):
        horizon_days = 7
    # The outcome check is scheduled at this horizon and the whole learning loop
    # reads it. A zero or a year are both ways of never being measured.
    horizon_days = max(1, min(horizon_days, 90))

    return ProposedAction(
        tool=tool,
        ad_account_id=ad_account_id,
        target_entity_id=target_entity_id,
        params=params,
        horizon_days=horizon_days,
    )


def recommended_option(proposal: dict[str, Any]) -> dict[str, Any] | None:
    """The option the strategy agent recommended, by label.

    Falls back to nothing rather than to the first option. "The model
    recommended something I could not find, so I did the first thing on the
    list" is how an owner ends up approving one action and getting another.
    """
    options = proposal.get("options")
    if not isinstance(options, list):
        return None
    label = str(proposal.get("recommended") or "").strip()
    for option in options:
        if isinstance(option, dict) and str(option.get("label", "")).strip() == label:
            return option
    return None
