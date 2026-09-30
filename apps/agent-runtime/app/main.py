"""Agent runtime HTTP surface.

The endpoints implemented here are the ones that work without a model key: the
compliance gate, the account audit, connection health, the approvals inbox and
rollback. The chat and orchestration endpoints from PRD Appendix B come with
the orchestrator.

Every mutating path goes through the tool pipeline. Nothing in this module
calls the Meta driver directly, so no route can bypass the policy layer.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from psycopg.rows import dict_row
from pydantic import BaseModel

from app.agents.account_audit import AccountAuditor
from app.agents.compliance import ComplianceGate, CreativeBundle, LicencePosture
from app.branding import BRAND
from app.auth.middleware import PrincipalMiddleware
from app.auth.route_audit import assert_every_route_is_guarded
from app.auth.scope import (
    AuthorizedWorkspace,
    Principal,
    authorized_action,
    authorized_ad_account,
    authorized_approval,
    authorized_workspace,
    current_principal,
)
from app.config import get_settings
from app.db import expectations
from app.db.pools import close_pools, open_pools, service_conn
from app.deps import get_driver, get_pipeline, get_rule_loader
from app.ingest.metrics import sync_workspace
from app.meta.driver import EntityStatus, MetaDriver, MetaError
from app.orchestrator.execution import PROPOSABLE_TOOLS
from app.policy.pipeline import AgentIdentity, Decision, ToolPipeline, ToolRequest
from app.policy.risk import Tool
from app.policy.rules import UnknownIndustry
from app.routes_business import router as business_router
from app.routes_chat import router as chat_router
from app.routes_billing import router as billing_router
from app.routes_creative import router as creative_router
from app.routes_settings import router as settings_router
from app.routes_admin import router as admin_router
from app.routes_reports import router as reports_router

log = logging.getLogger("advit.main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Both pools open at BOOT.

    A wrong credential has to fail here rather than on the first request that
    happens to need it - otherwise the service comes up healthy, serves
    `/health` and `/api/brand` for an hour, and fails the first time somebody
    actually uses it.
    """
    open_pools()
    # After the pools, because a resolver that cannot reach the database would
    # fail later anyway, and before serving anything at all: a code review can
    # miss a route, a failed boot cannot.
    assert_every_route_is_guarded(app)
    # And the database this build was written against. A container whose code
    # is newer than the schema does not crash here - the operator may be
    # applying the migration right now - but it does not pass /health either,
    # so Coolify keeps the previous container serving until it does.
    _refresh_schema_state()
    try:
        yield
    finally:
        close_pools()


def _refresh_schema_state() -> list[str]:
    """Re-probe the expectations and remember the answer on app.state."""
    try:
        with service_conn() as conn, conn.cursor() as cur:
            behind = expectations.missing(cur)
    except Exception:  # pragma: no cover - depends on environment
        # No database at all is db_ok=False's job to report; the schema
        # question stays open rather than being answered "behind" by a
        # connection error.
        behind = list(app.state.schema_behind) if hasattr(app.state, "schema_behind") else []
    app.state.schema_behind = behind
    if behind:
        log.error(
            "database is behind this build: %d migration(s) not applied - %s. "
            "/health answers 503 until they are.",
            len(behind), ", ".join(behind),
        )
    return behind


app = FastAPI(
    lifespan=lifespan,
    # Identity comes from app.branding so the name, by-line and site cannot
    # drift between the API docs, a report footer and the web apps.
    title=BRAND.product_name,
    version="0.1.0",
    summary=BRAND.byline,
    description=BRAND.api_description(),
    contact={"name": BRAND.company_name, "url": BRAND.website_url},
)

# Default-deny at the edge. Registered before the routers so a route added to
# either of them is closed the moment it exists rather than the moment somebody
# remembers to add a dependency.
app.add_middleware(PrincipalMiddleware)

# Business-truth, economics and CTA routes: the closed loop, which needs no
# pixel and no Meta signal at all.
app.include_router(business_router)

# Chat is the control surface; the dashboard is the read model (PRD 5.3).
app.include_router(chat_router)

# The creative studio: upload, rate, compare.
app.include_router(creative_router)

