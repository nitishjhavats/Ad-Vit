"""The operator's console, as an API: organisations, the Platform Watch inbox,
the trail, and the two acts the console exists to perform.

Every route here depends on ``authorized_superadmin`` and runs on the TENANT
connection under the operator's own claims. That is not a convenience; it is
the design. A superadmin is a person with a session, every table they touch
carries an ``or core.is_superadmin()`` branch in its policies, and the two
writes that had no such branch - an organisation's status, a rule's ``as_of`` -
got one in 20260917000001 with the guard INSIDE the database. Nothing in this
file reaches the connection that is not subject to RLS, and
``test_service_connection_surface`` will say so if that changes.

The consequence worth stating: a bug in this file cannot widen what the
operator may do. The worst it can do is refuse.

Coupons live in ``routes_billing`` beside the tenant's view of them, so the
dropdown and the redemption stay one file apart rather than one repository
apart.

Onboarding - creating an organisation - is the one act here that may leave the
database: an owner with no account yet is invited through GoTrue's admin API,
by ``app.auth.invite``. That is HTTP to the auth service, not a connection, and
it creates an auth user and nothing else; every row the organisation is made of
is still written on the tenant connection under the operator's own claims.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.errors import CheckViolation, InsufficientPrivilege, UniqueViolation
from pydantic import BaseModel, Field

from app.auth.invite import (
    InviteRefused,
    InviteUnavailable,
    generate_invite,
    generate_sign_in_link,
    invites_are_possible,
    missing_invite_configuration,
)
from app.auth.scope import Superadmin, authorized_superadmin
from app.billing import payments
from app.policy.rules import freshness_window

router = APIRouter(prefix="/api/admin")

Admin = Annotated[Superadmin, Depends(authorized_superadmin)]


# ---------------------------------------------------------------------------
# Who am I, and what is waiting
# ---------------------------------------------------------------------------


@router.get("/me")
def me(admin: Admin) -> dict[str, Any]:
    """The console's gate. A tenant gets the same 404 every /api/admin route
    gives, from ``authorized_superadmin`` - this route adds nothing to that
    decision, it only reports it."""
    with admin.principal.tx() as cur:
        cur.execute(
            "select id::text as user_id, email, full_name from core.platform_users where id = auth.uid()"
        )
        row = cur.fetchone()
    return row or {"user_id": admin.principal.subject, "email": None, "full_name": None}


@router.get("/overview")
def overview(admin: Admin) -> dict[str, Any]:
    """Counts, so the first screen says what needs a person before any list
    is opened. Every number is a query the operator could run by hand on the
    same connection; nothing is cached or derived elsewhere."""
    with admin.principal.tx() as cur:
        cur.execute(
            """
            select
              (select jsonb_object_agg(status, n) from (
                 select status::text as status, count(*) as n from core.organisations group by 1) s)
                                                         as organisations_by_status,
              (select jsonb_object_agg(severity, n) from (
                 select severity, count(*) as n from t_advit.watch_findings
                  where acknowledged_at is null group by 1) f)
                                                         as open_findings_by_severity,
              (select count(*) from core.invoices where status = 'draft')  as draft_invoices,
              (select count(*) from core.invoices
                where status = 'issued' and paid_at is null)                as unpaid_invoices,
              -- References typed by customers that nobody has matched to the
              -- bank statement yet. Each one is a person waiting.
              (select count(*) from core.payments where status = 'submitted') as payments_submitted,
              (select count(*) from core.coupons
                where is_active and (valid_to is null or valid_to > now()))  as live_coupons,
              (select count(*) from core.subscriptions
                where cancelled_at is null and status in ('past_due','grace','expired','suspended'))
                                                         as subscriptions_needing_attention
            """
        )
        row = cur.fetchone()
    return {
        "organisations_by_status": row["organisations_by_status"] or {},
        "open_findings_by_severity": row["open_findings_by_severity"] or {},
        "draft_invoices": row["draft_invoices"],
        "unpaid_invoices": row["unpaid_invoices"],
        "payments_submitted": row["payments_submitted"],
        "live_coupons": row["live_coupons"],
        "subscriptions_needing_attention": row["subscriptions_needing_attention"],
    }


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


@router.get("/plans")
def plans(admin: Admin) -> list[dict[str, Any]]:
    """Every plan for this product, active or not - plans_read shows a
    superadmin the inactive ones - with its features and how many live
    subscriptions sit on it. The console picks plan keys from this list."""
    with admin.principal.tx() as cur:
        cur.execute(
            """
            select p.id::text as plan_id, p.key, p.name, p.description, p.price_inr, p.billing_period,
                   p.trial_days, p.grace_days, p.is_active, p.sort_order,
                   coalesce((select jsonb_object_agg(f.feature_key, f.value_json)
                               from core.plan_features f where f.plan_id = p.id), '{}'::jsonb) as features,
                   (select count(*) from core.subscriptions s
                     where s.plan_id = p.id and s.cancelled_at is null)                       as subscriptions
              from core.plans p
             where p.product_id = t_advit.product_id()
             order by p.sort_order, p.key
            """
        )
        return cur.fetchall()


@router.get("/industries")
def industries(admin: Admin) -> list[dict[str, Any]]:
    """Every industry pack with its status - industries_select shows a
    superadmin the drafts too. The console picks a key from this list and
    onboarding refuses anything that is not active, so a draft in the list is
    information, not an option."""
    with admin.principal.tx() as cur:
        cur.execute(
            "select key, display_name, status::text as status, pack_id, summary "
            "from t_advit.industries order by status = 'active' desc, display_name"
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Organisations
# ---------------------------------------------------------------------------

ORGANISATIONS = """
select o.id::text, o.name, o.slug::text as slug, o.status::text as status,
       o.legal_name, o.gstin, o.state_code, o.billing_email::text as billing_email,
       o.activated_at, o.suspended_at, o.suspension_reason, o.created_at,
       p.key                as plan_key,
       p.name               as plan_name,
       s.status::text       as subscription_status,
       s.current_period_end,
       c.code               as coupon_code,
       (select count(*) from core.organisation_members m where m.org_id = o.id)      as members,
       (select count(*) from t_advit.workspaces w where w.org_id = o.id)             as workspaces,
       (select count(*) from t_advit.meta_connections mc
          join t_advit.workspaces w on w.id = mc.workspace_id where w.org_id = o.id) as ad_accounts,
       (select count(*) from core.entitlement_overrides eo
         where eo.org_id = o.id and (eo.expires_at is null or eo.expires_at > now())) as overrides,
       (o.gstin is not null and o.state_code is not null)                            as billing_ready
  from core.organisations o
  left join core.subscriptions s
         on s.org_id = o.id and s.product_id = t_advit.product_id() and s.cancelled_at is null
  left join core.plans p   on p.id = s.plan_id
  left join core.coupons c on c.id = s.coupon_id
