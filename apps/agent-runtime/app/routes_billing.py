"""Plans, coupons and invoices - the tenant's side and the operator's.

The tenant's routes are workspace-scoped like everything else, and read on the
TENANT connection: the coupons a tenant is offered are exactly the rows
``coupons_tenant_sees_live_offers`` lets through, and applying one goes through
``core.apply_coupon``, which refuses on every doubt in SQL rather than trusting
this file to have checked.

The operator's routes are under ``/api/admin/`` and depend on
``authorized_superadmin``, which reads ``core.is_superadmin()`` from the
database on every request. They also run on the tenant connection: an operator
is a person with a session, and ``coupons_superadmin_all`` is the policy that
lets them write. Nothing here needs the service connection, and nothing here
uses it.

Payments are the tenant's half of 20260918000002. There is no gateway: an
owner asks to pay an issued invoice and is shown where to send the money
(``app.billing.payments.pay_to``, the runtime's own environment), sends it,
and types the UTR / UPI reference inside the window. Every write goes through
``core.request_payment`` / ``core.submit_payment``, which check that the
caller is an owner or admin of the invoice's organisation in SQL; this file
maps their hints to status codes and never decides for them.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from psycopg.errors import CheckViolation, InsufficientPrivilege, UniqueViolation
from pydantic import BaseModel, Field, field_validator

from app.auth.scope import (
    AuthorizedWorkspace,
    Superadmin,
    authorized_superadmin,
    authorized_workspace,
)
from app.billing import payments
from app.config import get_settings

router = APIRouter()


# ---------------------------------------------------------------------------
# Tenant: what can I buy, what am I paying, what have I been billed
# ---------------------------------------------------------------------------

PLANS_WITH_OFFERS = """
select p.id::text                as plan_id,
       p.key, p.name, p.description,
       p.price_inr, p.billing_period, p.trial_days, p.sort_order,
       coalesce(
         (select jsonb_agg(jsonb_build_object(
                    'code', c.code, 'name', c.name, 'percent_off', c.percent_off,
                    'valid_to', c.valid_to)
                  order by c.percent_off desc)
            from core.coupon_plans cp
            join core.coupons c on c.id = cp.coupon_id
           where cp.plan_id = p.id),
         '[]'::jsonb)              as coupons,
       coalesce(
         (select jsonb_object_agg(f.feature_key, f.value_json)
            from core.plan_features f where f.plan_id = p.id),
         '{}'::jsonb)              as features
  from core.plans p
 where p.product_id = t_advit.product_id()
   and p.is_active
 order by p.sort_order