# Plans, coupons and invoices - the tenant's side and the operator's.
app.include_router(billing_router)

# The organisation's own OpenRouter key and its model tier per function.
app.include_router(settings_router)

# The operator's console: organisations, the Platform Watch inbox, the trail.
app.include_router(admin_router)

# The owner's report, analytics and suggestions tabs: one read model, tenant
# connection only.
app.include_router(reports_router)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health(response: Response) -> dict[str, Any]:
    db_ok = True
    try:
        with service_conn() as conn, conn.cursor() as cur:
            cur.execute("select 1")
    except Exception:  # pragma: no cover - depends on environment
        # Deliberately not captured. The exception text carries the DSN and
        # internal hostnames, and this response is public.
        db_ok = False

    # Schema behind the code is the one condition that makes this route say
    # NO (503) rather than "degraded" (200): a database that is down comes
    # back on its own and restarting the container would not help, but a
    # database missing a migration does not come back on its own, and the
    # container serving that code must not be the one traffic reaches. While
    # behind, re-probe per call so the answer flips the moment the migration
    # lands, without a restart.
    behind = list(getattr(app.state, "schema_behind", []))
    if behind and db_ok:
        behind = _refresh_schema_state()
    if behind:
        response.status_code = 503

    # Liveness and identity only.
    #
    # This route is unauthenticated by necessity - a container health check
    # cannot hold a session - so everything it returns is public. It used to
    # return `write_allowlist`, which is the list of ad accounts this process
    # may spend money on, and the raw `db_error` string, which carries the DSN
    # and internal hostnames on failure.
    #
    # The test that pinned that behaviour justified it as "the two facts an
    # operator needs before trusting the process with an ad account". True of an
    # operator; false of an anonymous caller, and the route could not tell them
    # apart. Operational detail moves behind authentication with the rest of the
    # API; until then it is simply not published.
    #
    # `db_ok` still distinguishes ok from degraded, because a health check that
    # cannot report unhealthy is decoration.
    return {
        "status": "behind" if behind else ("ok" if db_ok else "degraded"),
        "product": BRAND.product_name,
        "by": BRAND.company_name,
        "website": BRAND.website_url,
    }


