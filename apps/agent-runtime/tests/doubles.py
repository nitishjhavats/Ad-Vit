"""In-memory implementations of the pipeline's ports.

Deliberately faithful rather than trivial: the audit sink really records
ordering, the lock really excludes, and approvals really expire. A double that
always says yes would leave the steps it stands in for untested.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.policy.pipeline import (
    ApprovalRecord,
    ConnectionPolicy,
    GuardrailBreach,
    RiskClass,
    ToolOutcome,
    WorkspacePolicy,
)
from app.policy.risk import Tool

WORKSPACE = "00000000-0000-4000-8000-000000000050"
ORG = "00000000-0000-4000-8000-000000000010"
WRITABLE_ACCOUNT = "1000000000000003"   # no payment method attached
FUNDED_ACCOUNT = "1000000000000001"     # Demo Brand call ads, read-only


def workspace_policy(**overrides: Any) -> WorkspacePolicy:
    base: dict[str, Any] = dict(
        workspace_id=WORKSPACE,
        org_id=ORG,
        access_mode="full",
        effective_autonomy=1,
        daily_cap_inr=5000.0,
        monthly_cap_inr=150000.0,
        cac_ceiling_inr=900.0,
        is_paused=False,
        spend_basis_known=True,
        # The default double is a synced account, because that is what every
        # real workspace is within a day of connecting. Tests about the
        # unsynced state say so explicitly.
        month_basis_known=True,
        spend_today_inr=0.0,
        spend_month_inr=0.0,
        budget_step_ceiling_pct=20.0,
    )
    base.update(overrides)
    return WorkspacePolicy(**base)


class FakePolicyStore:
    def __init__(
        self,
        policy: WorkspacePolicy | None = None,
        *,
        connections: dict[str, bool] | None = None,
    ) -> None:
        self._policy = policy or workspace_policy()
        # ad_account_id -> write_enabled. Mirrors the seed: the funded accounts
        # are connected read-only.
        self._connections = connections or {
            WRITABLE_ACCOUNT: True,
            FUNDED_ACCOUNT: False,
        }
        self.approvals: dict[str, ApprovalRecord] = {}
        self.approval_workspace: dict[str, str] = {}
        self.created_approvals: list[dict[str, Any]] = []
        self._fresh_policy: WorkspacePolicy | None = None
        self.policy_reads = 0

    # Lets a test change the world between approval and execution, which is
    # exactly what step 7 exists to catch.
    def set_fresh_policy(self, policy: WorkspacePolicy) -> None:
        self._fresh_policy = policy

    def set_policy(self, policy: WorkspacePolicy) -> None:
        """Change what every subsequent read reports.

        Distinct from `set_fresh_policy`, which fires only from the second read
        onwards: this is for a test simulating the world moving on between two
        separate runs, where each run's first read must already see the change.
        """
        self._policy = policy
        self._fresh_policy = None

    def workspace_policy(self, workspace_id: str) -> WorkspacePolicy:
        self.policy_reads += 1
        if self._fresh_policy is not None and self.policy_reads > 1:
            return self._fresh_policy
        return self._policy

    def connection_policy(self, workspace_id: str, ad_account_id: str) -> ConnectionPolicy | None:
        if ad_account_id not in self._connections:
            return None
        return ConnectionPolicy(
            ad_account_id=ad_account_id,
            write_enabled=self._connections[ad_account_id],
            health="unknown",
        )

    def approval(self, approval_id: str, workspace_id: str) -> ApprovalRecord | None:
        # Scoped like the real store: a cross-tenant approval is invisible.
        record = self.approvals.get(approval_id)
        if record is None or self.approval_workspace.get(approval_id) != workspace_id:
            return None
        return record

    def create_approval(
        self, *, workspace_id: str, decision_id: str, risk: RiskClass,
        proposed: dict[str, Any], impact_inr: float | None,
    ) -> str:
        approval_id = f"appr-{uuid.uuid4()}"
        self.approvals[approval_id] = ApprovalRecord(
            id=approval_id, status="pending", expired=False, proposed=proposed
        )
        self.approval_workspace[approval_id] = workspace_id
        self.created_approvals.append(
            {
                "id": approval_id,
                "risk": risk,
                "proposed": proposed,
                "impact_inr": impact_inr,
                "decision_id": decision_id,
            }
        )
        return approval_id

    def grant(self, approval_id: str, status: str = "approved") -> None:
        prior = self.approvals[approval_id]
        self.approvals[approval_id] = ApprovalRecord(
            id=approval_id, status=status, expired=False, proposed=prior.proposed
        )

    def expire(self, approval_id: str) -> None:
        prior = self.approvals[approval_id]
        self.approvals[approval_id] = ApprovalRecord(
            id=approval_id, status="approved", expired=True, proposed=prior.proposed
        )

    def reject(self, approval_id: str) -> None:
        prior = self.approvals[approval_id]
        self.approvals[approval_id] = ApprovalRecord(
            id=approval_id, status="rejected", expired=False, proposed=prior.proposed
        )


@dataclass
class AuditEntry:
    audit_id: str
    workspace_id: str
    agent: str
    tool: Tool
    risk: RiskClass
    idempotency_key: str
    params: dict[str, Any]
    policy_decision_id: str
    approval_id: str | None
    outcome: ToolOutcome | None = None


class FakeAuditSink:
    def __init__(self) -> None:
        self.entries: dict[str, AuditEntry] = {}
        self.order: list[str] = []
        self.guardrail_events: list[dict[str, Any]] = []

    def pre(
        self, *, workspace_id: str, agent: str, tool: Tool, risk: RiskClass,
        idempotency_key: str, params: dict[str, Any], decision_id: str | None,
        approval_id: str | None, policy_decision_id: str,
    ) -> str:
        audit_id = f"audit-{len(self.entries) + 1}"
        self.entries[audit_id] = AuditEntry(
            audit_id=audit_id,
            workspace_id=workspace_id,
            agent=agent,
            tool=tool,
            risk=risk,
            idempotency_key=idempotency_key,
            params=dict(params),
            policy_decision_id=policy_decision_id,
            approval_id=approval_id,
        )
        self.order.append(f"pre:{audit_id}")
        return audit_id

    def post(self, *, audit_id: str, outcome: ToolOutcome) -> None:
        self.entries[audit_id].outcome = outcome
        self.order.append(f"post:{audit_id}")

    def guardrail(self, *, workspace_id: str, breach: GuardrailBreach, action_taken: str) -> None:
        self.guardrail_events.append(
            {"workspace_id": workspace_id, "breach": breach, "action_taken": action_taken}
        )


class FakeLockManager:
    """Per-ad-account mutation lock with a real mutex, so a test can prove that
    two concurrent runs do not both write."""

    def __init__(self, *, always_fail: bool = False) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._always_fail = always_fail
        self.acquisitions: list[str] = []
        self.held: set[str] = set()

    @contextmanager
    def acquire(self, ad_account_id: str, timeout_s: int) -> Iterator[bool]:
        if self._always_fail:
            yield False
            return
        lock = self._locks.setdefault(ad_account_id, threading.Lock())
        got = lock.acquire(timeout=timeout_s)
        if got:
            self.acquisitions.append(ad_account_id)
            self.held.add(ad_account_id)
        try:
            yield got
        finally:
            if got:
                self.held.discard(ad_account_id)
                lock.release()


class FakeScheduler:
    def __init__(self) -> None:
        self.queued: list[dict[str, Any]] = []

    def queue(self, *, decision_id: str, horizon_days: int) -> None:
        self.queued.append({"decision_id": decision_id, "horizon_days": horizon_days})


# The Media Buying agent is the only agent with write access to Meta (PRD 7.3).
MEDIA_BUYING_TOOLS = frozenset(
    {
        Tool.READ_ACCOUNT,
        Tool.READ_ENTITIES,
        Tool.READ_ENTITY,
        Tool.CREATE_CAMPAIGN_DRAFT,
        Tool.CREATE_AD_SET_DRAFT,
        Tool.PAUSE_ENTITY,
        Tool.ACTIVATE_ENTITY,
        Tool.UPDATE_BUDGET,
    }
)

ANALYTICS_TOOLS = frozenset(
    {Tool.READ_ACCOUNT, Tool.READ_ENTITIES, Tool.READ_ENTITY, Tool.READ_INSIGHTS}
)