"""


@router.get("/organisations")
def organisations(admin: Admin) -> list[dict[str, Any]]:
    with admin.principal.tx() as cur:
        cur.execute(ORGANISATIONS + " order by o.created_at desc")
        return cur.fetchall()


@router.get("/organisations/{org_id}")
def organisation(org_id: uuid.UUID, admin: Admin) -> dict[str, Any]:
    with admin.principal.tx() as cur:
        cur.execute(ORGANISATIONS + " where o.id = %s::uuid", (str(org_id),))
        org = cur.fetchone()
        if org is None:
            raise HTTPException(404, "organisation not found")

        cur.execute(
            """
            select m.user_id::text, u.email, u.full_name, m.role::text as role, m.joined_at
              from core.organisation_members m join core.platform_users u on u.id = m.user_id
             where m.org_id = %s::uuid order by m.joined_at
            """,
            (str(org_id),),
        )
        members = cur.fetchall()

        cur.execute(
            """
            select w.id::text, w.name, w.industry_key, w.autonomy_level, w.is_paused,
                   w.daily_cap_inr, w.monthly_cap_inr,
                   coalesce((select jsonb_agg(jsonb_build_object(
                               'ad_account_id', mc.ad_account_id, 'health', mc.health,
                               'write_enabled', mc.write_enabled))
                              from t_advit.meta_connections mc where mc.workspace_id = w.id), '[]'::jsonb)
                                                                    as ad_accounts
              from t_advit.workspaces w where w.org_id = %s::uuid order by w.created_at
            """,
            (str(org_id),),
        )
        workspaces = cur.fetchall()

        # Resolved entitlements with their source, from the same function the
        # tenant's own plan page reads. 'override' / 'plan' / 'default' is the
        # column that tells the operator whether a number is theirs to change
        # here or a fact about the plan.
        cur.execute(
            """
            select e.feature_key, e.value_json as value, e.value_type::text as value_type, e.source,
                   fd.name, o.reason, o.expires_at, o.set_by::text as set_by
              from core.org_entitlements(%s::uuid) e
              join core.feature_definitions fd on fd.key = e.feature_key
              left join core.entitlement_overrides o
                     on o.org_id = %s::uuid and o.feature_key = e.feature_key
                    and (o.expires_at is null or o.expires_at > now())
             order by e.feature_key
            """,
            (str(org_id), str(org_id)),
        )
        entitlements = cur.fetchall()

        cur.execute(
            """
            select id::text, number, status::text as status, period_start, period_end,
                   total_inr, gst_split, issued_at, due_at, paid_at,
                   buyer_state_code, seller_gstin
              from core.invoices where org_id = %s::uuid order by period_start desc limit 24
            """,
            (str(org_id),),
        )
        invoices = [_with_draft_reason(r) for r in cur.fetchall()]

    return {**org, "members": members, "workspaces": workspaces,
            "entitlements": entitlements, "invoices": invoices}


# A slug is what the derivation below can produce and nothing else, so a
# hand-typed one is held to the same shape rather than to a looser one.
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Every workspace starts under a cap it cannot exceed (PRD 6.1 step 8: there
# is no "unlimited", and the column has no default on purpose). These are the
# smaller of the two seeded workspaces - a first month's ceiling the owner
# raises deliberately, not one that is raised for them.
DEFAULT_DAILY_CAP_INR = 2000
DEFAULT_MONTHLY_CAP_INR = 50000


def derive_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class NewOrganisation(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    slug: str | None = Field(default=None, min_length=3, max_length=64)
    plan_key: str = Field(min_length=1, max_length=64)
    industry_key: str = Field(min_length=1, max_length=64)
    workspace_name: str = Field(min_length=1, max_length=120)
    owner_email: str = Field(min_length=3, max_length=254)
    trial: bool = True
    daily_cap_inr: int = Field(default=DEFAULT_DAILY_CAP_INR, gt=0)
    monthly_cap_inr: int = Field(default=DEFAULT_MONTHLY_CAP_INR, gt=0)


# Plans the catalogue keeps for existing subscriptions only. The rule is here
# rather than in a form default because a default steers and a route refuses.
NOT_FOR_NEW_SIGNUPS = frozenset({"standard"})


@router.post("/organisations", status_code=201)
def create_organisation(payload: NewOrganisation, admin: Admin) -> dict[str, Any]:
    """The first step of onboarding: an organisation, its subscription, its
    first workspace and its owner, in ONE transaction on the tenant connection.

    Every INSERT below is one the policies already let a superadmin make -
    organisations_insert_superadmin, subscriptions_write, workspaces_write and
    org_members_write through has_org_role, workspace_members_write through
    workspace_org. This route adds the order and the refusals; it does not add
    a privilege.

    The owner is the only part that can leave the database. An email that is
    already a platform user becomes the owner outright. One that is not needs
    an auth user, which only GoTrue may create: with the service-role key
    configured the user is invited and the ``action_link`` is returned for the
    operator to send by hand (nothing here sends email) - our own
    ``/auth/confirm`` link on the web app, which signs the owner in and asks
    for a password, not GoTrue's, which lands nowhere; without the key the whole
    act is refused before a row is written, because an organisation whose owner
    cannot sign in is not onboarded, it is stranded.
    """
    slug = payload.slug.strip().lower() if payload.slug else derive_slug(payload.name)
    if not _SLUG.match(slug):
        raise HTTPException(422, f"slug_invalid: {slug!r} is not lowercase words joined by single hyphens")
    owner_email = payload.owner_email.strip().lower()
    if not _EMAIL.match(owner_email):
        raise HTTPException(422, f"owner_email_invalid: {payload.owner_email!r} is not an email address")
    if payload.monthly_cap_inr < payload.daily_cap_inr:
        raise HTTPException(422, "caps_inverted: the monthly cap must cover the daily cap")

    with admin.principal.tx() as cur:
        # What the organisation is made of has to exist before anything is
        # written, and each absence is named: the operator fixes a typo in the
        # form, not in the database.
        cur.execute(
            "select id::text, trial_days, billing_period::text as billing_period, is_active "
            "from core.plans where key = %s and product_id = t_advit.product_id()",
            (payload.plan_key,),
        )
        plan = cur.fetchone()
        if plan is None:
            raise HTTPException(422, f"plan_unknown: no such plan: {payload.plan_key}")
        if not plan["is_active"]:
            raise HTTPException(422, f"plan_inactive: {payload.plan_key} is not sold any more")
        if payload.plan_key in NOT_FOR_NEW_SIGNUPS:
            # 20260917000002 keeps `standard` active for the subscriptions
            # already on it and says new sign-ups choose a PRD 19.3 tier. A
            # form default would only steer; this is the rule.
            raise HTTPException(
                422, f"plan_not_for_new_signups: {payload.plan_key} is kept for existing "
                     "subscriptions; a new organisation starts on a PRD 19.3 tier")
        if payload.trial and plan["trial_days"] <= 0:
            raise HTTPException(422, f"plan_has_no_trial: {payload.plan_key} carries no trial days")

        cur.execute("select status::text as status from t_advit.industries where key = %s",
                    (payload.industry_key,))
        industry = cur.fetchone()
        if industry is None:
            raise HTTPException(422, f"industry_unknown: no such industry: {payload.industry_key}")
        if industry["status"] != "active":
            raise HTTPException(
                422, f"industry_not_active: {payload.industry_key} is {industry['status']}; "
                     "no new workspace may start on it")

        cur.execute("select 1 from core.organisations where slug = %s", (slug,))
        if cur.fetchone() is not None:
            raise HTTPException(409, f"slug_taken: an organisation with slug {slug!r} already exists")

        cur.execute("select id::text, is_active from core.platform_users where email = %s", (owner_email,))
        existing = cur.fetchone()
        if existing is not None and not existing["is_active"]:
            raise HTTPException(
                422, f"owner_inactive: {owner_email} is a deactivated platform user and cannot own "
                     "an organisation; reactivate the user or name another owner")
        if existing is None and not invites_are_possible():
            # Nothing has been written yet, and nothing will be: the exception
            # unwinds the transaction. The hint names the variable(s) and the
            # one other way forward.
            missing = " and ".join(missing_invite_configuration())
            raise HTTPException(
                422,
                f"owner_unknown: no platform user has the email {owner_email}, and "
                f"{missing} not configured on the runtime, so it cannot invite one. "
                "Set it, or create the user in Supabase Studio (Authentication, Add user) "
                "first, then create the organisation again.",
            )

        # The one call that leaves the database happens now, after every check
        # and before any row: a GoTrue refusal costs nothing, and a failure in
        # the writes that follow leaves an invited user but no half-made
        # organisation to confuse the retry. It is still inside the
        # transaction only so the platform_users row the trigger provisions is
        # visible to the membership INSERT below.
        invited = existing is None
        action_link: str | None = None
        if existing is not None:
            owner_id = existing["id"]
        else:
            try:
                invitation = generate_invite(owner_email)
            except InviteUnavailable as exc:
                raise HTTPException(503, f"invite_unavailable: {exc}") from exc
            except InviteRefused as exc:
                raise HTTPException(502, f"invite_refused: {exc}") from exc
            owner_id = invitation.user_id
            # The link the operator sends is ours. GoTrue's own is not
            # returned: two links on one screen and one of them lands nowhere
            # is a support ticket, and the one-time hash is in both.
            action_link = invitation.link
            # GoTrue committed the user on its own connection; the trigger's
            # platform_users row is what the membership FK needs. Absent, the
            # organisation would be owned by nobody, so nothing is written and
            # the operator sees why.
            cur.execute("select 1 from core.platform_users where id = %s::uuid", (owner_id,))
            if cur.fetchone() is None:
                raise HTTPException(
                    502, f"invite_unprovisioned: Supabase Auth created {owner_email} but "
                         "core.platform_users has no row for it; is the on_auth_user_created "
                         "trigger installed?")

        try:
            cur.execute(
                """
                insert into core.organisations (name, slug, status, billing_email, created_by, activated_at)
                values (%s, %s, 'active', %s, auth.uid(), now())
                returning id::text
                """,
                (payload.name.strip(), slug, owner_email),
            )
        except UniqueViolation as exc:
            raise HTTPException(409, f"slug_taken: an organisation with slug {slug!r} already exists") from exc
        org_id = cur.fetchone()["id"]

        # The period is the plan's, in SQL, so a quarterly or yearly plan does
        # not get a monthly period because somebody wrote `+ 1 month` here.
        cur.execute(
            """
            insert into core.subscriptions
                   (org_id, product_id, plan_id, status, trial_ends_at, current_period_start, current_period_end)
            select %s::uuid, t_advit.product_id(), p.id,
                   case when %s then 'trialing' else 'active' end::core.subscription_status,
                   case when %s then now() + make_interval(days => p.trial_days) end,
                   now(),
                   now() + case p.billing_period
                             when 'monthly'   then interval '1 month'
                             when 'quarterly' then interval '3 months'
                             when 'yearly'    then interval '1 year'
                           end
              from core.plans p where p.id = %s::uuid
            returning id::text, status::text as status, trial_ends_at, current_period_end
            """,
            (org_id, payload.trial, payload.trial, plan["id"]),
        )
        subscription = cur.fetchone()

        cur.execute(
            """
            insert into t_advit.workspaces (org_id, name, industry_key, daily_cap_inr, monthly_cap_inr)
            values (%s::uuid, %s, %s, %s, %s)
            returning id::text
            """,
            (org_id, payload.workspace_name.strip(), payload.industry_key,
             payload.daily_cap_inr, payload.monthly_cap_inr),
        )
        workspace_id = cur.fetchone()["id"]

        # The pack's starting hypotheses, copied as the table's own comment
        # says onboarding does: "Copied into t_advit.account_context at
        # onboarding with source = 'industry_pack'". A tenant row, because the
        # OS tests these rather than trusting them.
        cur.execute(
            """
            insert into t_advit.account_context (workspace_id, dimension, key, value_json, confidence, source)
            select %s::uuid, d.dimension, d.key, d.value_json, d.confidence, 'industry_pack'
              from t_advit.industry_context_defaults d where d.industry_key = %s
            """,
            (workspace_id, payload.industry_key),
        )

        cur.execute(
            "insert into core.organisation_members (org_id, user_id, role, invited_by) "
            "values (%s::uuid, %s::uuid, 'owner', auth.uid())",
            (org_id, owner_id),
        )
        # An org owner already satisfies t_advit.is_workspace_member through
        # has_org_role; the explicit grant is what the workspace's own member
        # list shows, and what survives a later demotion to `member`.
        cur.execute(
            "insert into t_advit.workspace_members (workspace_id, user_id, role, added_by) "
            "values (%s::uuid, %s::uuid, 'marketing_manager', auth.uid())",
            (workspace_id, owner_id),
        )

        cur.execute(
            """
            select core.log_audit('organisation', 'organisation.created', p_org => %s::uuid,
                                  p_actor_type => 'superadmin', p_actor => auth.uid(),
                                  p_payload => %s::jsonb)
            """,
            (org_id, json.dumps({
                "plan_key": payload.plan_key, "industry_key": payload.industry_key,
                "workspace_id": workspace_id, "owner_email": owner_email, "invited": invited,
            })),
        )

    return {
        "org_id": org_id,
        "slug": slug,
        "workspace_id": workspace_id,
        "subscription_id": subscription["id"],
        "subscription_status": subscription["status"],
        "trial_ends_at": subscription["trial_ends_at"],
        "current_period_end": subscription["current_period_end"],
        "owner_user_id": owner_id,
        "action_link": action_link,
        "invited": invited,
        "note": ("no email was sent; send the action_link to the owner by hand - it opens "
                 "the web app's sign-up page, where they choose a password"
                 if invited else f"{owner_email} already had an account and is now the owner"),
    }


@router.post("/organisations/{org_id}/members/{user_id}/sign-in-link", status_code=201)
def issue_sign_in_link(org_id: uuid.UUID, user_id: uuid.UUID, admin: Admin) -> dict[str, Any]:
    """A fresh one-time link for a member who already has an account.

    The two cases: an invitation that expired before the owner opened it (a
    day, on the shared cluster), and a member who has forgotten their
    password and has no "forgot password" email to ask for, because nothing
    in this product sends email. Either way the account exists, GoTrue
    refuses a second ``invite`` for it, and a ``recovery`` link is the one
    whose name says what it permits: sign in once, choose a password.

    Scoped to a member of the named organisation rather than to any email
    the operator can type: the console shows this button beside a member
    row, and an operator who can mint a sign-in link for an arbitrary address
    can sign in as anybody. Who the link was issued for is written to the
    audit trail; the link itself is not, anywhere - a link in a log line is
    a sign-in in a log line.
    """
    with admin.principal.tx() as cur:
        cur.execute(
            """
            select u.email, u.is_active, m.role::text as role
              from core.organisation_members m
              join core.platform_users u on u.id = m.user_id
             where m.org_id = %s::uuid and m.user_id = %s::uuid
            """,
            (str(org_id), str(user_id)),
        )
        member = cur.fetchone()
        # One 404 for "no such organisation" and "not a member of it": the
        # route does not confirm a user id exists by refusing differently.
        if member is None:
            raise HTTPException(404, "member not found")
        if not member["is_active"]:
            raise HTTPException(
                422, f"member_inactive: {member['email']} is deactivated; a sign-in link for "
                     "a deactivated user would sign in somebody the platform has turned off")
        if not invites_are_possible():
            raise HTTPException(
                503, f"link_unavailable: {' and '.join(missing_invite_configuration())} not "
                     "configured on the runtime, so it cannot issue a link")
        try:
            issued = generate_sign_in_link(member["email"])
        except InviteUnavailable as exc:
            raise HTTPException(503, f"link_unavailable: {exc}") from exc
        except InviteRefused as exc:
            raise HTTPException(502, f"link_refused: {exc}") from exc
        # GoTrue answered for the address we sent; a link for some other user
        # id would be a link for somebody else, and is not handed out.
        if issued.user_id != str(user_id):
            raise HTTPException(
                502, "link_refused: Supabase Auth issued a link for a different user than the "
                     "member asked for; nothing was handed out")
        cur.execute(
            """
            select core.log_audit('organisation', 'organisation.sign_in_link_issued',
                                  p_org => %s::uuid, p_actor_type => 'superadmin',
                                  p_actor => auth.uid(), p_payload => %s::jsonb)
            """,
            (str(org_id), json.dumps({
                "user_id": str(user_id), "email": member["email"], "role": member["role"],
                "link_type": "recovery",
            })),
        )
    return {
        "org_id": str(org_id),
        "user_id": str(user_id),
        "email": member["email"],
        "action_link": issued.link,
        "note": ("no email was sent; send the action_link by hand - it opens the web app, "
                 "signs the holder in once and asks them to choose a password"),
    }


class OrgStatus(BaseModel):
    status: Literal["active", "suspended", "pending_activation"]
    reason: str | None = Field(default=None, max_length=500)


@router.patch("/organisations/{org_id}/status")
def set_org_status(org_id: uuid.UUID, payload: OrgStatus, admin: Admin) -> dict[str, Any]:
    """Through ``core.set_organisation_status`` and nothing else: the tenant's
    UPDATE grant no longer names the column, and the function carries the
    superadmin check, the reason requirement and the audit row itself."""
    with admin.principal.tx() as cur:
        try:
            cur.execute(
                "select (o).id::text as id, (o).status::text as status, (o).suspension_reason, "
                "(o).activated_at, (o).suspended_at "
                "from core.set_organisation_status(%s::uuid, %s::core.org_status, %s) o",
                (str(org_id), payload.status, payload.reason),
            )
        except CheckViolation as exc:
            raise HTTPException(422, f"{exc.diag.message_hint}: {exc.diag.message_primary}") from exc
        except InsufficientPrivilege as exc:
            # org_unknown is the only hint reachable here: not_superadmin was
            # decided by authorized_superadmin before this function ran.
            raise HTTPException(404, "organisation not found") from exc
        return cur.fetchone()


class SubscriptionPatch(BaseModel):
    plan_key: str | None = Field(default=None, min_length=1, max_length=64)
    status: Literal["trialing", "pending_payment", "active", "past_due", "grace", "expired", "suspended"] | None = None
    reason: str | None = Field(default=None, max_length=500)


@router.patch("/organisations/{org_id}/subscription")
def patch_subscription(org_id: uuid.UUID, payload: SubscriptionPatch, admin: Admin) -> dict[str, Any]:
    """Move an organisation between plans or subscription states.

    A plan change takes effect on the next invoice - ``raise_invoices`` reads
    the plan at the period start it is invoicing, and the one already issued
    for this period is a snapshot that does not move. The reason is required
    for anything that narrows access, because "why is this customer on grace"
    is the question the trail exists to answer.
    """
    if payload.plan_key is None and payload.status is None:
        raise HTTPException(422, "nothing to change")
    narrowing = payload.status in ("past_due", "grace", "expired", "suspended")
    if narrowing and not (payload.reason or "").strip():
        raise HTTPException(422, "reason_required: narrowing access needs a reason")

    sets: list[str] = []
    args: list[Any] = []
    with admin.principal.tx() as cur:
        if payload.plan_key is not None:
            cur.execute(
                "select id::text from core.plans where key = %s and product_id = t_advit.product_id()",
                (payload.plan_key,),
            )
            plan = cur.fetchone()
            if plan is None:
                raise HTTPException(422, f"no such plan: {payload.plan_key}")
            sets.append("plan_id = %s::uuid"); args.append(plan["id"])
        if payload.status is not None:
            sets.append("status = %s::core.subscription_status"); args.append(payload.status)

        cur.execute(
            "select s.id::text, p.key as plan_key, s.status::text as status "
            "from core.subscriptions s join core.plans p on p.id = s.plan_id "
            "where s.org_id = %s::uuid and s.product_id = t_advit.product_id() and s.cancelled_at is null",
            (str(org_id),),
        )
        before = cur.fetchone()
        if before is None:
            raise HTTPException(404, "no live subscription for that organisation")

        cur.execute(
            f"update core.subscriptions set {', '.join(sets)} where id = %s::uuid "
            "returning id::text, status::text as status, current_period_start, current_period_end",
            (*args, before["id"]),
        )
        after = cur.fetchone()
        cur.execute(
            """
            select core.log_audit('organisation', 'subscription.changed', p_org => %s::uuid,
                                  p_actor_type => 'superadmin', p_actor => auth.uid(),
                                  p_payload => %s::jsonb)
            """,
            (str(org_id), json.dumps({
                "from": {"plan_key": before["plan_key"], "status": before["status"]},
                "to": {"plan_key": payload.plan_key or before["plan_key"],
                       "status": payload.status or before["status"]},
                "reason": payload.reason,
            })),
        )
    return {**after, "plan_key": payload.plan_key or before["plan_key"]}


class Override(BaseModel):
    value: int | bool | str
    reason: str = Field(min_length=3, max_length=500)
    expires_at: datetime | None = None


@router.put("/organisations/{org_id}/entitlements/{feature_key}")
def set_override(org_id: uuid.UUID, feature_key: str, payload: Override, admin: Admin) -> dict[str, Any]:
    """An override, upserted. The value guard (20260910000004) types it against
    the feature definition; the stamp trigger (20260911000009) writes set_by
    from the session and the audit row. This route contributes the SQL and the
    error mapping and nothing that decides."""
    with admin.principal.tx() as cur:
        try:
            cur.execute(
                """
                insert into core.entitlement_overrides (org_id, feature_key, value_json, reason, expires_at)
                values (%s::uuid, %s, %s::jsonb, %s, %s)
                on conflict (org_id, feature_key) do update
                    set value_json = excluded.value_json,
                        reason     = excluded.reason,
                        expires_at = excluded.expires_at
                returning org_id::text, feature_key, value_json as value, reason, expires_at, set_by::text as set_by
                """,
                (str(org_id), feature_key, json.dumps(payload.value), payload.reason, payload.expires_at),
            )
        except CheckViolation as exc:
            raise HTTPException(422, f"{exc.diag.message_hint}: {exc.diag.message_primary}") from exc
        row = cur.fetchone()
    return row


@router.delete("/organisations/{org_id}/entitlements/{feature_key}")
def clear_override(org_id: uuid.UUID, feature_key: str, admin: Admin) -> dict[str, Any]:
    with admin.principal.tx() as cur:
        cur.execute(
            "delete from core.entitlement_overrides where org_id = %s::uuid and feature_key = %s returning value_json",
            (str(org_id), feature_key),
        )
        gone = cur.fetchone()
        if gone is None:
            raise HTTPException(404, "no override on that feature")
        cur.execute(
            """
            select core.log_audit('organisation', 'entitlement.cleared', p_org => %s::uuid,
                                  p_actor_type => 'superadmin', p_actor => auth.uid(),
                                  p_payload => %s::jsonb)
            """,
            (str(org_id), json.dumps({"feature_key": feature_key, "previous": gone["value_json"]})),
        )
    return {"org_id": str(org_id), "feature_key": feature_key, "previous": gone["value_json"]}


# ---------------------------------------------------------------------------
# Platform Watch: the inbox, and the act that answers it
# ---------------------------------------------------------------------------


@router.get("/watch/findings")
def findings(
    admin: Admin,
    include_acknowledged: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    with admin.principal.tx() as cur:
        cur.execute(
            """
            select f.id::text, f.detected_at, f.kind, f.subject, f.source_url, f.severity,
                   f.detail_json as detail, f.acknowledged_at, u.email as acknowledged_by
              from t_advit.watch_findings f
              left join core.platform_users u on u.id = f.acknowledged_by
             where (%s::boolean or f.acknowledged_at is null)
             order by f.acknowledged_at is not null,
                      case f.severity when 'urgent' then 0 when 'review' then 1 else 2 end,
                      f.detected_at desc
             limit %s
            """,
            (include_acknowledged, limit),
        )
        return cur.fetchall()


@router.post("/watch/findings/{finding_id}/acknowledge")
def acknowledge(finding_id: uuid.UUID, admin: Admin) -> dict[str, Any]:
    """Closes the finding. If the condition still holds tomorrow, Platform
    Watch opens a new one - acknowledging is "I have seen this", not "this is
    fixed", and the dedup index keeps the two honest."""
    with admin.principal.tx() as cur:
        cur.execute(
            """
            update t_advit.watch_findings
               set acknowledged_by = auth.uid(), acknowledged_at = now()
             where id = %s::uuid and acknowledged_at is null
             returning id::text, kind, subject, acknowledged_at
            """,
            (str(finding_id),),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "no open finding with that id")
    return row


