"""Tool risk classification and the autonomy matrix (PRD 10.5, 10.8).

Tools are classified by the damage a wrong call does, not by how complicated
the call is. The class decides the approval requirement, the verification depth
and the audit retention.

Everything in this module is data and pure functions. It is consulted at step 2
of the tool pipeline and reads nothing but its own tables - no model output, no
retrieved content, no request body can influence a classification.
"""

from __future__ import annotations

from enum import Enum


class RiskClass(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Tool(str, Enum):
    # --- LOW: reads. No approval at any tier, no verification needed. ------
    READ_ACCOUNT = "read_account"
    READ_ENTITIES = "read_entities"
    READ_ENTITY = "read_entity"
    READ_INSIGHTS = "read_insights"
    READ_DATASETS = "read_datasets"
    READ_DATASET_QUALITY = "read_dataset_quality"
    READ_AD_PREVIEW = "read_ad_preview"
    READ_POLICY_STATUS = "read_policy_status"

    # --- MEDIUM: create drafts (always paused), write memory. -------------
    CREATE_CAMPAIGN_DRAFT = "create_campaign_draft"
    CREATE_AD_SET_DRAFT = "create_ad_set_draft"
    UPLOAD_CREATIVE_ASSET = "upload_creative_asset"
    CREATE_CUSTOM_AUDIENCE = "create_custom_audience"
    WRITE_LEARNING = "write_learning"
    CREATE_EXPERIMENT_DESIGN = "create_experiment_design"

    # --- HIGH: change existing structure or state. ------------------------
    UPDATE_TARGETING = "update_targeting"
    UPDATE_PLACEMENTS = "update_placements"
    UPDATE_CREATIVE_ASSIGNMENT = "update_creative_assignment"
    PAUSE_ENTITY = "pause_entity"
    UPDATE_BUDGET = "update_budget"

    # --- CRITICAL: start spend, or change what the algorithm optimises. ---
    ACTIVATE_ENTITY = "activate_entity"
    CHANGE_OPTIMISATION_EVENT = "change_optimisation_event"
    CHANGE_OBJECTIVE = "change_objective"
    UPLOAD_CONVERSION_EVENTS = "upload_conversion_events"


RISK_CLASSES: dict[Tool, RiskClass] = {
    Tool.READ_ACCOUNT: RiskClass.LOW,
    Tool.READ_ENTITIES: RiskClass.LOW,
    Tool.READ_ENTITY: RiskClass.LOW,
    Tool.READ_INSIGHTS: RiskClass.LOW,
    Tool.READ_DATASETS: RiskClass.LOW,
    Tool.READ_DATASET_QUALITY: RiskClass.LOW,
    Tool.READ_AD_PREVIEW: RiskClass.LOW,
    Tool.READ_POLICY_STATUS: RiskClass.LOW,

    Tool.CREATE_CAMPAIGN_DRAFT: RiskClass.MEDIUM,
    Tool.CREATE_AD_SET_DRAFT: RiskClass.MEDIUM,
    Tool.UPLOAD_CREATIVE_ASSET: RiskClass.MEDIUM,
    Tool.CREATE_CUSTOM_AUDIENCE: RiskClass.MEDIUM,
    Tool.WRITE_LEARNING: RiskClass.MEDIUM,
    Tool.CREATE_EXPERIMENT_DESIGN: RiskClass.MEDIUM,

    Tool.UPDATE_TARGETING: RiskClass.HIGH,
    Tool.UPDATE_PLACEMENTS: RiskClass.HIGH,
    Tool.UPDATE_CREATIVE_ASSIGNMENT: RiskClass.HIGH,
    Tool.PAUSE_ENTITY: RiskClass.HIGH,
    Tool.UPDATE_BUDGET: RiskClass.HIGH,

    Tool.ACTIVATE_ENTITY: RiskClass.CRITICAL,
    Tool.CHANGE_OPTIMISATION_EVENT: RiskClass.CRITICAL,
    Tool.CHANGE_OBJECTIVE: RiskClass.CRITICAL,
    Tool.UPLOAD_CONVERSION_EVENTS: RiskClass.CRITICAL,
}

WRITE_TOOLS: frozenset[Tool] = frozenset(
    t for t, r in RISK_CLASSES.items() if r is not RiskClass.LOW
)

# Tools that mutate the ad account itself, as opposed to our own records.
# These are what the mutation lock serialises and what a read-only access mode
# refuses outright.
META_MUTATING_TOOLS: frozenset[Tool] = frozenset(
    {
        Tool.CREATE_CAMPAIGN_DRAFT,
        Tool.CREATE_AD_SET_DRAFT,
        Tool.UPLOAD_CREATIVE_ASSET,
        Tool.CREATE_CUSTOM_AUDIENCE,
        Tool.UPDATE_TARGETING,
        Tool.UPDATE_PLACEMENTS,
        Tool.UPDATE_CREATIVE_ASSIGNMENT,
        Tool.PAUSE_ENTITY,
        Tool.UPDATE_BUDGET,
        Tool.ACTIVATE_ENTITY,
        Tool.CHANGE_OPTIMISATION_EVENT,
        Tool.CHANGE_OBJECTIVE,
        Tool.UPLOAD_CONVERSION_EVENTS,
    }
)

# Structural change is approval-gated at EVERY tier, including L4. This is a
# permanent design commitment, not a v1 limitation (PRD 4.5, 10.5).
ALWAYS_APPROVED: frozenset[Tool] = frozenset(
    {
        Tool.ACTIVATE_ENTITY,
        Tool.CHANGE_OPTIMISATION_EVENT,
        Tool.CHANGE_OBJECTIVE,
        Tool.UPLOAD_CONVERSION_EVENTS,
        Tool.UPDATE_TARGETING,
        Tool.UPDATE_PLACEMENTS,
    }
)

# Pausing is HIGH class - it changes live delivery - but stopping something is
# always safe and PRD 10.5 grants it from L1. Rather than misclassify the risk,
# the exception is explicit: tool -> minimum autonomy at which it is
# pre-authorised without an approval.
PRE_AUTHORISED_FROM: dict[Tool, int] = {
    Tool.PAUSE_ENTITY: 1,
}

# Minimum autonomy at which a class stops needing an approval (PRD 10.8 table).
_AUTONOMY_FLOOR: dict[RiskClass, int] = {
    RiskClass.LOW: 0,
    RiskClass.MEDIUM: 2,
    RiskClass.HIGH: 3,
    RiskClass.CRITICAL: 99,  # unreachable: always approved
}


def risk_class(tool: Tool) -> RiskClass:
    return RISK_CLASSES[tool]


def requires_approval(tool: Tool, effective_autonomy: int) -> bool:
    """Whether this call needs a human before it may execute.

    ``effective_autonomy`` must be the value from
    ``t_advit.effective_autonomy()`` - min(workspace intent, plan
    entitlement) - never the raw workspace column, or a plan cap can be
    bypassed by editing the workspace.
    """
    if tool in ALWAYS_APPROVED:
        return True

    cls = risk_class(tool)
    if cls is RiskClass.LOW:
        return False

    floor = PRE_AUTHORISED_FROM.get(tool, _AUTONOMY_FLOOR[cls])
    return effective_autonomy < floor


def requires_freshness_recheck(tool: Tool) -> bool:
    """CRITICAL actions re-evaluate guardrails and evidence immediately before
    execution, because policy state can change between approval and execution
    (PRD 10.8 step 7, 14.6)."""
    return risk_class(tool) is RiskClass.CRITICAL


def requires_verification(tool: Tool) -> bool:
    """Success is a read-back that matches the proposal, not a 200 response."""
    return risk_class(tool) is not RiskClass.LOW


def requires_outcome_check(tool: Tool) -> bool:
    """CRITICAL actions get a scheduled post-execution outcome check at the
    pre-registered horizon."""
    return risk_class(tool) is RiskClass.CRITICAL
