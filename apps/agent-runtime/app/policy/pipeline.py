"""The tool invocation pipeline (PRD 10.8).

Fifteen steps. Steps 1-5 and 8-15 are deterministic code: no model output can
skip a step, and no content retrieved from a web page, a landing page, an ad or
a customer document can influence steps 1, 2, 3 or 5 - those read only from the
policy store and the database (PRD 14.7).

    1.  Tool router          is this tool in the agent's allow-list?
    2.  Policy check         does the autonomy tier permit this risk class?
    3.  Tenant check         does the target entity belong to this workspace?
    4.  Parameter validation schema + semantic bounds
    5.  Guardrail evaluation caps, ceilings, velocity, access mode, connection
    6.  Approval gate        HIGH/CRITICAL: suspend and wait
    7.  Freshness re-check   CRITICAL only: re-evaluate 5 and the evidence now
    8.  Concurrency lock     per-ad-account mutation lock
    9.  Audit (pre)          intent + idempotency key, BEFORE the call
    10. Driver call          the actual mutation
    11. Verification read    read the entity back
    12. State diff           expected vs actual; mismatch -> rollback
    13. Audit (post)         before, after, response, rollback handle
    14. Release lock
    15. Learning event       queue the outcome check at the horizon

The pipeline is synchronous and returns AWAITING_APPROVAL rather than blocking.
Suspension is the caller's job - a LangGraph ``interrupt()`` - which keeps every
step here directly testable.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ContextManager, Iterator, Protocol

from app.meta.driver import (
    Entity,
    EntityLevel,
    EntityStatus,
    MetaDriver,
    MetaError,
    MetaErrorKind,
    WriteForbidden,
)
from app.policy.risk import (
    ALWAYS_APPROVED,
    META_MUTATING_TOOLS,
    RiskClass,
    Tool,
    requires_approval,
    requires_freshness_recheck,
    requires_outcome_check,
    requires_verification,
    risk_class,
)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


class Decision(str, Enum):
    EXECUTED = "executed"
    AWAITING_APPROVAL = "awaiting_approval"
    DENIED = "denied"
    HALTED = "halted"


class DenialReason(str, Enum):
    TOOL_NOT_ALLOWED = "tool_not_allowed"           # step 1
    AUTONOMY_INSUFFICIENT = "autonomy_insufficient"  # step 2
    TENANT_MISMATCH = "tenant_mismatch"             # step 3
    INVALID_PARAMETERS = "invalid_parameters"        # step 4
    GUARDRAIL_BREACH = "guardrail_breach"           # step 5 / 7
    APPROVAL_EXPIRED = "approval_expired"           # step 6
    APPROVAL_REJECTED = "approval_rejected"         # step 6
    APPROVAL_MISMATCH = "approval_mismatch"         # step 6
    WORKSPACE_PAUSED = "workspace_paused"           # kill switch
    ACCESS_READ_ONLY = "access_read_only"           # subscription degraded
    CONNECTION_NOT_WRITABLE = "connection_not_writable"
    LOCK_TIMEOUT = "lock_timeout"                   # step 8
    VERIFICATION_MISMATCH = "verification_mismatch"  # step 12
    DRIVER_ERROR = "driver_error"                   # step 10
    ALREADY_EXECUTED = "already_executed"           # step 9


class AlreadyExecuted(Exception):
    """This exact tool call has already run against Meta.

    Raised by the audit sink at step 9, from the uniqueness constraint on
    `idempotency_key` that migration 7 introduced with the sentence "idempotency
    is a uniqueness constraint, not a convention". It was a convention: the
    insert resolved the collision with `do update ... returning id` and never
    looked at `executed_at`, so a replayed request was handed the first
    execution's row and the pipeline went on to issue the mutation a second time.

    Carries the prior action so the caller can say WHAT already happened and
    WHEN, rather than reporting a bare refusal for something that in fact
    succeeded.
    """

    def __init__(self, message: str, *, action_id: str, executed_at: object) -> None:
        super().__init__(message)
        self.action_id = action_id
        self.executed_at = executed_at


@dataclass(slots=True)
class GuardrailBreach:
    guardrail: str
    guardrail_class: str
    threshold: float | None
    observed: float | None
    message: str


@dataclass(slots=True)
class ToolOutcome:
    decision: Decision
    tool: Tool
    risk: RiskClass
    reason: DenialReason | None = None
    message: str = ""
    entity: Entity | None = None
    # Read results. Reads return data rather than an entity transition, so they
    # never populate before/after state or a rollback handle.
    data: Any | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    verified: bool = False
    verification_diff: dict[str, Any] | None = None
    rollback_handle: dict[str, Any] | None = None
    # Whether a rollback was actually carried out, and why it was not. The
    # verification-mismatch path used to say "rolled back" in its message while
    # performing no rollback at all, which is the one claim this product must
    # never make falsely.
    rolled_back: bool = False
    rollback_error: str | None = None
    breaches: list[GuardrailBreach] = field(default_factory=list)
    approval_id: str | None = None
    audit_id: str | None = None
    idempotency_key: str | None = None
    external_request_id: str | None = None
    outcome_check_queued: bool = False
    steps_completed: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.decision is Decision.EXECUTED


# ---------------------------------------------------------------------------
# Ports. Injected so every step is testable without a database.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """The AI is an actor with an identity, not an implicit superuser
    (PRD 3.5). Allow-lists are assigned by the orchestrator from static
    configuration - an agent cannot request a capability, because requesting
    one is not a representable action (PRD 14.7)."""

    name: str
    allowed_tools: frozenset[Tool]


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    workspace_id: str
    org_id: str
    access_mode: str            # full | read_only | denied
    effective_autonomy: int     # min(workspace intent, plan entitlement)
    daily_cap_inr: float
    monthly_cap_inr: float
    cac_ceiling_inr: float | None
    is_paused: bool
    # Whether the spend figures below rest on anything. Deliberately has no
    # default: `coalesce(sum(...), 0)` cannot tell "spent nothing" from "never
    # ingested", and a cap compared against an assumed zero is not a cap.
    spend_basis_known: bool
    # Separate from the flag above on purpose. That one is satisfied by a
    # completed connection check - we have enumerated the account. This one
    # requires ingested metrics for the current month, which is what the
    # monthly cap actually needs, and which no workspace has today.
    month_basis_known: bool
    # Most recent measured blended CAC, or None when it has never been
    # measurable. Only meaningful beside cac_ceiling_inr, which was loaded for
    # months and compared against nothing.
    recent_cac_inr: float | None = None
    spend_today_inr: float = 0.0
    spend_month_inr: float | None = None
    budget_step_ceiling_pct: float = 20.0


@dataclass(frozen=True, slots=True)
class ConnectionPolicy:
    ad_account_id: str
    write_enabled: bool
    health: str


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: str
    status: str                 # pending | approved | modified | rejected | expired
    expired: bool
    proposed: dict[str, Any] = field(default_factory=dict)


class PolicyStore(Protocol):
    def workspace_policy(self, workspace_id: str) -> WorkspacePolicy: ...
    def connection_policy(self, workspace_id: str, ad_account_id: str) -> ConnectionPolicy | None: ...
    def approval(self, approval_id: str, workspace_id: str) -> ApprovalRecord | None: ...
    def create_approval(
        self, *, workspace_id: str, decision_id: str, risk: RiskClass,
        proposed: dict[str, Any], impact_inr: float | None,
    ) -> str: ...


class AuditSink(Protocol):
    def pre(
        self, *, workspace_id: str, agent: str, tool: Tool, risk: RiskClass,
        idempotency_key: str, params: dict[str, Any], decision_id: str | None,
        approval_id: str | None, policy_decision_id: str,
    ) -> str: ...

    def post(
        self, *, audit_id: str, outcome: ToolOutcome,
    ) -> None: ...

    def guardrail(
        self, *, workspace_id: str, breach: GuardrailBreach, action_taken: str,
    ) -> None: ...


class LockManager(Protocol):
    def acquire(self, ad_account_id: str, timeout_s: int) -> ContextManager[bool]: ...


class OutcomeScheduler(Protocol):
    def queue(self, *, decision_id: str, horizon_days: int) -> None: ...


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolRequest:
    tool: Tool
    workspace_id: str
    ad_account_id: str
    params: dict[str, Any] = field(default_factory=dict)
    target_entity_id: str | None = None
    decision_id: str | None = None
    approval_id: str | None = None
    idempotency_key: str | None = None
    horizon_days: int = 7

    def authorisation_fingerprint(self) -> dict[str, Any]:
        """What an approval actually authorises.

        An approval is permission to do ONE specific thing. Without binding it
        to that thing, holding any valid approval id is permission to do
        anything: a Rs 500 pause approval would execute a Rs 200,000 activation
        in another workspace. Every field here is one an owner would consider
        part of what they said yes to.
        """
        spend_params = {
            k: v for k, v in self.params.items()
            if k in ("daily_budget_inr", "optimisation_event", "objective", "campaign_id")
        }
        return {
            "tool": self.tool.value,
            "workspace_id": self.workspace_id,
            "ad_account_id": self.ad_account_id,
            "target_entity_id": self.target_entity_id,
            "params": {k: spend_params[k] for k in sorted(spend_params)},
        }

    def derive_idempotency_key(self) -> str:
        """Derived from the decision and the action parameters, so a retry
        after an ambiguous timeout produces the same key (PRD 10.9)."""
        if self.idempotency_key:
            return self.idempotency_key
        basis = "|".join(
            [
                self.decision_id or "no-decision",
                self.tool.value,
                self.ad_account_id,
                self.target_entity_id or "-",
                repr(sorted(self.params.items())),
            ]
        )
        return f"idem-{uuid.uuid5(uuid.NAMESPACE_URL, basis)}"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class ToolPipeline:
    def __init__(
        self,
        *,
        driver: MetaDriver,
        policy: PolicyStore,
        audit: AuditSink,
        locks: LockManager,
        scheduler: OutcomeScheduler | None = None,
        lock_timeout_s: int = 30,
    ) -> None:
        self._driver = driver
        self._policy = policy
        self._audit = audit
        self._locks = locks
        self._scheduler = scheduler
        self._lock_timeout_s = lock_timeout_s

    def invoke(self, agent: AgentIdentity, request: ToolRequest) -> ToolOutcome:
        tool = request.tool
        risk = risk_class(tool)
        steps: list[int] = []

        def deny(reason: DenialReason, message: str, breaches=None) -> ToolOutcome:
            return ToolOutcome(
                decision=Decision.DENIED,
                tool=tool,
                risk=risk,
                reason=reason,
                message=message,
                breaches=breaches or [],
                steps_completed=steps,
            )

        # -- 1. Tool router -------------------------------------------------
        if tool not in agent.allowed_tools:
            return deny(
                DenialReason.TOOL_NOT_ALLOWED,
                f"agent {agent.name!r} has no allow-list entry for {tool.value}",
            )
        steps.append(1)

        wp = self._policy.workspace_policy(request.workspace_id)

        # Kill switch and subscription state come before anything else: a
        # frozen workspace or a lapsed subscription must stop a mutation
        # regardless of autonomy or approval (PRD 14.3, 18).
        if wp.is_paused and tool in META_MUTATING_TOOLS:
            return ToolOutcome(
                decision=Decision.HALTED,
                tool=tool,
                risk=risk,
                reason=DenialReason.WORKSPACE_PAUSED,
                message="workspace automation is frozen; nothing executes until unfrozen",
                steps_completed=steps,
            )

        if tool in META_MUTATING_TOOLS and wp.access_mode != "full":
            reason = (
                DenialReason.ACCESS_READ_ONLY
                if wp.access_mode == "read_only"
                else DenialReason.GUARDRAIL_BREACH
            )
            return deny(
                reason,
                f"organisation access mode is {wp.access_mode!r}; reads continue, writes do not",
            )

        # -- 2. Policy check ------------------------------------------------
        needs_approval = requires_approval(tool, wp.effective_autonomy)
        steps.append(2)

        # -- 3. Tenant check ------------------------------------------------
        conn = self._policy.connection_policy(request.workspace_id, request.ad_account_id)
        if conn is None:
            return deny(
                DenialReason.TENANT_MISMATCH,
                f"ad account {request.ad_account_id} is not connected to this workspace",
            )
        if tool in META_MUTATING_TOOLS and not conn.write_enabled:
            return deny(
                DenialReason.CONNECTION_NOT_WRITABLE,
                f"ad account {request.ad_account_id} is connected read-only",
            )
        if request.target_entity_id is not None:
            target = self._driver.get_entity(request.ad_account_id, request.target_entity_id)
            if target is None:
                return deny(
                    DenialReason.TENANT_MISMATCH,
                    f"entity {request.target_entity_id} not found in ad account "
                    f"{request.ad_account_id}",
                )
        else:
            target = None
        steps.append(3)

        # -- 4. Parameter validation ----------------------------------------
        invalid = self._validate(tool, request, target)
        if invalid:
            return deny(DenialReason.INVALID_PARAMETERS, invalid)
        steps.append(4)

        # -- 5. Guardrail evaluation ----------------------------------------
        # Read once, here, where the target was resolved and where a MetaError
        # surfaces the same way the target's own read does. Both `_guardrails`
        # calls and `_impact` take this value rather than reaching for the
        # driver themselves.
        live_children_inr: float | None = None
        if (
            tool is Tool.ACTIVATE_ENTITY
            and target is not None
            and target.level is EntityLevel.CAMPAIGN
        ):
            live_children_inr = self._live_child_budget(request.ad_account_id, target.id)

        breaches = self._guardrails(tool, request, wp, target, live_children_inr)
        if breaches:
            for b in breaches:
                self._audit.guardrail(
                    workspace_id=request.workspace_id, breach=b, action_taken="denied"
                )
            return deny(
                DenialReason.GUARDRAIL_BREACH,
                "; ".join(b.message for b in breaches),
                breaches,
            )
        steps.append(5)

        # LOW-risk reads stop here. PRD 10.8: no approval at any tier, no
        # verification needed - and PRD 10.9: reads never take the mutation
        # lock, or a long report would block the account's writes.
        if risk is RiskClass.LOW:
            try:
                data = self._execute_read(tool, request)
            except MetaError as exc:
                return ToolOutcome(
                    decision=Decision.DENIED,
                    tool=tool,
                    risk=risk,
                    reason=DenialReason.DRIVER_ERROR,
                    message=f"{exc.kind.value}: {exc.message}",
                    steps_completed=steps,
                )
            return ToolOutcome(
                decision=Decision.EXECUTED,
                tool=tool,
                risk=risk,
                data=data,
                entity=data if isinstance(data, Entity) else None,
                verified=True,
                steps_completed=steps,
            )

        policy_decision_id = str(uuid.uuid4())

        # -- 6. Approval gate -----------------------------------------------
        if needs_approval:
            if request.approval_id is None:
                approval_id = self._policy.create_approval(
                    workspace_id=request.workspace_id,
                    decision_id=request.decision_id or policy_decision_id,
                    risk=risk,
                    proposed={
                        "tool": tool.value,
                        "ad_account_id": request.ad_account_id,
                        "target_entity_id": request.target_entity_id,
                        "params": request.params,
                        # Bound at issue, verified at redemption. Without this
                        # an approval is a bearer token for any mutation.
                        "authorisation": request.authorisation_fingerprint(),
                    },
                    impact_inr=self._impact(tool, request, target, live_children_inr),
                )
                return ToolOutcome(
                    decision=Decision.AWAITING_APPROVAL,
                    tool=tool,
                    risk=risk,
                    approval_id=approval_id,
                    message="suspended pending approval",
                    steps_completed=steps,
                )

            record = self._policy.approval(request.approval_id, request.workspace_id)
            if record is None:
                return deny(
                    DenialReason.APPROVAL_EXPIRED, f"approval {request.approval_id} not found"
                )
            if record.status == "rejected":
                return deny(DenialReason.APPROVAL_REJECTED, "the proposal was rejected")
            if record.expired or record.status == "expired":
                # A budget proposal built on Tuesday's data is void by Friday;
                # the system re-derives rather than executing stale intent.
                return deny(
                    DenialReason.APPROVAL_EXPIRED,
                    "approval has expired; re-derive the proposal from fresh state",
                )
            if record.status not in ("approved", "modified"):
                return deny(
                    DenialReason.APPROVAL_EXPIRED,
                    f"approval is {record.status!r}, not approved",
                )

            # The approval must authorise THIS request. Checking only that it
            # exists and is approved makes it a bearer token: any valid id
            # would execute any mutation, in any workspace the caller reaches.
            authorised = (record.proposed or {}).get("authorisation")
            if authorised is None:
                return deny(
                    DenialReason.APPROVAL_MISMATCH,
                    "approval carries no authorisation binding; it cannot be redeemed",
                )
            presented = request.authorisation_fingerprint()
            if authorised != presented:
                differing = sorted(
                    k for k in set(authorised) | set(presented)
                    if authorised.get(k) != presented.get(k)
                )
                return deny(
                    DenialReason.APPROVAL_MISMATCH,
                    "approval does not authorise this action; it differs on: "
                    + ", ".join(differing),
                )
        steps.append(6)

        idempotency_key = request.derive_idempotency_key()
        before_state = target.state() if target else None

        # -- 8. Concurrency lock, taken BEFORE step 7 -----------------------
        #
        # The numbering is the PRD's and is kept; the ORDER is not, and the
        # swap is the point.
        #
        # Step 5 evaluated the caps against a snapshot read at step 1, outside
        # any lock. Two runs arriving together both read the same pre-mutation
        # committed spend, both project their own delta onto it, both pass a cap
        # the code calls absolute, and then serialise harmlessly at step 8 -
        # where the lock protects the WRITE and no longer protects the DECISION
        # that permitted it. Reproduced: two sequential activations against one
        # cap both executed, and the account ended over its ceiling with no
        # guardrail event recorded, because from each run's point of view
        # nothing had been breached.
        #
        # So the authoritative evaluation now happens while holding the lock,
        # and the step-5 pass stays where it is as a cheap early rejection - a
        # run that cannot possibly fit should not queue behind the lock to find
        # that out.
        #
        # TWO locks, in a fixed order, because the caps are workspace-scoped and
        # the entity serialisation is account-scoped:
        #
        #   * the workspace lock is what makes the cap arithmetic atomic. With
        #     only the per-account lock, a workspace holding three ad accounts
        #     runs three concurrent mutations against three different locks and
        #     the shared daily cap is checked three times against the same
        #     figure.
        #   * the account lock is what stops two runs mutating the same entity.
        #
        # Always workspace first, then account. A consistent order is the whole
        # of the deadlock argument, and it is cheap to keep because there is
        # exactly one place that takes them.
        lock_ctx: ContextManager[bool]
        if tool in META_MUTATING_TOOLS:
            lock_ctx = self._mutation_locks(request)
        else:
            lock_ctx = nullcontext(True)

        with lock_ctx as acquired:
            if not acquired:
                # A run that cannot take the lock does not queue indefinitely:
                # the state it reasoned about may no longer exist, so it
                # re-derives afterwards (PRD 10.9).
                return deny(
                    DenialReason.LOCK_TIMEOUT,
                    f"could not acquire the mutation lock for ad account "
                    f"{request.ad_account_id} within {self._lock_timeout_s}s; re-derive and retry",
                )
            steps.append(8)

            # -- 7. Authoritative re-check, inside the lock -----------------
            #
            # Runs for EVERY mutating tool, not only the CRITICAL ones.
            # `requires_freshness_recheck` still decides whether a stale
            # APPROVAL needs re-validating - that is about the gap between a
            # human saying yes and the action running. This is about the gap
            # between two concurrent runs, which exists at every risk class:
            # eleven MEDIUM budget increases racing one another add up to the
            # same overspend as one CRITICAL activation.
            if tool in META_MUTATING_TOOLS:
                fresh_wp = self._policy.workspace_policy(request.workspace_id)
                if fresh_wp.is_paused:
                    return ToolOutcome(
                        decision=Decision.HALTED,
                        tool=tool,
                        risk=risk,
                        reason=DenialReason.WORKSPACE_PAUSED,
                        message="workspace was frozen between approval and execution",
                        steps_completed=steps,
                    )
                if fresh_wp.access_mode != "full":
                    return deny(
                        DenialReason.ACCESS_READ_ONLY,
                        "organisation access changed between approval and execution",
                    )
                fresh = self._guardrails(tool, request, fresh_wp, target, live_children_inr)
                if fresh:
                    for b in fresh:
                        self._audit.guardrail(
                            workspace_id=request.workspace_id, breach=b, action_taken="halted"
                        )
                    return deny(
                        DenialReason.GUARDRAIL_BREACH,
                        "guardrail breached between approval and execution: "
                        + "; ".join(b.message for b in fresh),
                        fresh,
                    )
            steps.append(7)

            # -- 9. Audit (pre) ---------------------------------------------
            try:
                audit_id = self._audit.pre(
                    workspace_id=request.workspace_id,
                    agent=agent.name,
                    tool=tool,
                    risk=risk,
                    idempotency_key=idempotency_key,
                    params=request.params,
                    decision_id=request.decision_id,
                    approval_id=request.approval_id,
                    policy_decision_id=policy_decision_id,
                )
            except AlreadyExecuted as exc:
                # Deliberately NOT followed by self._audit.post().
                #
                # There is no new outcome to record - the outcome on file is the
                # one that actually happened - and posting would overwrite the
                # first execution's after_state, verification and rollback handle
                # with this run's empty ones. That is the same erasure 7460446
                # fixed for failed retries, arriving by a different door.
                outcome = ToolOutcome(
                    decision=Decision.DENIED,
                    tool=tool,
                    risk=risk,
                    reason=DenialReason.ALREADY_EXECUTED,
                    message=str(exc),
                    audit_id=exc.action_id,
                    idempotency_key=idempotency_key,
                    steps_completed=steps,
                )
                return outcome
            steps.append(9)

            # -- 10. Driver call --------------------------------------------
            try:
                result = self._execute(tool, request, idempotency_key)
            except WriteForbidden as exc:
                outcome = deny(DenialReason.CONNECTION_NOT_WRITABLE, exc.message)
                outcome.audit_id = audit_id
                outcome.idempotency_key = idempotency_key
                self._audit.post(audit_id=audit_id, outcome=outcome)
                return outcome
            except MetaError as exc:
                outcome = ToolOutcome(
                    decision=Decision.DENIED,
                    tool=tool,
                    risk=risk,
                    reason=DenialReason.DRIVER_ERROR,
                    message=f"{exc.kind.value}: {exc.message}",
                    audit_id=audit_id,
                    idempotency_key=idempotency_key,
                    external_request_id=exc.external_request_id,
                    steps_completed=steps,
                )
                self._audit.post(audit_id=audit_id, outcome=outcome)
                return outcome
            steps.append(10)

            entity = result.entity

            # -- 11 & 12. Verification read and state diff ------------------
            verified = True
            diff: dict[str, Any] | None = None
            if requires_verification(tool):
                readback = self._driver.get_entity(request.ad_account_id, entity.id)
                steps.append(11)
                if readback is None:
                    verified, diff = False, {"error": "entity not readable after write"}
                else:
                    diff = self._diff(self._expected_state(tool, request, entity), readback.state())
                    verified = not diff
                    entity = readback
                steps.append(12)

                if not verified:
                    # Silent partial execution is the worst failure class in
                    # this product (PRD 10.9): never report success unverified.
                    # The write has already landed on Meta, so refusing is not
                    # enough - it has to be undone, and the report has to say
                    # whether undoing it worked.
                    handle = self._rollback_handle(tool, request, before_state, entity)
                    rolled_back, rollback_error = self._execute_rollback(
                        handle, idempotency_key
                    )
                    if rolled_back:
                        message = (
                            "written state does not match the proposal; rolled back"
                        )
                    else:
                        message = (
                            "written state does not match the proposal and the "
                            f"rollback did not succeed ({rollback_error}). The object "
                            "is live on Meta in a state nobody approved and needs a "
                            "human now."
                        )
                    outcome = ToolOutcome(
                        decision=Decision.DENIED,
                        tool=tool,
                        risk=risk,
                        reason=DenialReason.VERIFICATION_MISMATCH,
                        message=message,
                        entity=entity,
                        before_state=before_state,
                        after_state=entity.state(),
                        verified=False,
                        verification_diff=diff,
                        # Carried even on success, so a human can repeat or
                        # check it. Withholding it left the caller with a live
                        # object and no documented way back.
                        rollback_handle=handle,
                        rolled_back=rolled_back,
                        rollback_error=rollback_error,
                        audit_id=audit_id,
                        idempotency_key=idempotency_key,
                        steps_completed=steps,
                    )
                    self._audit.post(audit_id=audit_id, outcome=outcome)
                    return outcome

            # -- 13. Audit (post) -------------------------------------------
            outcome = ToolOutcome(
                decision=Decision.EXECUTED,
                tool=tool,
                risk=risk,
                entity=entity,
                before_state=before_state,
                after_state=entity.state(),
                verified=verified,
                verification_diff=diff,
                rollback_handle=self._rollback_handle(tool, request, before_state, entity),
                approval_id=request.approval_id,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                external_request_id=result.external_request_id,
                steps_completed=steps,
            )
            self._audit.post(audit_id=audit_id, outcome=outcome)
            steps.append(13)

        # -- 14. Lock released by the context manager -----------------------
        steps.append(14)

        # -- 15. Learning event ---------------------------------------------
        if requires_outcome_check(tool) and self._scheduler and request.decision_id:
            self._scheduler.queue(
                decision_id=request.decision_id, horizon_days=request.horizon_days
            )
            outcome.outcome_check_queued = True
        steps.append(15)

        outcome.steps_completed = steps
        return outcome

    # -- step 4 ------------------------------------------------------------

    def _validate(self, tool: Tool, request: ToolRequest, target: Entity | None) -> str | None:
        p = request.params

        if tool is Tool.CREATE_CAMPAIGN_DRAFT:
            if not str(p.get("name", "")).strip():
                return "campaign name is required"
            if not str(p.get("objective", "")).strip():
                return "campaign objective is required"

        elif tool is Tool.CREATE_AD_SET_DRAFT:
            if not str(p.get("name", "")).strip():
                return "ad set name is required"
            if not str(p.get("campaign_id", "")).strip():
                return "campaign_id is required"
            budget = p.get("daily_budget_inr")
            if not isinstance(budget, (int, float)) or budget <= 0:
                return f"daily_budget_inr must be a positive number, got {budget!r}"
            if not str(p.get("optimisation_event", "")).strip():
                return "optimisation_event is required"

        elif tool is Tool.UPDATE_BUDGET:
            budget = p.get("daily_budget_inr")
            if not isinstance(budget, (int, float)) or budget <= 0:
                return f"daily_budget_inr must be a positive number, got {budget!r}"
            if target is None:
                return "target_entity_id is required for a budget change"

        elif tool in (Tool.ACTIVATE_ENTITY, Tool.PAUSE_ENTITY):
            if target is None:
                return "target_entity_id is required for a state change"

        return None

    # -- step 5 / 7 --------------------------------------------------------

    @contextmanager
    def _mutation_locks(self, request: ToolRequest) -> Iterator[bool]:
        """The workspace lock, then the account lock. Never the other way round.

        Yields False if either could not be taken within the timeout, so the
        caller's single `if not acquired` branch covers both.
        """
        with self._locks.acquire(
            f"workspace:{request.workspace_id}", self._lock_timeout_s
        ) as workspace_lock:
            if not workspace_lock:
                yield False
                return
            with self._locks.acquire(
                request.ad_account_id, self._lock_timeout_s
            ) as account_lock:
                yield bool(account_lock)

    def _guardrails(
        self,
        tool: Tool,
        request: ToolRequest,
        wp: WorkspacePolicy,
        target: Entity | None,
        live_children_inr: float | None = None,
    ) -> list[GuardrailBreach]:
        breaches: list[GuardrailBreach] = []
        delta = self._spend_delta(tool, request, target)

        if delta is None:
            # An early return, not one more clause. Every financial check below
            # is gated on `delta > 0` and would raise TypeError on None, and
            # stacking four refusals on a single absence says the same thing
            # four times - the reasoning this file already gives for keeping the
            # CAC ceiling silent when the basis is unknown.
            #
            # The remedy named here has to be one that exists. "Sync the
            # account" does not: nothing in this repo writes t_advit.ad_sets,
            # and the captured fixture snapshot carries no budgets at all. What
            # does work is setting the budget explicitly, which is an
            # update_budget through this same pipeline - so the caller is told
            # that instead.
            return [
                GuardrailBreach(
                    guardrail="spend_delta_unknown",
                    guardrail_class="financial",
                    threshold=wp.daily_cap_inr,
                    observed=None,
                    message=(
                        f"Meta reported no daily budget for "
                        f"{request.target_entity_id}, so the rupees this would "
                        f"commit are unknown and the Rs {wp.daily_cap_inr:,.0f} "
                        f"daily and Rs {wp.monthly_cap_inr:,.0f} monthly caps "
                        "cannot be checked. Set the budget explicitly first - an "
                        "activation whose cost nobody can state is not one this "
                        "system will approve."
                    ),
                )
            ]

        if (
            tool is Tool.ACTIVATE_ENTITY
            and target is not None
            and target.level is EntityLevel.CAMPAIGN
            and live_children_inr is None
            and self._has_live_children(request.ad_account_id, target.id)
        ):
            # A campaign fronting live ad sets whose budgets Meta did not report.
            # Distinct from the campaign fronting only drafts, which legitimately
            # commits nothing and legitimately shows a blank impact - and which
            # an approver cannot tell apart from this one, which is the whole
            # reason this refuses rather than passing the blank through.
            return [
                GuardrailBreach(
                    guardrail="impact_unknown",
                    guardrail_class="financial",
                    threshold=wp.daily_cap_inr,
                    observed=None,
                    message=(
                        "this campaign fronts live ad sets whose daily budgets "
                        "Meta did not report, so the money it sets moving cannot "
                        "be stated - and an approval card with a blank figure is "
                        "indistinguishable from one that genuinely commits "
                        "nothing."
                    ),
                )
            ]

        if delta > 0 and not wp.spend_basis_known:
            # Nothing has ever been ingested for this workspace, so committed
            # budget and month-to-date spend are unknown - not zero. Waving the
            # action through would compare a live cap against an assumption and
            # let an account spend past its ceiling on the very first action,
            # which is the failure the cap exists to prevent. This is a data
            # gap, not a zero (PRD 14.1).
            breaches.append(
                GuardrailBreach(
                    guardrail="spend_basis_unknown",
                    guardrail_class="financial",
                    threshold=wp.daily_cap_inr,
                    observed=delta,
                    message=(
                        "no Meta spend has been ingested for this workspace, so "
                        f"the Rs {wp.daily_cap_inr:,.0f} daily and "
                        f"Rs {wp.monthly_cap_inr:,.0f} monthly caps cannot be "
                        "checked. Sync the account before committing spend."
                    ),
                )
            )

        if delta > 0:
            # Caps are absolute. The system halts and asks rather than
            # exceeding them by a rupee (PRD 10.5).
            projected_day = wp.spend_today_inr + delta
            if projected_day > wp.daily_cap_inr:
                breaches.append(
                    GuardrailBreach(
                        guardrail="daily_spend_cap",
                        guardrail_class="financial",
                        threshold=wp.daily_cap_inr,
                        observed=projected_day,
                        message=(
                            f"daily cap Rs {wp.daily_cap_inr:,.0f} would be exceeded: "
                            f"Rs {wp.spend_today_inr:,.0f} committed + Rs {delta:,.0f} "
                            f"requested = Rs {projected_day:,.0f}"
                        ),
                    )
                )

            if not wp.month_basis_known:
                # The month's spend has never been ingested, so there is nothing
                # to project from. Refusing is the only honest answer: a cap
                # compared against an assumed zero is not a cap, and this one
                # governs the larger of the two ceilings.
                breaches.append(
                    GuardrailBreach(
                        guardrail="monthly_basis_unknown",
                        guardrail_class="financial",
                        threshold=wp.monthly_cap_inr,
                        observed=delta,
                        message=(
                            "no Meta spend has been ingested for this month, so the "
                            f"Rs {wp.monthly_cap_inr:,.0f} monthly cap cannot be "
                            "checked. Sync the account before committing spend."
                        ),
                    )
                )

            projected_month = (wp.spend_month_inr or 0.0) + delta
            if wp.spend_month_inr is not None and projected_month > wp.monthly_cap_inr:
                breaches.append(
                    GuardrailBreach(
                        guardrail="monthly_spend_cap",
                        guardrail_class="financial",
                        threshold=wp.monthly_cap_inr,
                        observed=projected_month,
                        message=(
                            f"monthly cap Rs {wp.monthly_cap_inr:,.0f} would be exceeded: "
                            f"projected Rs {projected_month:,.0f}"
                        ),
                    )
                )

        # CAC ceiling. The field was loaded from the workspace and compared
        # against nothing at all - a guardrail that reads as protection and
        # is not one, which is worse than an absent one.
        #
        # It fires only on a MEASURED breach. When the CAC is unknown this
        # guardrail stays silent deliberately: the daily and monthly basis
        # guards above already refuse an account we know nothing about, and
        # stacking a third refusal on the same absence would say the same thing
        # three times while sounding like three problems.
        if (
            delta > 0
            and wp.cac_ceiling_inr is not None
            and wp.recent_cac_inr is not None
            and wp.recent_cac_inr > wp.cac_ceiling_inr
        ):
            breaches.append(
                GuardrailBreach(
                    guardrail="cac_ceiling",
                    guardrail_class="financial",
                    threshold=wp.cac_ceiling_inr,
                    observed=wp.recent_cac_inr,
                    message=(
                        f"blended CAC is Rs {wp.recent_cac_inr:,.0f} against a ceiling "
                        f"of Rs {wp.cac_ceiling_inr:,.0f} derived from your own margin "
                        "and RTO. Committing more spend here buys losses faster "
                        "(PRD 10.3)."
                    ),
                )
            )

        # Per-step budget ceiling. A 15% shift repeated eleven times in a week
        # is a 4x scale-up nobody approved (PRD 10.5, D4).
        if tool is Tool.UPDATE_BUDGET and target is not None:
            reported = target.fields.get("daily_budget_inr")
            requested = float(request.params["daily_budget_inr"])

            if reported is None:
                # `or 0` made `current` zero, and `current > 0` then skipped this
                # ceiling outright - so raising an unknown ad set from Rs 2,000
                # to Rs 50,000 passed a guard whose entire job is to stop a jump
                # that size. The cap still fires (the delta over-estimates in the
                # safe direction), so this is a breach rather than a refusal: it
                # is visible, and it does not block the one path that can also
                # supply the missing number.
                breaches.append(
                    GuardrailBreach(
                        guardrail="budget_step_unknown",
                        guardrail_class="financial",
                        threshold=wp.budget_step_ceiling_pct,
                        observed=None,
                        message=(
                            f"Meta reported no current daily budget for "
                            f"{request.target_entity_id}, so the "
                            f"{wp.budget_step_ceiling_pct:.0f}% per-step ceiling "
                            f"cannot be applied to a move to Rs {requested:,.0f}."
                        ),
                    )
                )

            current = float(reported or 0)
            if current > 0 and requested > current:
                increase_pct = (requested - current) / current * 100.0
                if increase_pct > wp.budget_step_ceiling_pct:
                    breaches.append(
                        GuardrailBreach(
                            guardrail="budget_step_ceiling",
                            guardrail_class="financial",
                            threshold=wp.budget_step_ceiling_pct,
                            observed=increase_pct,
                            message=(
                                f"a {increase_pct:.0f}% increase exceeds the "
                                f"{wp.budget_step_ceiling_pct:.0f}% per-step ceiling; a jump this "
                                "size resets the learning phase and destabilises delivery for "
                                "3-7 days"
                            ),
                        )
                    )
        return breaches

    def _spend_delta(
        self, tool: Tool, request: ToolRequest, target: Entity | None
    ) -> float | None:
        """New daily spend this call commits, or None when it cannot be known.

        Creating a PAUSED object commits nothing - which is precisely why
        paused-first is the default. But `.get("daily_budget_inr") or 0`
        collapsed two different facts into that same nothing: "this entity
        commits no rupees" and "Meta did not tell us what this entity commits".

        The second is the common case for any entity this system did not create
        itself, which is every entity in a real connected account. And because
        `_guardrails` gates all four financial guards behind `delta > 0`, a zero
        that meant "unknown" skipped the daily cap, the monthly cap, the
        spend-basis guard and the CAC ceiling in one step - on "turn the Diwali
        ad set back on", the most ordinary operation an account has.

        Each level is spelled out rather than falling through to a shared
        `return 0.0`, because what an ABSENT budget means depends entirely on
        which level is missing it.
        """
        if tool is Tool.UPDATE_BUDGET and target is not None:
            # An unknown CURRENT budget can only over-estimate the delta, and
            # over-estimating is the safe direction for a cap - it errs toward
            # the guard firing. The step ceiling is the one that suffers, and it
            # is handled where it is evaluated rather than by refusing here.
            current = float(target.fields.get("daily_budget_inr") or 0)
            return max(0.0, float(request.params["daily_budget_inr"]) - current)

        if tool is Tool.ACTIVATE_ENTITY and target is not None:
            budget = target.fields.get("daily_budget_inr")

            if target.level is EntityLevel.AD_SET:
                # The level that is SUPPOSED to carry the number. Absent here is
                # a gap in what we know, not a fact about the ad set.
                return None if budget is None else float(budget)

            if target.level is EntityLevel.CAMPAIGN:
                # Under CBO the campaign holds the budget and activating it
                # commits that money directly. Returning 0.0 unconditionally -
                # as an earlier draft of this fix did - would discard a budget
                # that is right there in the response.
                if budget is not None:
                    return float(budget)
                # Under ABO the campaign has no budget of its own, and that
                # absence is a fact about the level rather than a gap. Its
                # rupees are its children's, and each was counted when THAT
                # child was activated; counting them again here would
                # double-count against the daily cap, which
                # test_activating_a_campaign_does_not_double_count_against_the_cap
                # exists to prevent.
                return 0.0

            # An ad. The budget lives on its ad set, which was counted when the
            # ad set was activated. Stated separately rather than sharing the
            # campaign's branch: the number is the same and the reason is not.
            return 0.0

        return 0.0

    def _impact(
        self,
        tool: Tool,
        request: ToolRequest,
        target: Entity | None,
        live_children_inr: float | None = None,
    ) -> float | None:
        """What the human is being asked to approve, in rupees per day.

        Deliberately NOT _spend_delta, though it was until an audit caught it.
        The two answer different questions:

          * _spend_delta is NEW commitment, for cap arithmetic. Activating a
            campaign adds nothing - each of its ad sets was already counted when
            THAT ad set was activated, and counting them again here would
            double-count against the daily cap.
          * impact is the money that starts MOVING because of this action. For a
            campaign that is the sum of its live children, and it is precisely
            what the approver needs to see.

        Conflating them made the approval card lie in the most consequential
        direction available: "activate this campaign" was presented with no
        rupee impact at all, while the campaign stood in front of ad sets ready
        to spend the moment it went live.
        """
        delta = self._spend_delta(tool, request, target)
        if delta:
            return delta

        if (
            tool is Tool.ACTIVATE_ENTITY
            and target is not None
            and target.level is EntityLevel.CAMPAIGN
        ):
            # Already read in `invoke`; None here means either "nothing live" or
            # "a live child would not say", and `_guardrails` has already
            # refused the second case before any approval card is written.
            return live_children_inr or None

        return None

    def _has_live_children(self, ad_account_id: str, campaign_id: str) -> bool:
        """Whether anything behind this campaign is already ACTIVE.

        Exists to separate the two campaign cases that both arrive as
        `live_children_inr is None`: "nothing is live" (a real zero, which must
        still reach an approver) and "something is live and would not say what
        it costs" (a refusal). Without it the fix would refuse every campaign
        activation, including the paused-first default the product is built on.
        """
        children = self._driver.get_entities(
            ad_account_id, EntityLevel.AD_SET, parent_id=campaign_id
        )
        return any(c.status is EntityStatus.ACTIVE for c in children)

    def _live_child_budget(self, ad_account_id: str, campaign_id: str) -> float | None:
        """Daily budget of the ad sets that begin spending when this campaign
        goes live, or None when any of them will not say.

        Paused children are excluded, which is the whole point of paused-first:
        a campaign fronting nothing but drafts genuinely commits nothing, and
        saying otherwise would train approvers to ignore the number.

        None and 0.0 are different answers and the caller must keep them apart.
        0.0 is "nothing live behind this campaign". None is "something is live
        and we cannot say what it costs", which must not be shown to an approver
        as a blank card - blank is what the genuine zero looks like.

        Called ONCE per invocation, from `invoke` where the target is resolved,
        rather than from `_guardrails` and `_impact` separately. `_guardrails`
        runs twice (step 5 and again at step 7 after approval) and neither call
        site is inside an `except MetaError`, so reading the driver from in
        there turned one campaign activation into as many as five reads and
        turned a Meta hiccup into a 500 rather than a DENIED.
        """
        children = self._driver.get_entities(
            ad_account_id, EntityLevel.AD_SET, parent_id=campaign_id
        )
        live = [c for c in children if c.status is EntityStatus.ACTIVE]
        if any(c.fields.get("daily_budget_inr") is None for c in live):
            return None
        return sum(float(c.fields["daily_budget_inr"]) for c in live)

    # -- step 10 -----------------------------------------------------------

    def _execute_read(self, tool: Tool, request: ToolRequest) -> Any:
        acct = request.ad_account_id
        p = request.params

        if tool is Tool.READ_ENTITY:
            return self._driver.get_entity(acct, request.target_entity_id)
        if tool is Tool.READ_ENTITIES:
            level = EntityLevel(p.get("level", EntityLevel.CAMPAIGN.value))
            return self._driver.get_entities(acct, level, parent_id=p.get("parent_id"))
        if tool is Tool.READ_DATASETS:
            return self._driver.get_datasets(acct)
        if tool is Tool.READ_ACCOUNT:
            getter = getattr(self._driver, "get_account", None)
            return getter(acct) if getter else None

        raise MetaError(
            MetaErrorKind.VALIDATION,
            f"read tool {tool.value} has no binding in this driver",
        )

    def _execute(self, tool: Tool, request: ToolRequest, idempotency_key: str):
        p = request.params
        acct = request.ad_account_id

        if tool is Tool.CREATE_CAMPAIGN_DRAFT:
            return self._driver.create_campaign(
                acct,
                name=p["name"],
                objective=p["objective"],
                idempotency_key=idempotency_key,
            )
        if tool is Tool.CREATE_AD_SET_DRAFT:
            return self._driver.create_ad_set(
                acct,
                campaign_id=p["campaign_id"],
                name=p["name"],
                daily_budget_inr=float(p["daily_budget_inr"]),
                optimisation_event=p["optimisation_event"],
                idempotency_key=idempotency_key,
            )
        if tool is Tool.ACTIVATE_ENTITY:
            return self._driver.update_status(
                acct,
                request.target_entity_id,
                EntityStatus.ACTIVE,
                idempotency_key=idempotency_key,
            )
        if tool is Tool.PAUSE_ENTITY:
            return self._driver.update_status(
                acct,
                request.target_entity_id,
                EntityStatus.PAUSED,
                idempotency_key=idempotency_key,
            )
        if tool is Tool.UPDATE_BUDGET:
            return self._driver.update_budget(
                acct,
                request.target_entity_id,
                float(p["daily_budget_inr"]),
                idempotency_key=idempotency_key,
            )
        raise MetaError(
            MetaErrorKind.VALIDATION,
            f"tool {tool.value} has no execution binding in this driver",
        )

    # -- steps 11-13 -------------------------------------------------------

    def _expected_state(
        self, tool: Tool, request: ToolRequest, entity: Entity
    ) -> dict[str, Any]:
        """What the proposal said the world should look like afterwards.

        Only the fields this call claims to set are asserted; anything Meta
        owns is left out, so the diff catches real divergence rather than
        noise.
        """
        p = request.params
        if tool is Tool.CREATE_CAMPAIGN_DRAFT:
            return {"name": p["name"], "status": EntityStatus.PAUSED.db}
        if tool is Tool.CREATE_AD_SET_DRAFT:
            return {
                "name": p["name"],
                "status": EntityStatus.PAUSED.db,
                "daily_budget_inr": float(p["daily_budget_inr"]),
            }
        if tool is Tool.ACTIVATE_ENTITY:
            return {"status": EntityStatus.ACTIVE.db}
        if tool is Tool.PAUSE_ENTITY:
            return {"status": EntityStatus.PAUSED.db}
        if tool is Tool.UPDATE_BUDGET:
            return {"daily_budget_inr": float(p["daily_budget_inr"])}
        return {}

    @staticmethod
    def _diff(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, want in expected.items():
            got = actual.get(key)
            if isinstance(want, float) or isinstance(got, float):
                try:
                    if abs(float(want) - float(got)) > 0.005:
                        out[key] = {"expected": want, "actual": got}
                    continue
                except (TypeError, ValueError):
                    pass
            if got != want:
                out[key] = {"expected": want, "actual": got}
        return out

    def _execute_rollback(
        self, handle: dict[str, Any] | None, idempotency_key: str | None
    ) -> tuple[bool, str | None]:
        """Undo a write that failed verification.

        A rollback that itself fails is reported, never swallowed: the caller
        has a live object on Meta in a state nobody approved, and the only
        thing worse than saying so is not saying so.
        """
        if not handle:
            return False, "no rollback is defined for this tool"

        account = handle.get("ad_account_id")
        entity_id = handle.get("entity_id")
        kind = handle.get("kind")
        # A distinct key: the rollback is its own write, and reusing the failed
        # call's key would let a deduplicating driver discard it.
        key = f"{idempotency_key or entity_id}:rollback"

        try:
            if kind == "pause_created_entity":
                self._driver.update_status(
                    account, entity_id, EntityStatus.PAUSED, idempotency_key=key
                )
            elif kind == "restore_status":
                status = handle.get("status")
                if not status:
                    return False, "the prior status was not captured"
                self._driver.update_status(
                    account, entity_id, EntityStatus(status), idempotency_key=key
                )
            elif kind == "restore_budget":
                budget = handle.get("daily_budget_inr")
                if budget is None:
                    return False, "the prior budget was not captured"
                self._driver.update_budget(
                    account, entity_id, float(budget), idempotency_key=key
                )
            else:
                return False, f"unknown rollback kind {kind!r}"
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            return False, f"{type(exc).__name__}: {exc}"

        return True, None

    def _rollback_handle(
        self,
        tool: Tool,
        request: ToolRequest,
        before_state: dict[str, Any] | None,
        entity: Entity,
    ) -> dict[str, Any] | None:
        """Rollback is a first-class action with a visible time limit
        (PRD 10.6). A created object is reverted by pausing it - it was created
        paused, so there is nothing to undo unless it was later activated."""
        if tool in (Tool.CREATE_CAMPAIGN_DRAFT, Tool.CREATE_AD_SET_DRAFT):
            return {
                "kind": "pause_created_entity",
                "entity_id": entity.id,
                "ad_account_id": request.ad_account_id,
            }
        if tool is Tool.ACTIVATE_ENTITY:
            return {
                "kind": "restore_status",
                "entity_id": entity.id,
                "ad_account_id": request.ad_account_id,
                "status": EntityStatus.PAUSED.db,
            }
        if tool is Tool.PAUSE_ENTITY and before_state:
            return {
                "kind": "restore_status",
                "entity_id": entity.id,
                "ad_account_id": request.ad_account_id,
                "status": before_state.get("status"),
            }
        if tool is Tool.UPDATE_BUDGET and before_state:
            return {
                "kind": "restore_budget",
                "entity_id": entity.id,
                "ad_account_id": request.ad_account_id,
                "daily_budget_inr": before_state.get("daily_budget_inr"),
            }
        return None