@router.get("/rules")
def rules(admin: Admin) -> dict[str, Any]:
    """Every compliance rule and every knowledge row with its age against its
    window - the same arithmetic ``check_freshness`` runs nightly, so the list
    and the inbox cannot disagree about what is stale."""
    # The operator's date, matching the trigger that accepts a re-verification
    # and the nightly job that reports staleness.
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    with admin.principal.tx() as cur:
        cur.execute(
            """
            select code, jurisdiction, instrument, rule_type::text as rule_type, severity::text as severity,
                   title, explanation, source_url, as_of, is_active, scope::text as scope
              from t_advit.policy_rules order by jurisdiction, severity desc, code
            """
        )
        rule_rows = cur.fetchall()
        cur.execute(
            "select id::text, topic, statement, source_url, as_of, severity::text as severity, status "
            "from t_advit.platform_knowledge order by as_of, topic"
        )
        knowledge_rows = cur.fetchall()

    def _age(row: dict[str, Any], jurisdiction: str) -> dict[str, Any]:
        window = freshness_window(jurisdiction)
        age = (today - row["as_of"]).days
        return {**row, "days_old": age, "window_days": window.days, "stale": age > window.days}

    return {
        "rules": [_age(r, r["jurisdiction"]) for r in rule_rows],
        "knowledge": [_age(k, "meta") for k in knowledge_rows],
    }


