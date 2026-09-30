"""Chat and brand routes.

The chat endpoint is the product's control surface (PRD 5.3): the dashboard is
the read model, this is where decisions are made. It returns the whole run -
narration, proposal, compliance verdict, the facts every number came from, the
records they were retrieved from, and what the run cost - because a proposal
the owner cannot interrogate is a proposal they should not approve.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agents.compliance import ComplianceGate
from app.auth.scope import AuthorizedWorkspace, authorized_workspace
from app.branding import BRAND, BRAND_CSS
from app.config import get_settings
from app.db.pools import bind_tenant
from app.deps import get_pipeline, get_rule_loader
from app.models.router import ModelRouter, RoutingConfig
from app.orchestrator.graph import Orchestrator, build_graph, new_run_id

router = APIRouter()

_graph = None


def _get_graph():
    """Built once. The router and rule loader both cache, so rebuilding per
    request would only re-read config."""
    global _graph
    if _graph is not None:
        return _graph

    settings = get_settings()
    model_router = None
    if settings.openrouter_api_key:
        model_router = ModelRouter(
            settings.openrouter_api_key,
            RoutingConfig.load(settings.routing_config_path),
        )

    loader = get_rule_loader()
    orch = Orchestrator(
        router=model_router,
        gate_factory=lambda bt: ComplianceGate(list(loader.load(bt))),
        # The pipeline was accepted by this constructor and then never read.
        # Orchestrator.media_buying is the first caller it has ever had from a
        # request path - until now `invoke` was reachable only from the rollback
        # route, which needs an actions row that only invoke() creates, so the
        # path was circular and Execute mode did not exist.
        pipeline=get_pipeline(),
    )
    _graph = build_graph(orch)
    return _graph


class CreativeBundleIn(BaseModel):
    primary_text: str = ""
    headline: str = ""
    description: str = ""
    # Which product this creative advertises. A REFERENCE, not a claim: the
    # licence number and the classification are read from
    # t_advit.catalog_products for this workspace. A sku that is not in the
    # workspace's catalogue resolves to nothing, and stage 9 then reports
    # itself unevaluated rather than clearing the creative.
    product_sku: str | None = None


class ChatRequest(BaseModel):
    # `workspace_id` is gone from the body and is a path parameter instead.
    #
    # Not cosmetics. It was the only thing naming the tenant, it was chosen by
    # the caller, and nothing checked it - so any request could drive the whole
    # orchestrator against any workspace whose id it could guess or had once
    # seen. As a path parameter it is resolved by `authorized_workspace` before
    # the route body runs, and route_audit fails the boot if it ever reappears
    # here.
    message: str = Field(min_length=1)
    # Supplied by the creative upload flow. Absent for ordinary conversation,
    # which the compliance gate must not treat as ad copy.
    creative: CreativeBundleIn | None = None
    thread_id: str | None = None

    # Removed, and refused loudly rather than dropped quietly.
    #
    # These were the only inputs to IN_AYUSH_LICENCE_ON_FILE, a BLOCK rule, so
    # a caller could clear a statutory block by typing a string
    # ("FAKE-NOT-A-LICENCE" was the reproduction) while the workspace's real
    # catalogue row held NULL. They are now read from
    # t_advit.catalog_products.
    #
    # Kept in the model on purpose. Deleting them would let pydantic ignore
    # them silently, and a client that keeps sending a licence number would go
    # on believing it controls a compliance verdict while the verdict quietly
    # comes from somewhere else. /api/chat has no authentication, so silence is
    # the worst option available: a 422 naming the replacement is a one-line
    # client change and cannot be mistaken for working.
    ayush_licence_no: str | None = Field(default=None, deprecated=True)
    product_classification: str | None = Field(default=None, deprecated=True)


@router.post("/api/workspaces/{workspace_id}/chat")
def chat(
    payload: ChatRequest,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    # `model_fields_set` rather than a truthiness test, so an explicit null is
    # refused too. A client sending the key at all believes it controls a
    # compliance verdict, and that belief is the thing being corrected.
    rejected = sorted(
        payload.model_fields_set & {"ayush_licence_no", "product_classification"}
    )
    if rejected:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{', '.join(rejected)} is no longer accepted: the AYUSH licence and the "
                "product classification are read from t_advit.catalog_products, not from "
                "the request. Name the product with creative.product_sku instead."
            ),
        )

    run_id = new_run_id()

    # Bind the caller's transaction factory for the duration of the run. Every
    # read the orchestrator makes - account context, business truth, the product
    # catalogue - then happens under this caller's claims, and RLS decides what
    # is in the prompt. Unbinding on the way out means a graph node that somehow
    # runs later raises instead of reaching for the privileged connection.
    #
    # A FACTORY, not an open transaction: a chat turn makes model calls that
    # take seconds, and holding one transaction across them would pin an
    # idle-in-transaction snapshot and exhaust the pool under trivial
    # concurrency. The principal is request-scoped; each transaction is
    # query-scoped.
    try:
        with bind_tenant(ws.principal.tx):
            state = _get_graph().invoke(
                {
                    "run_id": run_id,
                    "workspace_id": ws.id,
                    "thread_id": payload.thread_id or run_id,
                    "trigger": "user_message",
                    "message": payload.message,
                    "creative": payload.creative.model_dump() if payload.creative else None,
                }
            )
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - surfaced rather than swallowed
        raise HTTPException(status_code=500, detail=f"run failed: {exc}") from exc

    errors = state.get("errors", [])

    # A missing workspace is the caller's mistake and genuinely has no answer.
    # An agent failing is NOT: the compliance verdict, the computed facts and
    # the data gaps were all produced deterministically before any model was
    # called, and they are the most trustworthy part of the run. Discarding
    # them because a provider returned 402 would break the rule the whole
    # design rests on - quality degrades, availability does not (PRD 14.6).
    if any("not found" in e for e in errors):
        raise HTTPException(status_code=404, detail="; ".join(errors))

    completions = state.get("completions", [])
    return {
        "run_id": run_id,
        # Named so a caller can tell a complete answer from a partial one
        # rather than inferring it from a missing field.
        "degraded": bool(errors),
        "degraded_reason": "; ".join(errors) if errors else None,
        "intent": state.get("intent"),
        "mode": state.get("mode"),
        "narration": state.get("narration", ""),
        "proposal": state.get("proposal"),
        # Whether the proposal was held for a CTA decision, and the CTA model's
        # own recommendation so the client can render the choice as argued
        # options rather than as a blank.
        "cta_gate": state.get("cta_gate"),
        "questions": state.get("questions", []),
        "compliance": state.get("compliance"),
        # Every number in the narration traces to this block; nothing else is
        # authoritative (PRD 17.7).
        "facts": state.get("facts", {}),
        "gaps": state.get("facts_gaps", []),
        # Which memory records were in context. This is how "why did it say
        # that?" gets answered (PRD 17.8).
        "retrieved_record_ids": state.get("retrieved_record_ids", []),
        "activity": state.get("events", []),
        "cost": {
            "inr": round(sum(c.get("cost_inr", 0) for c in completions), 4),
            "calls": [
                {
                    "role": c.get("role"),
                    "class": c.get("class"),
                    "model": c.get("model"),
                    "tokens_in": c.get("tokens_in"),
                    "tokens_out": c.get("tokens_out"),
                    "reasoning_tokens": c.get("reasoning_tokens"),
                    "cost_inr": round(c.get("cost_inr", 0), 4),
                    "fell_back": c.get("fell_back"),
                }
                for c in completions
            ],
        },
    }


@router.get("/api/brand")
def brand(format: Literal["json", "html", "markdown", "text"] = "json") -> Any:
    """The product lock-up, from one source so it cannot drift across surfaces.

        ad-vit
        by Broadmate Global
        broadmate.org
    """
    if format == "html":
        return {"html": BRAND.html(), "css": BRAND_CSS}
    if format == "markdown":
        return {"markdown": BRAND.markdown()}
    if format == "text":
        return {"text": BRAND.plain()}
    return {**BRAND.as_dict(), "footer": BRAND.footer()}
