"""The creative studio's HTTP surface.

Three steps, because a video should not pass through the API server:

  1. ``POST /api/workspaces/{ws}/creatives/uploads`` - declares the upload and
     gets back a one-shot signed URL. The runtime writes the ``creatives`` row
     first, in status ``uploaded``, so the object path is derived from a row
     this process owns rather than from anything the caller sent.

  2. The browser PUTs the bytes to that URL. Storage enforces the bucket's
     size and type limits and its own policies.

  3. ``POST /api/workspaces/{ws}/creatives/{id}/analyse`` - the runtime
     confirms the object is really there, pulls it down for ffmpeg, rates it on
     the organisation's own ``creative_analysis`` tier, and writes the rating.

Every route depends on ``authorized_workspace``, so ``is_workspace_member`` is
the boundary before any of this runs, and the route audit refuses to boot if
one is added without it.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agents.compliance import ComplianceGate, LicencePosture
from app.auth.scope import AuthorizedWorkspace, authorized_workspace
from app.creative import analyse, storage
from app.creative.rubric import CRITERIA, RUBRIC_VERSION
from app.db.pools import service_conn
from app.deps import get_rule_loader
from app.models.for_org import NoKeyForOrganisation, router_for_org
from app.orchestrator.graph import _licence_posture
from app.policy.rules import UnknownIndustry

router = APIRouter()

ContentType = Literal[
    "video/mp4", "video/quicktime", "video/webm",
    "image/jpeg", "image/png", "image/webp",
]


# ---------------------------------------------------------------------------
# 1. Declare an upload
# ---------------------------------------------------------------------------


class UploadRequest(BaseModel):
    original_name: str = Field(min_length=1, max_length=255)
    content_type: ContentType
    size_bytes: int = Field(gt=0, le=4 * 1024 * 1024 * 1024)
    # A reference into the workspace's catalogue, not a claim. Resolved to a
    # LicencePosture at analysis time the same way the chat path does it.
    product_sku: str | None = None
    # Asked at upload rather than inferred: an undeclared AI creative is not the
    # same as a human one, and the AI-disclosure stage treats "not declared" as
    # unevaluated rather than as no.
    ai_generated: bool | None = None


@router.post("/api/workspaces/{workspace_id}/creatives/uploads")
def declare_upload(
    payload: UploadRequest,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    creative_id = str(uuid.uuid4())
    path = storage.object_path(ws.id, creative_id, payload.content_type)
    media_type = "video" if payload.content_type.startswith("video/") else "image"

    product_id = None
    with ws.principal.tx() as cur:
        if payload.product_sku:
            cur.execute(
                "select id::text as id from t_advit.catalog_products "
                " where workspace_id = %s::uuid and sku = %s",
                (ws.id, payload.product_sku),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(
                    422, f"product {payload.product_sku!r} is not in this workspace's catalogue"
                )
            product_id = row["id"]

        # The tenant's own row on the tenant connection: creatives_write is the
        # policy that decides. uploaded_by from the session, not the payload.
        cur.execute(
            """
            insert into t_advit.creatives
              (id, workspace_id, asset_ref, media_type, original_name, size_bytes,
               ai_generated, product_id, uploaded_by, status)
            values (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s::uuid, auth.uid(), 'uploaded')
            """,
            (
                creative_id, ws.id, path, media_type, payload.original_name,
                payload.size_bytes, bool(payload.ai_generated), product_id,
            ),
        )

    try:
        signed = storage.sign_upload(path)
    except storage.StorageUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    return {
        "creative_id": creative_id,
        "upload": {
            "url": signed.url,
            "method": "PUT",
            "headers": {"Content-Type": payload.content_type},
        },
        "next": f"/api/workspaces/{ws.id}/creatives/{creative_id}/analyse",
    }


def _measured_float(measured: list[dict[str, Any]], key: str) -> float | None:
    """`observed` is a display string ("9.0s"); the column wants the number."""
    entry = next((m for m in measured if m["key"] == key), None)
    if not entry or not entry.get("observed"):
        return None
    try:
        return float(str(entry["observed"]).rstrip("s"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 3. Analyse
# ---------------------------------------------------------------------------


class AnalyseRequest(BaseModel):
    objective: Literal["awareness", "consideration", "conversion"] = "conversion"


def _catalogue(cur, workspace_id: str) -> list[dict[str, Any]]:
    cur.execute(
        "select sku, ayush_licence_no, classification::text as classification "
        "  from t_advit.catalog_products where workspace_id = %s::uuid",
        (workspace_id,),
    )
    return cur.fetchall()


@router.post("/api/workspaces/{workspace_id}/creatives/{creative_id}/analyse")
def analyse_creative(
    creative_id: uuid.UUID,
    payload: AnalyseRequest,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    # -- what we are analysing, and what governs it ------------------------
    with ws.principal.tx() as cur:
        cur.execute(
            """
            select c.id::text as id, c.asset_ref, c.media_type, c.status::text as status,
                   c.original_name, p.sku as product_sku,
                   w.industry_key as business_type
              from t_advit.creatives c
              join t_advit.workspaces w on w.id = c.workspace_id
              left join t_advit.catalog_products p on p.id = c.product_id
             where c.id = %s::uuid and c.workspace_id = %s::uuid
            """,
            (str(creative_id), ws.id),
        )
        creative = cur.fetchone()
        if creative is None:
            raise HTTPException(404, "creative not found")
        catalogue = _catalogue(cur, ws.id)

    if creative["media_type"] != "video":
        raise HTTPException(422, "only video creatives can be analysed at the moment")

    try:
        if not storage.exists(creative["asset_ref"]):
            raise HTTPException(
                409, "the file has not been uploaded yet; PUT it to the signed URL first"
            )
    except storage.StorageUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    try:
        gate = ComplianceGate(list(get_rule_loader().load(creative["business_type"])))
    except UnknownIndustry as exc:
        raise HTTPException(500, f"this workspace's industry has no rule pack: {exc}") from exc

    posture: LicencePosture | None = _licence_posture(catalogue, creative["product_sku"])

    # The organisation's own key and tier. No key is a 422 the owner can act
    # on, not a rating with the judged half silently missing - though the
    # measured and compliance halves still run, because they cost nothing.
    model_router = None
    router_note: str | None = None
    try:
        model_router = router_for_org(ws.org_id)
    except NoKeyForOrganisation as exc:
        router_note = str(exc)

    # -- mark analysing, so a second click does not start a second run --------
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "update t_advit.creatives set status = 'analysing', analysis_error = null "
            " where id = %s::uuid and status in ('uploaded', 'failed', 'analysed')",
            (str(creative_id),),
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise HTTPException(409, "this creative is already being analysed")
        conn.commit()

    # -- the work ------------------------------------------------------------
    try:
        with tempfile.TemporaryDirectory(prefix="advit-creative-") as tmp:
            local = storage.download_to(creative["asset_ref"], Path(tmp) / "creative")
            with service_conn() as conn, conn.cursor() as cur:
                rating = analyse.rate(
                    path=local,
                    router=model_router,
                    gate=gate,
                    licence_posture=posture,
                    cur=cur,
                    workspace_id=ws.id,
                    business_type=creative["business_type"],
                    product_hint=creative["product_sku"],
                    objective=payload.objective,
                )
    except Exception as exc:  # noqa: BLE001 - recorded on the row, then surfaced
        with service_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "update t_advit.creatives set status = 'failed', analysis_error = %s "
                " where id = %s::uuid",
                (f"{type(exc).__name__}: {exc}"[:2000], str(creative_id)),
            )
            conn.commit()
        if isinstance(exc, storage.StorageUnavailable):
            raise HTTPException(503, str(exc)) from exc
        raise HTTPException(500, f"analysis failed: {exc}") from exc

    if router_note:
        rating.limitations.append(router_note)

    # -- write the rating: the SYSTEM's judgement, on the service connection --
    verdict = (rating.compliance or {}).get("verdict") or "not_evaluated"
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            update t_advit.creatives
               set status = 'analysed',
                   analysed_at = now(),
                   rating_json = %s::jsonb,
                   compliance_verdict = %s::t_advit.compliance_verdict,
                   duration_s = %s,
                   ratio = %s,
                   has_captions = %s
             where id = %s::uuid
            """,
            (
                json.dumps(rating.as_dict(), default=str),
                verdict,
                _measured_float(rating.measured, "length"),
                next((m for m in rating.measured if m["key"] == "vertical"), {}).get("observed"),
                bool(rating.on_screen_text.strip()) or None,
                str(creative_id),
            ),
        )
        conn.commit()

    return {"creative_id": str(creative_id), "status": "analysed", "rating": rating.as_dict()}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/creatives")