class Reverify(BaseModel):
    # Defaults to today in IST on the database side; a date is accepted so an
    # operator can record a reading made yesterday. The trigger refuses the
    # future and refuses going backwards.
    as_of: date | None = None


def _reverify(cur, *, table: str, key_column: str, key_type: str, key: str,
              as_of: date | None) -> dict[str, Any] | None:
    # table / key_column / key_type are the two literal call sites below, never
    # request input; the key itself is a bound parameter.
    cur.execute(
        f"""
        update t_advit.{table}
           set as_of = coalesce(%s::date, (now() at time zone 'Asia/Kolkata')::date)
         where {key_column} = %s::{key_type}
         returning {key_column}::text as subject, as_of, source_url
        """,
        (as_of, key),
    )
    return cur.fetchone()


def _close_stale_finding(cur, *, kind: str, subject: str) -> bool:
    cur.execute(
        """
        update t_advit.watch_findings
           set acknowledged_by = auth.uid(), acknowledged_at = now()
         where kind = %s and subject = %s and acknowledged_at is null
        """,
        (kind, subject),
    )
    return cur.rowcount > 0


@router.post("/rules/{code}/reverify")
def reverify_rule(code: str, payload: Reverify, admin: Admin) -> dict[str, Any]:
    """The human act Platform Watch asks for: "I have re-read the source and
    the rule still holds." Writes ``as_of`` - the only column a session may
    write on that table - and closes the ``rule_stale`` finding about it in
    the same transaction, so the inbox and the rule agree.

    It does NOT touch the pattern. If the source changed and the rule must
    change with it, that is a migration, reviewed, with the statute beside it.
    """
    with admin.principal.tx() as cur:
        try:
            row = _reverify(cur, table="policy_rules", key_column="code", key_type="text",
                            key=code, as_of=payload.as_of)
        except CheckViolation as exc:
            raise HTTPException(422, f"{exc.diag.message_hint}: {exc.diag.message_primary}") from exc
        if row is None:
            raise HTTPException(404, "no such rule")
        closed = _close_stale_finding(cur, kind="rule_stale", subject=code)
    return {**row, "finding_closed": closed}


