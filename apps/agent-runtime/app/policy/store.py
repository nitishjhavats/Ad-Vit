"""Postgres-backed implementations of the pipeline's ports.

These are the adapters that make the tool pipeline real. Two properties matter
more than anything else here:

* Authorization reads only from the database. ``workspace_policy`` calls
  ``core.access_mode`` and ``t_advit.effective_autonomy`` rather than
  recomputing them in Python, so the plan cap and the autonomy ladder cannot
  drift between the SQL definition and this code.

* The pre-call audit row is committed before the driver is invoked, in its own
  transaction. If the process dies mid-write, the retry path has a record to
  consult (PRD 10.9). An audit row written in the same transaction as the
  mutation would vanish along with it, which defeats the purpose.
"""

from __future__ import annotations

import json
import time
import zlib
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from app.db.pools import service_conn, service_dsn
from app.policy.pipeline import (
    AlreadyExecuted,
    ApprovalRecord,
    ConnectionPolicy,
    GuardrailBreach,
    RiskClass,
    ToolOutcome,
    WorkspacePolicy,
)
from app.policy.risk import Tool


def _advisory_key(ad_account_id: str) -> int:
    """Stable 32-bit key for a per-ad-account advisory lock."""
    return zlib.crc32(str(ad_account_id).encode("utf-8")) - 2**31