def list_creatives(
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> list[dict[str, Any]]:
    with ws.principal.tx() as cur:
        cur.execute(
            """
            select c.id::text as id, c.original_name, c.media_type, c.status::text as status,
                   c.created_at, c.analysed_at, c.analysis_error,
                   c.rating_json ->> 'overall' as overall,
                   c.compliance_verdict::text as compliance_verdict,
                   p.sku as product_sku
              from t_advit.creatives c
              left join t_advit.catalog_products p on p.id = c.product_id
             where c.workspace_id = %s::uuid
             order by c.created_at desc
            """,
            (ws.id,),
        )
        return cur.fetchall()


@router.get("/api/workspaces/{workspace_id}/creatives/rubric")
def rubric(ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)]) -> dict[str, Any]:
    """What a video is scored against, and why. Shown beside every rating, so
    an owner can disagree with the rubric as well as with the score."""
    return {
        "version": RUBRIC_VERSION,
        "criteria": [
            {"key": c.key, "label": c.label, "kind": c.kind.value, "weight": c.weight, "why": c.why}
            for c in CRITERIA
        ],
    }



@router.get("/api/workspaces/{workspace_id}/creatives/{creative_id}")
def get_creative(
    creative_id: uuid.UUID,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    with ws.principal.tx() as cur:
        cur.execute(
            """
            select c.id::text as id, c.original_name, c.media_type, c.status::text as status,
                   c.created_at, c.analysed_at, c.analysis_error, c.rating_json,
                   c.compliance_verdict::text as compliance_verdict, c.ai_generated,
                   p.sku as product_sku
              from t_advit.creatives c
              left join t_advit.catalog_products p on p.id = c.product_id
             where c.id = %s::uuid and c.workspace_id = %s::uuid
            """,
            (str(creative_id), ws.id),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "creative not found")
    return row


_ = datetime, timezone