@router.post("/knowledge/{knowledge_id}/reverify")
def reverify_knowledge(knowledge_id: uuid.UUID, payload: Reverify, admin: Admin) -> dict[str, Any]:
    with admin.principal.tx() as cur:
        try:
            row = _reverify(cur, table="platform_knowledge", key_column="id", key_type="uuid",
                            key=str(knowledge_id), as_of=payload.as_of)
        except CheckViolation as exc:
            raise HTTPException(422, f"{exc.diag.message_hint}: {exc.diag.message_primary}") from exc
        if row is None:
            raise HTTPException(404, "no such knowledge row")
        closed = _close_stale_finding(cur, kind="knowledge_stale", subject=str(knowledge_id))
    return {**row, "finding_closed": closed}


# ---------------------------------------------------------------------------
# Invoices and the trail
# ---------------------------------------------------------------------------


def _with_draft_reason(row: dict[str, Any]) -> dict[str, Any]:
    """The reason a draft is a draft, derived from the row rather than stored:
    the two conditions are the two nullable columns, and a stored string could
    drift from them."""
    reason = None
    if row["status"] == "draft":
        if not row.get("seller_gstin"):
            reason = "seller GSTIN / state code not configured (SELLER_GSTIN, SELLER_STATE_CODE)"
        elif not row.get("buyer_state_code"):
            reason = "buyer state code missing on the organisation"
        else:
            reason = "draft"
    return {**row, "draft_reason": reason}