@app.get("/api/health/detail")
def health_detail(
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict[str, Any]:
    """The operational half of the old /health, behind authentication.

    `write_allowlist` is the list of ad accounts this process may spend money
    on, and the database error string carries the DSN and internal hostnames.
    Both are exactly what an operator needs before trusting the process with an
    ad account, and exactly what an anonymous caller should not be handed - and
    the unauthenticated route could not tell them apart.
    """
    settings = get_settings()
    db_error: str | None = None
    try:
        with service_conn() as conn, conn.cursor() as cur:
            cur.execute("select 1")
    except Exception as exc:  # pragma: no cover - depends on environment
        db_error = str(exc)

    behind = list(getattr(app.state, "schema_behind", []))
    return {
        "meta_driver": settings.meta_driver,
        "write_allowlist": sorted(settings.write_allowlist),
        "database": {"ok": db_error is None, "error": db_error},
        # Which migrations this build needs and the database lacks, by file
        # stem - the list an operator applies, in order.
        "schema": {"ok": not behind, "missing": behind},
        "openrouter_key_present": bool(settings.openrouter_api_key),
    }


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------


class ComplianceRequest(BaseModel):
    primary_text: str = ""
    headline: str = ""
    description: str = ""
    cta_type: str = ""
    destination_url: str = ""
    lp_first_fold_text: str | None = None
    # Not a Literal. The two labels used to be written out here as well as in
    # the database, which meant selling a third industry required a redeploy of
    # this file - the enum growing back in Python after being removed from the
    # schema. The catalogue is t_advit.industries; an unseeded key is refused
    # by the loader (UnknownIndustry) rather than quietly served the rules
    # scoped to every pack, which would be the Meta layer with the whole Indian
    # statutory layer missing.
    business_type: str = "general_d2c"
    ai_generated_declared: bool | None = None
    product_classification: str | None = None
    ayush_licence_no: str | None = None
    has_media: bool = False


@app.post("/api/compliance/check")
def compliance_check(
    payload: ComplianceRequest,
    # Authenticated but deliberately unscoped: a pure function of its input that
    # reads no tenant row. The principal is here for metering and abuse, and the
    # path is listed in route_audit.UNSCOPED with that reason.
    principal: Annotated[Principal, Depends(current_principal)] = None,
) -> dict[str, Any]:
    """Ad-hoc pre-flight on a creative bundle (PRD 13.4).

    Reports each layer independently and names the stages it did not run.
    A verdict of ``not_evaluated`` means no rule fired but the gate cannot
    certify the bundle - it is not a pass.
    """
    try:
        ruleset = get_rule_loader().load(payload.business_type)
    except UnknownIndustry as exc:
        # 422, not 404: the request named a pack that does not exist, and the
        # only alternative would be to adjudicate it under a ruleset nobody
        # chose. A guard with nothing to compare against refuses.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    gate = ComplianceGate(list(ruleset))

    fields = payload.model_dump()
    licence_no = fields.pop("ayush_licence_no")
    classification = fields.pop("product_classification")

    # This route carries no workspace_id, so there is no catalogue to resolve a
    # licence posture against: whatever the caller passes is an assumption they
    # are asking the gate to reason under, not a fact on file. Marked
    # caller_asserted and reported back as such, so a clean verdict from here
    # can never be read as certifying a real workspace's product. The
    # orchestrator path builds `catalogue` postures and only those.
    posture = LicencePosture(
        source="caller_asserted",
        ayush_licence_no=licence_no,
        classification=classification,
    )
    result = gate.check(CreativeBundle(**fields, licence_posture=posture))

    return {
        "verdict": result.verdict.value,
        "licence_posture_source": posture.source,
        "layers": {
            "meta": result.layer_verdict("meta").value,
            "india": result.layer_verdict("india").value,
        },
        "overall_risk": result.overall_risk,
        # Three disjoint sets, so "checked" is never ambiguous.
        "stages_evaluated": result.stages_evaluated,
        "stages_partial": result.stages_partial,
        "stages_skipped": result.stages_skipped,
        "ruleset": {
            "rule_count": len(ruleset),
            "oldest_as_of": ruleset.oldest_as_of.isoformat() if ruleset.oldest_as_of else None,
            # Surfaced so the caller can say "I need to verify the current
            # rule" rather than asserting a stale one (PRD 13.5).
            "stale_rules": list(ruleset.stale_codes),
        },
        "findings": [
            {
                "rule_code": f.rule_code,
                "layer": f.layer,
                "instrument": f.instrument,
                "stage": f.stage,
                "severity": f.severity.value,
                "title": f.title,
                "explanation": f.explanation,
                "field": f.field,
                "offending_span": f.offending_span,
                "span": [f.span_start, f.span_end] if f.span_start is not None else None,
                "suggested_rewrite": f.suggested_rewrite,
                "source_url": f.source_url,
                "as_of": f.as_of.isoformat(),
                "needs_legal_verification": f.needs_legal_verification,
            }
            for f in result.findings
        ],
    }


# ---------------------------------------------------------------------------
# Account audit and connection health
# ---------------------------------------------------------------------------


@app.get("/api/audit/account/{ad_account_id}")
def audit_account(
    ad_account_id: str,
    # The resolver reads t_advit.meta_connections under RLS, so the question
    # "is this ad account yours?" is answered by meta_connections_select rather
    # than by this route. Without it, a signed-in stranger who knows an account
    # id gets a full structural audit of somebody else's advertising.
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_ad_account)] = None,
) -> dict[str, Any]:
    """Structural and measurement audit with a scored punch-list (FR-005)."""
    try:
        return AccountAuditor(get_driver()).audit(ad_account_id).as_dict()
    except MetaError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@app.get("/api/workspaces/{workspace_id}/connections/health")