"""


@router.get("/api/workspaces/{workspace_id}/billing/plans")
def plans(ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)]) -> list[dict[str, Any]]:
    """The catalogue, with the live coupons each plan will accept.

    The coupon list is whatever RLS shows this caller - active, in window, not
    exhausted - so the dropdown and the redemption function agree by
    construction rather than by two copies of the same rule.
    """
    with ws.principal.tx() as cur:
        cur.execute(PLANS_WITH_OFFERS)
        return cur.fetchall()


@router.get("/api/workspaces/{workspace_id}/billing/subscription")
def subscription(ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)]) -> dict[str, Any]:
    with ws.principal.tx() as cur:
        cur.execute(
            """
            select s.id::text as subscription_id, s.status::text as status,
                   p.key as plan_key, p.name as plan_name,
                   s.current_period_start, s.current_period_end, s.trial_ends_at,
                   s.grace_ends_at,
                   -- The same answer every guard reads, so the billing page
                   -- and the refusal it explains cannot disagree.
                   core.access_mode(s.org_id, s.product_id)::text as access_mode,
                   c.code as coupon_code,
                   e.list_price_inr, e.percent_off, e.discount_inr, e.price_inr
              from core.subscriptions s
              join core.plans p on p.id = s.plan_id
              left join core.coupons c on c.id = s.coupon_id
              cross join lateral core.effective_price(s.plan_id, s.coupon_id) e
             where s.org_id = %s::uuid
               and s.product_id = t_advit.product_id()
               and s.cancelled_at is null
            """,
            (ws.org_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "no live subscription")
    return row


class ApplyCoupon(BaseModel):
    code: str = Field(min_length=3, max_length=32)


@router.post("/api/workspaces/{workspace_id}/billing/coupon")
def apply_coupon(
    payload: ApplyCoupon,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    """Every refusal comes from core.apply_coupon with a hint naming the reason.

    Mapped to 422 rather than 403: "this coupon does not apply to your plan" is
    a fact about the request, not about who is asking.
    """
    with ws.principal.tx() as cur:
        try:
            cur.execute("select core.apply_coupon(%s::uuid, %s) as coupon_id", (ws.org_id, payload.code))
        except InsufficientPrivilege as exc:
            hint = exc.diag.message_hint or "refused"
            status = 403 if hint == "not_billing_admin" else 422
            raise HTTPException(status, f"{hint}: {exc.diag.message_primary}") from exc
        cur.fetchone()
    return subscription(ws)


@router.get("/api/workspaces/{workspace_id}/billing/invoices")
def invoices(ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)]) -> list[dict[str, Any]]:
    """Owners and admins only - invoices_org_read says so, and a media buyer
    asking gets an empty list rather than a refusal that confirms there is
    something to see."""
    with ws.principal.tx() as cur:
        cur.execute(
            """
            select id::text, number, status::text as status, period_start, period_end,
                   plan_name, list_price_inr, coupon_code, percent_off, discount_inr,
                   taxable_inr, gst_rate_percent, gst_split, cgst_inr, sgst_inr, igst_inr,
                   total_inr, issued_at, due_at, paid_at
              from core.invoices
             where org_id = %s::uuid
             order by period_start desc
            """,
            (ws.org_id,),
        )
        rows = cur.fetchall()
        # The newest payment row per invoice, whatever its state - an expired
        # or rejected one is still what happened last, and the page should say
        # "request again" rather than "never requested".
        cur.execute(
            f"""
            select distinct on (p.invoice_id) {payments.PAYMENT_COLUMNS}
            {payments.PAYMENT_FROM}
             where p.org_id = %s::uuid
             order by p.invoice_id, p.created_at desc
            """,
            (ws.org_id,),
        )
        newest = {r["invoice_id"]: payments.shape(r) for r in cur.fetchall()}
    return [{**row, "payment": newest.get(row["id"])} for row in rows]


# ---------------------------------------------------------------------------
# Tenant: paying an invoice
# ---------------------------------------------------------------------------


def _refusal(exc: Exception) -> HTTPException:
    """The SQL function's hint and message, as the status the contract names.

    42501 from the definer functions comes in two shapes: ``not_allowed`` is a
    member of the organisation who is not its owner or admin, and may be told
    so; anything else is "not found", because the caller is a stranger to the
    row and learns nothing from the shape of the refusal.
    """
    hint = getattr(exc.diag, "message_hint", None) or "refused"
    message = getattr(exc.diag, "message_primary", None) or str(exc)
    if isinstance(exc, InsufficientPrivilege):
        if hint == "not_allowed":
            return HTTPException(403, f"{hint}: {message}")
        return HTTPException(404, "not found")
    if isinstance(exc, UniqueViolation):
        return HTTPException(409, f"{hint}: {message}")
    return HTTPException(422, f"{hint}: {message}")


def _assert_visible_row_is_ours(cur, table: str, row_id: str, org_id: str) -> None:
    """A row the caller CAN see that belongs to another organisation is a
    path mismatch - an owner of two organisations naming the wrong workspace -
    and is 404 here rather than an act on the other organisation's money. A
    row the caller cannot see is left to the function, which distinguishes a
    member (403) from a stranger (404)."""
    cur.execute(f"select org_id::text as org_id from {table} where id = %s::uuid", (row_id,))
    row = cur.fetchone()
    if row is not None and row["org_id"] != org_id:
        raise HTTPException(404, "not found")


@router.get("/api/workspaces/{workspace_id}/billing/payments")
def list_payments(ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)]) -> dict[str, Any]:
    """Every payment row for the organisation, newest first, and where the
    money goes. ``pay_to`` is null until the runtime has bank details; a
    member sees an empty list, because payments_read is owner/admin only."""
    with ws.principal.tx() as cur:
        cur.execute(
            f"select {payments.PAYMENT_COLUMNS} {payments.PAYMENT_FROM} "
            "where p.org_id = %s::uuid order by p.created_at desc",
            (ws.org_id,),
        )
        rows = [payments.shape(r) for r in cur.fetchall()]
    return {"pay_to": payments.pay_to(), "payments": rows}


@router.post("/api/workspaces/{workspace_id}/billing/invoices/{invoice_id}/payment-request", status_code=201)
def request_payment(
    invoice_id: uuid.UUID,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
    response: Response,
) -> dict[str, Any]:
    """Open a payment request for an issued invoice.

    503 BEFORE anything is written when the runtime has nowhere to send the
    money: a request without bank details is a deadline the customer cannot
    meet. 201 with the new row; 200 with the existing row when one is still
    inside its window, because a reload is not a second deadline.
    """
    pay_to = payments.pay_to()
    if pay_to is None:
        raise HTTPException(
            503,
            "bank_details_unavailable: the runtime has no seller UPI id or bank account configured "
            "(SELLER_UPI_ID, or SELLER_BANK_ACCOUNT_NAME / SELLER_BANK_ACCOUNT_NUMBER / SELLER_BANK_IFSC); "
            "nothing was requested",
        )
    window_hours = get_settings().payment_window_hours
    with ws.principal.tx() as cur:
        _assert_visible_row_is_ours(cur, "core.invoices", str(invoice_id), ws.org_id)
        try:
            cur.execute(
                "select (p).id::text as id, (p).created_at = now() as created_now "
                "from core.request_payment(%s::uuid, %s) p",
                (str(invoice_id), window_hours),
            )
        except (InsufficientPrivilege, CheckViolation, UniqueViolation) as exc:
            raise _refusal(exc) from exc
        result = cur.fetchone()
        payment = payments.fetch(cur, result["id"])
    if payment is None:
        raise HTTPException(404, "not found")
    # now() is fixed for the transaction, so a row inserted in it carries
    # exactly that stamp and a row from an earlier request does not.
    if not result["created_now"]:
        response.status_code = 200
    return {"payment": payment, "pay_to": pay_to}


class SubmitPayment(BaseModel):
    method: Literal["upi", "bank_transfer"]
    # Trimmed BEFORE it is measured: a reference pasted with a trailing
    # newline is the reference, and Field bounds would have measured the
    # newline.
    reference: str
    paid_on: date | None = None

    @field_validator("reference", mode="before")
    @classmethod
    def _trimmed(cls, v: object) -> str:
        v = str(v or "").strip()
        if not 4 <= len(v) <= 64:
            raise ValueError("a UTR or UPI reference is 4 to 64 characters")
        return v


@router.post("/api/workspaces/{workspace_id}/billing/payments/{payment_id}/submit")
def submit_payment(
    payment_id: uuid.UUID,
    payload: SubmitPayment,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    """Quote the reference. Confirms nothing - the row waits for an operator.

    ``window_elapsed`` is refused with 422 and the row is moved to expired in a
    second transaction: the refusal is an exception in SQL, and an exception
    rolls back what the function wrote, so the closing has to happen where
    the refusal cannot undo it.
    """
    try:
        with ws.principal.tx() as cur:
            _assert_visible_row_is_ours(cur, "core.payments", str(payment_id), ws.org_id)
            cur.execute(
                "select (p).id::text as id from core.submit_payment(%s::uuid, %s, %s, %s) p",
                (str(payment_id), payload.method, payload.reference, payload.paid_on),
            )
            row = payments.fetch(cur, cur.fetchone()["id"])
    except CheckViolation as exc:
        # Caught OUTSIDE the transaction, which the exception has already
        # rolled back; the closing needs a transaction of its own.
        if exc.diag.message_hint == "window_elapsed":
            with ws.principal.tx() as cur:
                cur.execute("select core.expire_payment(%s::uuid)", (str(payment_id),))
        raise _refusal(exc) from exc
    except InsufficientPrivilege as exc:
        raise _refusal(exc) from exc
    if row is None:
        raise HTTPException(404, "not found")
    return row


# ---------------------------------------------------------------------------
# Operator: coupons
# ---------------------------------------------------------------------------


class CouponIn(BaseModel):
    code: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    percent_off: float = Field(gt=0, le=100)
    plan_keys: list[str] = Field(min_length=1)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    max_redemptions: int | None = Field(default=None, gt=0)


@router.get("/api/admin/coupons")
def list_coupons(admin: Annotated[Superadmin, Depends(authorized_superadmin)]) -> list[dict[str, Any]]:
    with admin.principal.tx() as cur:
        cur.execute(
            """
            select c.id::text, c.code, c.name, c.percent_off, c.valid_from, c.valid_to,
                   c.max_redemptions, c.redemptions, c.is_active, c.created_at,
                   -- The same predicate coupons_tenant_sees_live_offers applies, so
                   -- what the console calls "live" is what a tenant's dropdown shows.
                   (c.is_active
                    and c.valid_from <= now()
                    and (c.valid_to is null or c.valid_to > now())
                    and (c.max_redemptions is null or c.redemptions < c.max_redemptions)) as is_live,
                   coalesce((select array_agg(p.key order by p.sort_order)
                               from core.coupon_plans cp join core.plans p on p.id = cp.plan_id
                              where cp.coupon_id = c.id), '{}') as plan_keys
              from core.coupons c
             where c.product_id = t_advit.product_id()
             order by c.created_at desc
            """
        )
        return cur.fetchall()


@router.post("/api/admin/coupons", status_code=201)
def create_coupon(
    payload: CouponIn,
    admin: Annotated[Superadmin, Depends(authorized_superadmin)],
) -> dict[str, Any]:
    with admin.principal.tx() as cur:
        cur.execute(
            "select id::text, key from core.plans where product_id = t_advit.product_id() and key = any(%s)",
            (payload.plan_keys,),
        )
        found = {r["key"]: r["id"] for r in cur.fetchall()}
        missing = sorted(set(payload.plan_keys) - set(found))
        if missing:
            raise HTTPException(422, f"no such plan(s): {', '.join(missing)}")

        try:
            cur.execute(
                """
                insert into core.coupons
                  (product_id, code, name, percent_off, valid_from, valid_to, max_redemptions, created_by)
                values (t_advit.product_id(), %s, %s, %s, coalesce(%s, now()), %s, %s, auth.uid())
                returning id::text, code
                """,
                (payload.code, payload.name, payload.percent_off,
                 payload.valid_from, payload.valid_to, payload.max_redemptions),
            )
            coupon = cur.fetchone()
            for plan_id in found.values():
                cur.execute(
                    "insert into core.coupon_plans (coupon_id, plan_id) values (%s::uuid, %s::uuid)",
                    (coupon["id"], plan_id),
                )
        except UniqueViolation as exc:
            raise HTTPException(409, f"a coupon with code {payload.code.upper()!r} already exists") from exc
        except CheckViolation as exc:
            raise HTTPException(422, exc.diag.message_primary or "invalid coupon") from exc

        cur.execute(
            """
            select core.log_audit('platform', 'coupon.created',
                                  p_actor_type => 'superadmin', p_actor => auth.uid(),
                                  p_payload => %s::jsonb)
            """,
            (json.dumps({"code": coupon["code"], "percent_off": payload.percent_off,
                         "plans": sorted(found)}),),
        )
    return {"id": coupon["id"], "code": coupon["code"], "plan_keys": sorted(found)}


class CouponPatch(BaseModel):
    is_active: bool | None = None
    valid_to: datetime | None = None
    max_redemptions: int | None = Field(default=None, gt=0)


@router.patch("/api/admin/coupons/{coupon_id}")
def update_coupon(
    coupon_id: uuid.UUID,
    payload: CouponPatch,
    admin: Annotated[Superadmin, Depends(authorized_superadmin)],
) -> dict[str, Any]:
    """Deactivate, close the window, or change the cap. The code and the
    percentage are not editable: a coupon that has been redeemed at 25% must
    not later read 40% on the invoice it is named on. Make a new coupon."""
    sets, args = [], []
    if payload.is_active is not None:
        sets.append("is_active = %s"); args.append(payload.is_active)
    if payload.valid_to is not None:
        sets.append("valid_to = %s"); args.append(payload.valid_to)
    if payload.max_redemptions is not None:
        sets.append("max_redemptions = %s"); args.append(payload.max_redemptions)
    if not sets:
        raise HTTPException(422, "nothing to change")

    with admin.principal.tx() as cur:
        try:
            cur.execute(
                f"update core.coupons set {', '.join(sets)} where id = %s::uuid "
                "returning id::text, code, is_active, valid_to, max_redemptions, redemptions",
                (*args, str(coupon_id)),
            )
        except CheckViolation as exc:
            raise HTTPException(422, exc.diag.message_primary or "invalid change") from exc
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "coupon not found")
    return row