@router.get("/invoices")
def invoices(
    admin: Admin,
    status: Literal["draft", "issued", "paid", "void"] | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    with admin.principal.tx() as cur:
        cur.execute(
            """
            select i.id::text, i.number, i.status::text as status, i.org_id::text as org_id, o.name as org_name,
                   i.period_start, i.period_end, i.plan_name, i.list_price_inr, i.coupon_code, i.percent_off,
                   i.discount_inr, i.taxable_inr, i.gst_rate_percent, i.gst_split, i.cgst_inr, i.sgst_inr,
                   i.igst_inr, i.total_inr, i.buyer_legal_name, i.buyer_gstin, i.buyer_state_code,
                   i.seller_gstin, i.issued_at, i.due_at, i.paid_at
              from core.invoices i join core.organisations o on o.id = i.org_id
             where (%s::text is null or i.status::text = %s::text)
             order by i.status = 'draft' desc, i.period_start desc, o.name
             limit %s
            """,
            (status, status, limit),
        )
        return [_with_draft_reason(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Payments: the references waiting for a person
# ---------------------------------------------------------------------------

# The operator's view of a payment: the tenant's columns plus who it is from.
# The organisation and the submitter are joined on the tenant connection under
# the operator's claims - organisations and platform_users both show a
# superadmin every row - so nothing here reaches past RLS.
ADMIN_PAYMENT_SELECT = f"""
select {payments.PAYMENT_COLUMNS},
       p.org_id::text as org_id,
       o.name         as org_name,
       o.slug::text   as org_slug,
       u.email        as submitted_by_email
{payments.PAYMENT_FROM}
  join core.organisations o on o.id = p.org_id
  left join core.platform_users u on u.id = p.submitted_by
"""


@router.get("/payments")
def list_payments(
    admin: Admin,
    status: Literal["submitted", "all"] = Query(default="submitted"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """The inbox. Default is what needs a person - references submitted and
    not yet matched - newest submission first; ``all`` is the ledger."""
    with admin.principal.tx() as cur:
        cur.execute(
            ADMIN_PAYMENT_SELECT
            + """
             where (%s::text = 'all' or p.status::text = %s::text)
             order by p.status = 'submitted' desc, p.submitted_at desc nulls last, p.created_at desc
             limit %s
            """,
            (status, status, limit),
        )
        return [payments.shape(r) for r in cur.fetchall()]


class Review(BaseModel):
    verdict: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=500)


@router.post("/payments/{payment_id}/review")
def review_payment(payment_id: uuid.UUID, payload: Review, admin: Admin) -> dict[str, Any]:
    """Through ``core.review_payment`` and nothing else: the superadmin check,
    the reason requirement, the invoice's paid stamp and the subscription's
    restore are all inside it. What comes back is read from the tables the
    function wrote, not from the request."""
    with admin.principal.tx() as cur:
        try:
            cur.execute(
                "select (p).id::text as id, (p).invoice_id::text as invoice_id, "
                "(p).subscription_id::text as subscription_id "
                "from core.review_payment(%s::uuid, %s::core.payment_status, %s) p",
                (str(payment_id), payload.verdict, payload.note),
            )
        except CheckViolation as exc:
            hint = exc.diag.message_hint or "refused"
            # A verdict on a row that is not awaiting one is a conflict with
            # the row's state, not a fault in the request.
            status = 409 if hint == "payment_not_submitted" else 422
            raise HTTPException(status, f"{hint}: {exc.diag.message_primary}") from exc
        except InsufficientPrivilege as exc:
            # payment_unknown is the only hint reachable here: not_superadmin
            # was decided by authorized_superadmin before this function ran.
            raise HTTPException(404, "payment not found") from exc
        ids = cur.fetchone()

        cur.execute(ADMIN_PAYMENT_SELECT + " where p.id = %s::uuid", (ids["id"],))
        payment = payments.shape(cur.fetchone())
        cur.execute("select status::text as status from core.invoices where id = %s::uuid", (ids["invoice_id"],))
        invoice_status = cur.fetchone()["status"]
        cur.execute("select status::text as status from core.subscriptions where id = %s::uuid",
                    (ids["subscription_id"],))
        subscription_status = cur.fetchone()["status"]
    return {
        "payment": payment,
        "invoice_status": invoice_status,
        "subscription_status": subscription_status,
    }


def _trail(cur, *, org_id: str | None, event_prefix: str | None, limit: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        select a.id, a.at, a.scope::text as scope, a.org_id::text as org_id, o.name as org_name,
               a.workspace_id::text as workspace_id, a.actor_type::text as actor_type,
               a.actor_id::text as actor_id, u.email as actor_email,
               a.impersonated_by::text as impersonated_by, a.event, a.payload_json as payload
          from core.audit_log a
          left join core.organisations o on o.id = a.org_id
          left join core.platform_users u on u.id = a.actor_id
         where (%s::uuid is null or a.org_id = %s::uuid)
           and (%s::text is null or a.event like %s::text || '%%')
         order by a.id desc
         limit %s
        """,
        (org_id, org_id, event_prefix, event_prefix, limit),
    )
    return cur.fetchall()


@router.get("/audit")
def audit(
    admin: Admin,
    event_prefix: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """The whole trail, newest first. ``audit_log_read`` shows a superadmin
    every scope; the filter here narrows, never widens."""
    with admin.principal.tx() as cur:
        return _trail(cur, org_id=None, event_prefix=event_prefix, limit=limit)


@router.get("/organisations/{org_id}/audit")
def organisation_audit(
    org_id: uuid.UUID,
    admin: Admin,
    event_prefix: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """One organisation's trail. The organisation is in the PATH, like every
    other id that names a tenant - the boot-time route audit refuses an org_id
    query parameter anywhere, and an operator's filter is no exception to a
    rule whose value is that it has none."""
    with admin.principal.tx() as cur:
        return _trail(cur, org_id=str(org_id), event_prefix=event_prefix, limit=limit)