def connections_health(
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    """Diagnostic, not decorative: each chip states what is wrong and how to
    fix it (PRD 16.2)."""
    with ws.principal.tx() as cur:
            cur.execute(
                """
                select w.name as workspace_name,
                       w.industry_key as business_type,
                       w.autonomy_level,
                       t_advit.effective_autonomy(w.id) as effective_autonomy,
                       core.access_mode(w.org_id, t_advit.product_id())::text
                                                        as access_mode,
                       w.is_paused,
                       w.daily_cap_inr, w.monthly_cap_inr
                  from t_advit.workspaces w where w.id = %s
                """,
                (ws.id,),
            )
            workspace = cur.fetchone()
            if workspace is None:
                # Unreachable in practice - authorized_workspace already proved
                # membership on this same connection - but a resolver and a
                # route reading different rows is the kind of drift that should
                # 404 rather than crash on a None subscript.
                raise HTTPException(status_code=404, detail="workspace not found")

            cur.execute(
                """
                select ad_account_id, health::text as health, write_enabled,
                       currency, health_detail
                  from t_advit.meta_connections
                 where workspace_id = %s order by ad_account_id
                """,
                (ws.id,),
            )
            connections = cur.fetchall()

    driver = get_driver()
    for c in connections:
        datasets = driver.get_datasets(c["ad_account_id"])
        c["dataset_count"] = len(datasets)
        c["measurement_ready"] = bool(datasets)

    return {
        "workspace": workspace,
        "automation": {
            "autonomy_level": workspace["autonomy_level"],
            "effective_autonomy": workspace["effective_autonomy"],
            # These differ whenever the plan caps the workspace's intent.
            "capped_by_plan": workspace["effective_autonomy"] < workspace["autonomy_level"],
            "access_mode": workspace["access_mode"],
            "is_paused": workspace["is_paused"],
        },
        "meta_connections": connections,
        "model_access": {
            "driver": get_settings().meta_driver,
            "openrouter_key_present": bool(get_settings().openrouter_api_key),
        },
    }


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


@app.post("/api/workspaces/{workspace_id}/sync")
def sync_metrics(
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
    days: int = Query(default=7, ge=1, le=92),
    driver: MetaDriver = Depends(get_driver),
) -> dict[str, Any]:
    """Read this workspace's ad accounts and write what Meta reports.

    A manual trigger. The 07:30 brief and the monitoring loop will call
    `sync_workspace` on a schedule, and no scheduler exists yet - so until one
    does, this is how `t_advit.metrics_daily` gets rows at all.

    The route says WHICH WORKSPACE. It does not say which ad accounts: those
    come from a SELECT over our own `meta_connections`, which is the same rule
    `app/auth/scope.py::system_workspace` states for the unattended path. A
    caller that could name an ad account could make this process read one it
    does not hold.

    The response is the reports themselves rather than a row count, because
    "wrote 340 rows" answers no question anybody has. `discrepancies` is the one
    that matters: campaign-level spend that does not add up to the account
    total is the only available evidence that a sync silently dropped a
    campaign, and every row it wrote would look individually correct.
    """
    reports = [
        report.as_dict()
        for report in sync_workspace(driver, workspace_id=ws.id, lookback_days=days)
    ]
    if not reports:
        return {
            "synced": [],
            "note": "this workspace has no connected ad accounts",
        }

    return {
        "synced": reports,
        # Surfaced at the top level rather than left inside each report, because
        # a partial failure is the case an operator must not scroll past: one
        # account failing while two succeed produces a dashboard that looks
        # fine and is short a third of its spend.
        "failed": [r["ad_account_id"] for r in reports if r["error"]],
        "rows_written": sum(r["rows_written"] for r in reports),
    }


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@app.get("/api/workspaces/{workspace_id}/approvals")
def list_approvals(
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
    status: str = "pending",
) -> list[dict[str, Any]]:
    with ws.principal.tx() as cur:
            cur.execute(
                """
                select a.id::text, a.status::text, a.risk_class::text,
                       a.proposed_json, a.impact_inr, a.expires_at,
                       (a.expires_at <= now()) as expired,
                       d.decision_type, d.reasoning,
                       d.expected_effect_json, d.horizon_days, d.confidence
                  from t_advit.approvals a
                  join t_advit.decisions d on d.id = a.decision_id
                 where a.workspace_id = %s and a.status::text = %s
                 order by a.created_at desc
                """,
                (ws.id, status),
            )
            return cur.fetchall()


class ApprovalResponse(BaseModel):
    action: Literal["approve", "reject", "modify"]
    # A rejection reason is a training signal and is stored as one (FR-014).
    reason: str | None = None

    # `responded_by` is gone, deliberately.
    #
    # It was a caller-supplied human signature on the row that DISCHARGES an
    # approval - the row pipeline step 6 redeems before spending money. Any
    # client could name anybody as the person who approved, which makes the
    # approval trail a record of what the client typed rather than of who
    # signed. It now comes from auth.uid() on the tenant connection, which the
    # caller cannot choose.


@app.post("/api/approvals/{approval_id}/respond")
def respond_to_approval(
    approval_id: str,
    payload: ApprovalResponse,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_approval)] = None,
    pipeline: ToolPipeline = Depends(get_pipeline),
) -> dict[str, Any]:
    if payload.action == "reject" and not (payload.reason or "").strip():
        raise HTTPException(
            status_code=422,
            detail="a rejection reason is required: it is stored as a training signal",
        )

    status = {"approve": "approved", "reject": "rejected", "modify": "modified"}[payload.action]

    # On the TENANT connection, because `approvals_respond` is exactly this
    # check: 20260903000007 grants `authenticated` UPDATE on approvals alone,
    # and the policy decides whether this caller may answer this one.
    with ws.principal.tx() as cur:
        cur.execute(
            """
            update t_advit.approvals
               set status = %s::t_advit.approval_status,
                   responded_at = now(),
                   -- From the session, never from the payload. This is the
                   -- signature on the row that authorises spending.
                   responded_by = auth.uid(),
                   reject_reason = %s
             where id = %s::uuid and status = 'pending'
            returning id::text, status::text, workspace_id::text,
                      responded_by::text
            """,
            (status, payload.reason, approval_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=409,
                detail="approval is not pending; it may already be answered or expired",
            )
        cur.execute(
            """
            select core.log_audit('workspace', %s,
                                  -- p_org is NOT optional here. core.log_audit
                                  -- refuses a workspace-scoped row that does
                                  -- not name its organisation (hint
                                  -- audit_org_required), and on the superuser
                                  -- connection this route never reached that
                                  -- branch. Every approval response would have
                                  -- 500'd on the first tenant request.
                                  p_org        => %s::uuid,
                                  p_workspace  => %s::uuid,
                                  p_actor_type => 'user',
                                  p_actor      => auth.uid(),
                                  p_payload    => %s::jsonb,
                                  p_approval_id => %s::uuid)
            """,
            (
                f"approval.{status}",
                ws.org_id,
                row["workspace_id"],
                json.dumps({"reason": payload.reason}),
                approval_id,
            ),
        )

    if payload.action != "approve":
        return row

    # -----------------------------------------------------------------------
    # Redemption. The half that was missing.
    #
    # Answering an approval used to update a row and stop. The proposal it
    # authorised was never carried out by anything - `ToolPipeline.invoke` had
    # one caller in the whole application, the rollback route, which needs an
    # actions row that only invoke() creates - so the approvals inbox was a
    # button that recorded an opinion.
    #
    # The approval row IS the durable suspension point, and using it as one
    # needs no checkpointer: it survives a restart and a deploy, it already
    # carries the authorisation fingerprint that step 6 checks the redemption
    # against, and it is the row the inbox reads. A LangGraph checkpoint would
    # be a second suspension mechanism for the same event, and the two could
    # disagree.
    #
    # Everything still runs: the tenant check, the guardrails re-evaluated
    # inside the lock against CURRENT spend, the authorisation binding, the
    # verification step. An approval granted on Tuesday's numbers does not
    # execute on Friday's - it is refused and the proposal is re-derived.
    # -----------------------------------------------------------------------
    request = _redeem(approval_id, ws)
    if request is None:
        return {**row, "execution": {"attempted": False,
                                     "reason": "the approval carries no executable action"}}

    outcome = pipeline.invoke(
        AgentIdentity(name="media_buying", allowed_tools=frozenset(PROPOSABLE_TOOLS.values())),
        request,
    )
    return {
        **row,
        "execution": {
            "attempted": True,
            "tool": request.tool.value,
            "decision": outcome.decision.value,
            "reason": outcome.reason.value if outcome.reason else None,
            "message": outcome.message,
            "verified": outcome.verified,
            "action_id": outcome.audit_id,
            "rollback_handle": outcome.rollback_handle,
            "breaches": [b.guardrail for b in outcome.breaches],
        },
    }


def _redeem(approval_id: str, ws: AuthorizedWorkspace) -> ToolRequest | None:
    """Rebuild the request the approval authorised, from the approval row.

    From the STORED proposal, never from the response body. The whole value of
    the authorisation fingerprint is that what executes is what was shown to the
    owner; rebuilding from anything the client sends would make the binding
    check compare a request against itself.

    Read on the service connection because it is the same governance spine the
    pipeline writes to, and because `authorized_approval` has already proved
    this approval belongs to this caller.
    """
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select a.decision_id::text as decision_id, a.proposed_json,
                   d.horizon_days
              from t_advit.approvals a
              left join t_advit.decisions d on d.id = a.decision_id
             where a.id = %s::uuid
            """,
            (approval_id,),
        )
        record = cur.fetchone()

    if record is None:
        return None
    proposed = record["proposed_json"] or {}
    tool = PROPOSABLE_TOOLS.get(str(proposed.get("tool") or ""))
    if tool is None or not proposed.get("ad_account_id"):
        # An approval created for something outside the proposable set - a
        # rollback, say - is answered but not redeemed here. Refusing to
        # construct a request out of a tool this route does not recognise is
        # the same rule as in app/orchestrator/execution.py.
        return None

    return ToolRequest(
        tool=tool,
        workspace_id=ws.id,
        ad_account_id=str(proposed["ad_account_id"]),
        target_entity_id=proposed.get("target_entity_id"),
        params=dict(proposed.get("params") or {}),
        decision_id=record["decision_id"],
        approval_id=approval_id,
        horizon_days=int(record["horizon_days"] or 7),
    )


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


@app.post("/api/actions/{action_id}/rollback")
def rollback_action(
    action_id: str,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_action)],
    pipeline: ToolPipeline = Depends(get_pipeline),
) -> dict[str, Any]:
    """Revert an executed action, if still revertible (FR-019).

    The revert is itself a tool call, so it passes through the same policy
    layer and writes its own audit record. Nothing bypasses the pipeline,
    including undo.
    """
    # Tenant connection: `actions_select` uses t_advit.is_workspace_member, so a
    # row belonging to another tenant is not merely filtered here - it does not
    # exist, and the resolver above has already established that this one does.
    with ws.principal.tx() as cur:
        cur.execute(
            """
            select id::text, workspace_id::text, decision_id::text,
                   rollback_handle, rollback_expires_at, rolled_back_at, verified,
                   -- Compared in SQL, against the clock that WROTE the expiry.
                   -- Fetching the timestamp and comparing it in Python would
                   -- introduce skew between the API host and the database into a
                   -- safety window, for no benefit.
                   (rollback_expires_at is not null
                    and rollback_expires_at <= now())    as rollback_expired
              from t_advit.actions where id = %s::uuid
            """,
            (action_id,),
        )
        action = cur.fetchone()

    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    if action["rolled_back_at"] is not None:
        raise HTTPException(status_code=409, detail="action has already been rolled back")
    if not action["rollback_handle"]:
        raise HTTPException(status_code=422, detail="action carries no rollback handle")

    # The expiry the store writes (now() + 24 hours) and that the schema calls
    # rollback's visible time limit. It was selected and never compared, so the
    # limit existed in the column and nowhere else.
    #
    # A NULL expiry is REFUSED, not waved through. A handle with no expiry is a
    # row written before the store set one, or by a path that skipped it - either
    # way there is nothing to compare against, and the alternative reading ("no
    # expiry means it never expires") would make the least-known row the most
    # permissive one. That is the exact shape of every other defect in this
    # ledger.
    if action["rollback_expires_at"] is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "this action carries a rollback handle with no expiry on file, so how "
                "long it has been revertible cannot be established. Refusing rather "
                "than assuming it is still inside the window."
            ),
        )

    if action["rollback_expired"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"the rollback window for this action closed at "
                f"{action['rollback_expires_at'].isoformat()}. Reverting it now would "
                "not restore the account to its earlier state - spend has accrued "
                "since, and later changes may have been made on top of this one."
            ),
        )

    handle = action["rollback_handle"]
    kind = handle.get("kind")

    if kind in ("pause_created_entity", "restore_status"):
        target_status = handle.get("status", EntityStatus.PAUSED.value)
        tool = (
            Tool.PAUSE_ENTITY
            if target_status == EntityStatus.PAUSED.value
            else Tool.ACTIVATE_ENTITY
        )
        request = ToolRequest(
            tool=tool,
            workspace_id=action["workspace_id"],
            ad_account_id=handle["ad_account_id"],
            target_entity_id=handle["entity_id"],
            decision_id=action["decision_id"],
        )
    elif kind == "restore_budget":
        request = ToolRequest(
            tool=Tool.UPDATE_BUDGET,
            workspace_id=action["workspace_id"],
            ad_account_id=handle["ad_account_id"],
            target_entity_id=handle["entity_id"],
            params={"daily_budget_inr": float(handle["daily_budget_inr"])},
            decision_id=action["decision_id"],
        )
    else:
        raise HTTPException(status_code=422, detail=f"unsupported rollback kind {kind!r}")

    agent = AgentIdentity(
        name="media_buying",
        allowed_tools=frozenset({Tool.PAUSE_ENTITY, Tool.ACTIVATE_ENTITY, Tool.UPDATE_BUDGET}),
    )
    outcome = pipeline.invoke(agent, request)

    if outcome.decision is Decision.EXECUTED:
        # Service connection: `authenticated` holds SELECT on t_advit.actions and
        # nothing else, so that an agent - or a browser - cannot rewrite its own
        # history. Marking the rollback is the system recording what it did.
        with service_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "update t_advit.actions set rolled_back_at = now() where id = %s::uuid",
                (action_id,),
            )
            conn.commit()

    return {
        "decision": outcome.decision.value,
        "reason": outcome.reason.value if outcome.reason else None,
        "message": outcome.message,
        "verified": outcome.verified,
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/api/workspaces/{workspace_id}/dashboard")
def dashboard(
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    """KPI band. Row one is the money, row two is the platform (PRD 16.2).

    Every figure is computed in SQL and handed over as a fact. No model
    arithmetic anywhere in this path.
    """
    # A read model, so it runs as the caller: the numbers on this dashboard are
    # exactly the rows RLS says they may see, and nothing here feeds a guardrail.
    with ws.principal.tx() as cur:
            cur.execute(
                """
                select date, blended_cac_inr, contribution_margin_inr, confirm_rate,
                       rto_rate, delivered_aov_inr, mer, computed_at
                  from t_advit.blended_daily
                 where workspace_id = %s
                   and date >= current_date - make_interval(days => %s)
                 order by date desc
                """,
                (ws.id, days),
            )
            money = cur.fetchall()

            cur.execute(
                """
                select coalesce(sum(spend_inr), 0)          as spend_inr,
                       coalesce(sum(impressions), 0)         as impressions,
                       coalesce(sum(link_clicks), 0)         as link_clicks,
                       coalesce(sum(results), 0)             as results,
                       max(attribution_regime::text)         as attribution_regime,
                       max(ingested_at)                      as freshness
                  from t_advit.metrics_daily
                 where workspace_id = %s and level = 'account'
                   and date >= current_date - make_interval(days => %s)
                """,
                (ws.id, days),
            )
            platform = cur.fetchone()

    return {
        # Row one is larger in the UI on purpose: it is the thing that most
        # distinguishes this dashboard from Ads Manager.
        "money": money,
        "platform": platform,
        "data_freshness": platform["freshness"] if platform else None,
        "note": (
            "Business-truth rows drive the money band. Gaps are marked, never "
            "interpolated, so an empty series means the day was not reported."
        ),
    }