class PostgresPolicyStore:
    """Every read here is guardrail arithmetic, so every read is on the service
    connection.

    Not a convenience. ``workspace_policy`` sums committed budgets across
    ``ad_sets`` and ``actions``; under RLS a row the caller cannot see is
    ABSENT, and absent sums to zero. A cap that gets smaller when a less
    privileged user asks is not a cap.
    """

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        with service_conn() as conn:
            yield conn

    def workspace_policy(self, workspace_id: str) -> WorkspacePolicy:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                with acted as (
                  -- The latest verified, un-rolled-back budget this system set
                  -- on each entity today, in IST because that is the day the
                  -- cap is expressed in. One row per entity: a later budget
                  -- change supersedes an earlier one rather than adding to it.
                  select distinct on (a.after_state_json->>'id')
                         a.after_state_json->>'id'                        as entity_id,
                         (a.after_state_json->>'daily_budget_inr')::numeric as budget_inr
                    from t_advit.actions a
                   where a.workspace_id = %(workspace)s::uuid
                     and a.verified
                     and a.rolled_back_at is null
                     and a.executed_at >=
                         date_trunc('day', now() at time zone 'Asia/Kolkata')
                           at time zone 'Asia/Kolkata'
                     and a.action_type in ('activate_entity', 'update_budget')
                     and a.after_state_json ? 'daily_budget_inr'
                     -- A budget set on something left paused commits nothing.
                     --
                     -- Entity.state() now writes EntityStatus.db, which is the
                     -- t_advit.entity_status vocabulary, so this comparison is
                     -- exact by construction. lower() stays as tolerance for
                     -- rows written before that normalisation existed - they
                     -- carry Meta's upper case, and a guardrail that silently
                     -- stops counting historical actions is the failure this
                     -- whole CTE was added to fix.
                     and lower(a.after_state_json->>'status') = 'active'
                   order by a.after_state_json->>'id', a.executed_at desc
                )
                select
                  w.id::text                                as workspace_id,
                  w.org_id::text                             as org_id,
                  core.access_mode(w.org_id, t_advit.product_id())::text
                                                            as access_mode,
                  t_advit.effective_autonomy(w.id)         as effective_autonomy,
                  w.daily_cap_inr,
                  w.monthly_cap_inr,
                  w.cac_ceiling_inr,
                  w.is_paused,
                  -- Committed daily spend, not spend already incurred: the
                  -- question a cap check answers is "would activating this
                  -- take the account past its ceiling today", and an ad set
                  -- that is live but has not spent yet still commits its
                  -- budget.
                  --
                  -- Two sources, unioned per entity rather than added:
                  --
                  --   * t_advit.ad_sets is what Meta last told us, and is
                  --     the only source for ad sets this system never touched.
                  --     But it only changes when a sync runs, so between syncs
                  --     it is stale - and a stale figure meant eleven
                  --     sequential activations against one cap all passed,
                  --     which is precisely the repeated-small-step scale-up
                  --     PRD D4 exists to prevent.
                  --
                  --   * t_advit.actions is what THIS system did today, and
                  --     is authoritative the moment it executes, sync or no
                  --     sync.
                  --
                  -- Adding them would double-count an entity we activated and
                  -- Meta has since confirmed. Taking the larger would drop our
                  -- own action whenever other ad sets outweigh it. So each
                  -- entity is counted once, preferring our own fresher record.
                  coalesce((
                    select sum(s.budget_inr)
                      from t_advit.ad_sets s
                     where s.workspace_id = w.id
                       and s.status = 'active'
                       -- NOT EXISTS, never NOT IN: meta_id is null for a draft
                       -- that has not synced, and `x not in (..., null)` is
                       -- never true, which would silently zero this term.
                       and not exists (
                         select 1 from acted a
                          where a.entity_id = s.meta_id
                       )
                  ), 0)
                  + coalesce((select sum(a.budget_inr) from acted a), 0)
                                                             as committed_daily_inr,
                  -- No coalesce, and the month boundary is the WORKSPACE's,
                  -- not the server's.
                  --
                  -- `current_date` is UTC here; t_advit.workspaces.timezone
                  -- exists, defaults to Asia/Kolkata, and was read by nothing.
                  -- For the first five and a half hours of every IST day the
                  -- two disagree, and on the 1st of a month that disagreement
                  -- is a whole month of spend counted or missed.
                  (
                    select sum(m.spend_inr)
                      from t_advit.metrics_daily m
                     where m.workspace_id = w.id
                       and m.level = 'account'
                       and m.date >= date_trunc(
                             'month', (now() at time zone w.timezone)::date)
                  )                                          as spend_month_inr,
                  -- Distinct from spend_basis_known below, and the distinction
                  -- matters. That flag is satisfied by a completed connection
                  -- check, which says we have enumerated the account - not that
                  -- we hold its spend history. Today every workspace satisfies
                  -- it while t_advit.metrics_daily is empty, so the monthly
                  -- cap has been comparing against a confident zero.
                  exists (
                    select 1
                      from t_advit.metrics_daily m
                     where m.workspace_id = w.id
                       and m.level = 'account'
                       and m.date >= date_trunc(
                             'month', (now() at time zone w.timezone)::date)
                  )                                          as month_basis_known,
                  -- The most recent MEASURED blended CAC, for the ceiling
                  -- guardrail below. Restricted to the last 7 days because a
                  -- month-old CAC says nothing about today's delivery, and
                  -- to non-NULL rows because blended_daily now distinguishes
                  -- "we measured this" from "we could not".
                  (
                    select b.blended_cac_inr
                      from t_advit.blended_daily b
                     where b.workspace_id = w.id
                       and b.blended_cac_inr is not null
                       and b.date >= (now() at time zone w.timezone)::date - 7
                     order by b.date desc
                     limit 1
                  )                                          as recent_cac_inr,
                  -- Have we ever actually read this account from Meta? Both
                  -- sums above coalesce to zero, so without this a
                  -- never-synced workspace is indistinguishable from one that
                  -- has spent nothing - and the caps would be compared against
                  -- an assumption.
                  --
                  -- Either signal is enough: a completed connection check means
                  -- we have enumerated the account, and ingested metrics mean
                  -- we have its spend history even if the check has since gone
                  -- stale.
                  (
                    exists (
                      select 1
                        from t_advit.meta_connections c
                       where c.workspace_id = w.id
                         and c.last_checked_at is not null
                    )
                    or exists (
                      select 1
                        from t_advit.metrics_daily m
                       where m.workspace_id = w.id
                    )
                  )                                          as spend_basis_known
                from t_advit.workspaces w
                where w.id = %(workspace)s::uuid
                """,
                {"workspace": workspace_id},
            )
            row = cur.fetchone()

        if row is None:
            raise LookupError(f"workspace {workspace_id} does not exist")

        return WorkspacePolicy(
            workspace_id=row["workspace_id"],
            org_id=row["org_id"],
            access_mode=row["access_mode"],
            # Already min(workspace intent, plan entitlement), floored to L0
            # when paused or not in full access. Never read the raw column.
            effective_autonomy=int(row["effective_autonomy"] or 0),
            daily_cap_inr=float(row["daily_cap_inr"]),
            monthly_cap_inr=float(row["monthly_cap_inr"]),
            cac_ceiling_inr=(
                float(row["cac_ceiling_inr"]) if row["cac_ceiling_inr"] is not None else None
            ),
            is_paused=bool(row["is_paused"]),
            spend_basis_known=bool(row["spend_basis_known"]),
            spend_today_inr=float(row["committed_daily_inr"]),
            month_basis_known=row["month_basis_known"],
            recent_cac_inr=(
                float(row["recent_cac_inr"])
                if row["recent_cac_inr"] is not None
                else None
            ),
            spend_month_inr=(
                float(row["spend_month_inr"])
                if row["spend_month_inr"] is not None
                else None
            ),
        )

    def connection_policy(
        self, workspace_id: str, ad_account_id: str
    ) -> ConnectionPolicy | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select ad_account_id, write_enabled, health::text as health
                  from t_advit.meta_connections
                 where workspace_id = %s and ad_account_id = %s
                """,
                (workspace_id, ad_account_id),
            )
            row = cur.fetchone()

        if row is None:
            return None
        return ConnectionPolicy(
            ad_account_id=row["ad_account_id"],
            write_enabled=bool(row["write_enabled"]),
            health=row["health"],
        )

    def approval(self, approval_id: str, workspace_id: str) -> ApprovalRecord | None:
        with self._conn() as conn, conn.cursor() as cur:
            # Scoped by workspace: an approval belonging to another tenant must
            # not even be readable, let alone redeemable.
            cur.execute(
                """
                select id::text, status::text, proposed_json,
                       (expires_at <= now()) as expired
                  from t_advit.approvals
                 where id = %s and workspace_id = %s
                """,
                (approval_id, workspace_id),
            )
            row = cur.fetchone()

        if row is None:
            return None
        return ApprovalRecord(
            id=row["id"],
            status=row["status"],
            expired=bool(row["expired"]),
            proposed=row["proposed_json"] or {},
        )

    def create_approval(
        self,
        *,
        workspace_id: str,
        decision_id: str,
        risk: RiskClass,
        proposed: dict[str, Any],
        impact_inr: float | None,
        ttl_hours: int = 24,
    ) -> str:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.approvals
                  (decision_id, workspace_id, risk_class, proposed_json,
                   impact_inr, expires_at)
                values (%s, %s, %s::t_advit.risk_class, %s::jsonb, %s,
                        now() + make_interval(hours => %s))
                returning id::text
                """,
                (
                    decision_id,
                    workspace_id,
                    risk.value,
                    json.dumps(proposed),
                    impact_inr,
                    ttl_hours,
                ),
            )
            approval_id = cur.fetchone()["id"]
            conn.commit()
        return approval_id


class PostgresAuditSink:
    """Writes both halves of the record: the governance trail in
    ``core.audit_log`` and the executed-action row in ``t_advit.actions``.

    They are not redundant. The audit log answers "who did this, under whose
    authority"; the actions row carries the idempotency key, the before and
    after state, the verification diff and the rollback handle.
    """

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        with service_conn() as conn:
            yield conn

    def pre(
        self,
        *,
        workspace_id: str,
        agent: str,
        tool: Tool,
        risk: RiskClass,
        idempotency_key: str,
        params: dict[str, Any],
        decision_id: str | None,
        approval_id: str | None,
        policy_decision_id: str,
    ) -> str:
        if decision_id is None:
            raise ValueError(
                "a mutating tool call requires a decision_id: every material action "
                "must be explainable from its evidence (PRD Appendix E)"
            )

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.actions
                  (decision_id, workspace_id, approval_id, action_type, risk_class,
                   meta_request_json, idempotency_key, verified)
                values (%s, %s, %s, %s, %s::t_advit.risk_class, %s::jsonb, %s, false)
                on conflict (idempotency_key) do update
                   set meta_request_json = excluded.meta_request_json
                 -- Only an attempt that never reached the driver may be rebound.
                 --
                 -- Without this predicate the collision resolved to "update the
                 -- row and hand back its id", so a replayed request was given the
                 -- FIRST execution's action row and the pipeline issued the
                 -- mutation again - a second budget change, a second activation -
                 -- while overwriting the original request payload on the way past.
                 --
                 -- A DO UPDATE whose WHERE is false updates nothing and RETURNS
                 -- NOTHING, so `fetchone()` coming back None is the signal that a
                 -- row exists and has already run. That is the branch below.
                 --
                 -- An unexecuted row is still rebound on purpose: that is a retry
                 -- of an attempt that died between this insert and the driver
                 -- call, which is exactly what the idempotency key is for.
                 where t_advit.actions.executed_at is null
                returning id::text
                """,
                (
                    decision_id,
                    workspace_id,
                    approval_id,
                    tool.value,
                    risk.value,
                    json.dumps(params),
                    idempotency_key,
                ),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    select id::text, executed_at, action_type
                      from t_advit.actions
                     where idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                prior = cur.fetchone()
                if prior is None:  # pragma: no cover - would mean the row vanished
                    raise RuntimeError(
                        "the idempotency insert returned no row and no conflicting row "
                        "exists; refusing to guess whether this action has run"
                    )
                raise AlreadyExecuted(
                    f"{prior['action_type']} with this idempotency key already executed "
                    f"at {prior['executed_at']} as action {prior['id']}; not running it "
                    "a second time",
                    action_id=prior["id"],
                    executed_at=prior["executed_at"],
                )

            action_id = row["id"]

            cur.execute(
                """
                select core.log_audit(
                  'workspace', %s, p_workspace => %s,
                  p_actor_type => 'agent', p_actor => null,
                  p_payload => %s::jsonb, p_policy_id => %s, p_approval_id => %s
                )
                """,
                (
                    f"action.intent.{tool.value}",
                    workspace_id,
                    json.dumps(
                        {
                            "agent": agent,
                            "tool": tool.value,
                            "risk_class": risk.value,
                            "idempotency_key": idempotency_key,
                            "action_id": action_id,
                        }
                    ),
                    policy_decision_id,
                    approval_id,
                ),
            )
            # Committed before the driver call, deliberately.
            conn.commit()

        return action_id

    def post(self, *, audit_id: str, outcome: ToolOutcome) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update t_advit.actions
                -- Two kinds of column here, and they must not be treated
                -- alike. meta_response_json, verification_diff and error_json
                -- describe THIS attempt and should reflect the latest one. The
                -- state snapshots and the rollback handle describe an execution
                -- that HAPPENED, and a later attempt must not erase them.
                --
                -- after_state_json in particular is read by workspace_policy to
                -- compute committed spend, so nulling it on a failed retry
                -- silently released a successful activation's budget back to
                -- the daily cap - the same cap that exists to stop exactly that
                -- money being committed twice.
                   set meta_response_json  = %s::jsonb,
                       before_state_json   = coalesce(%s::jsonb, before_state_json),
                       after_state_json    = coalesce(%s::jsonb, after_state_json),
                       -- Guarded on whether THIS post carries an execution,
                       -- not on whether it succeeded. The verification-mismatch
                       -- path posts decision=DENIED while a real write is live
                       -- on Meta, so `ok` is the wrong discriminator - it would
                       -- discard exactly the verdict that matters most. A post
                       -- with no after_state performed nothing and has no
                       -- standing to revise the verification of something that
                       -- did.
                       verified            = case when %s then %s else verified end,
                       verification_diff   = case when %s then %s::jsonb
                                                  else verification_diff end,
                       rollback_handle     = coalesce(%s::jsonb, rollback_handle),
                       -- `else <column>`, not a bare `end`. A CASE with no ELSE
                       -- yields NULL, so these two cleared themselves on any
                       -- post() whose outcome was not ok or carried no rollback
                       -- handle - including a post() on a row that had ALREADY
                       -- executed.
                       --
                       -- pre() upserts on idempotency_key and returns the same
                       -- row for a retry, so a retry that fails after a first
                       -- attempt succeeded erased both the executed marker and
                       -- the rollback window. The change stays live on Meta
                       -- while the trail says it never ran and offers no way
                       -- back - the one claim this product must never make.
                       --
                       -- These are only ever set, never unset. An execution that
                       -- happened is a fact about the past.
                       rollback_expires_at = case when %s then now() + interval '24 hours'
                                                  else rollback_expires_at end,
                       external_request_id = %s,
                       executed_at         = case when %s then now()
                                                  else executed_at end,
                       error_json          = %s::jsonb
                 where id = %s
                returning workspace_id::text
                """,
                (
                    json.dumps({"decision": outcome.decision.value, "message": outcome.message}),
                    json.dumps(outcome.before_state) if outcome.before_state else None,
                    json.dumps(outcome.after_state) if outcome.after_state else None,
                    outcome.after_state is not None,
                    outcome.verified,
                    outcome.after_state is not None,
                    json.dumps(outcome.verification_diff)
                    if outcome.verification_diff is not None
                    else None,
                    json.dumps(outcome.rollback_handle) if outcome.rollback_handle else None,
                    outcome.rollback_handle is not None,
                    outcome.external_request_id,
                    outcome.ok,
                    json.dumps({"reason": outcome.reason.value, "message": outcome.message})
                    if outcome.reason
                    else None,
                    audit_id,
                ),
            )
            updated = cur.fetchone()
            if updated is None:
                raise LookupError(f"action {audit_id} does not exist")

            cur.execute(
                """
                select core.log_audit(
                  'workspace', %s, p_workspace => %s,
                  p_actor_type => 'agent', p_actor => null, p_payload => %s::jsonb
                )
                """,
                (
                    f"action.{outcome.decision.value}",
                    updated["workspace_id"],
                    json.dumps(
                        {
                            "tool": outcome.tool.value,
                            "verified": outcome.verified,
                            "action_id": audit_id,
                            "reason": outcome.reason.value if outcome.reason else None,
                        }
                    ),
                ),
            )
            conn.commit()

    def guardrail(
        self, *, workspace_id: str, breach: GuardrailBreach, action_taken: str
    ) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.guardrail_events
                  (workspace_id, guardrail, guardrail_class, threshold, observed,
                   action_taken, detail_json)
                values (%s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    workspace_id,
                    breach.guardrail,
                    breach.guardrail_class,
                    breach.threshold,
                    breach.observed,
                    action_taken,
                    json.dumps({"message": breach.message}),
                ),
            )
            conn.commit()


class PostgresLockManager:
    """Per-ad-account mutation lock built on a Postgres session advisory lock.

    Reads never take it (PRD 10.9). The lease is the connection: if a worker
    crashes, the connection drops and the lock is released, so a dead process
    cannot deadlock an account.
    """

    # NOT pooled, and the exception is load-bearing rather than an oversight.
    #
    # pg_try_advisory_lock takes a SESSION lock, and this one is held across
    # audit.pre (which commits), the driver call and audit.post - spanning
    # transactions on purpose, because the lease has to outlive the write it
    # protects. `SET LOCAL` survives a transaction pooler; a session advisory
    # lock does not. supabase/config.toml has `[db.pooler] enabled = false`
    # today, so this is a landmine that INTRODUCING Supavisor would create
    # rather than a present bug - and putting this connection in a pool would
    # be the same mistake one layer up.

    @contextmanager
    def acquire(self, ad_account_id: str, timeout_s: int) -> Iterator[bool]:
        key = _advisory_key(ad_account_id)
        deadline = time.monotonic() + timeout_s

        with psycopg.connect(service_dsn()) as conn:
            conn.autocommit = True
            acquired = False
            try:
                while True:
                    with conn.cursor() as cur:
                        cur.execute("select pg_try_advisory_lock(%s)", (key,))
                        acquired = bool(cur.fetchone()[0])
                    if acquired or time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)

                yield acquired
            finally:
                if acquired:
                    with conn.cursor() as cur:
                        cur.execute("select pg_advisory_unlock(%s)", (key,))


class PostgresOutcomeScheduler:
    """Queues the outcome check at the pre-registered horizon.

    Writes a placeholder ``t_advit.outcomes`` row rather than relying on a
    job runner being present, so the obligation to measure survives a restart.
    """

    def queue(self, *, decision_id: str, horizon_days: int) -> None:
        with service_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.outcomes
                  (decision_id, workspace_id, horizon_days, verdict, metrics_json, notes)
                select d.id, d.workspace_id, %s, 'unmeasurable', '{}'::jsonb,
                       'queued at execution; to be measured at the horizon'
                  from t_advit.decisions d
                 where d.id = %s
                on conflict (decision_id, horizon_days) do nothing
                """,
                (horizon_days, decision_id),
            )
            conn.commit()
