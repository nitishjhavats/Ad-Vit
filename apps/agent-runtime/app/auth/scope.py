"""A route may not take ``workspace_id: str``. It takes an ``AuthorizedWorkspace``.

Exactly four dependencies and one job-only helper can produce one, and every
request-facing producer proves ownership with a SELECT on the **tenant**
connection — so the answer comes from the RLS policies in ``supabase/migrations``
rather than from a Python ``if``:

  * ``authorized_workspace()``  — the path parameter, for ``/api/workspaces/{id}/…``
  * ``authorized_action()``     — ``POST /api/actions/{action_id}/rollback``
  * ``authorized_approval()``   — ``POST /api/approvals/{approval_id}/respond``
  * ``authorized_ad_account()`` — ``GET  /api/audit/account/{ad_account_id}``
  * ``system_workspace()``      — ``app/jobs/`` only; ids come from a SELECT over
                                  our own tables and never from a request.

The last four exist because RLS gives row visibility and nothing else. It cannot
resolve "which tenant owns action 7f3a?" for you, so every entity-keyed route
needs a written line of ownership resolution — and ``route_audit`` fails the boot
if a new one appears without it.

``rollback`` is the one that matters most and the one easiest to miss: it takes a
bare UUID, reads ``t_advit.actions`` privileged, and hands the workspace straight
to ``ToolPipeline.invoke``, which mutates Meta. Without a resolver, any signed-in
user who learns an action id can roll back another tenant's spend.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Path, Request

from app.auth.tokens import KeyServerUnavailable, TokenRejected, VerifiedToken, verify
from app.db.pools import tenant_tx


class Capability(str, Enum):
    READ_WORKSPACE = "read_workspace"
    SUBMIT_BUSINESS_TRUTH = "submit_business_truth"
    RESPOND_TO_APPROVAL = "respond_to_approval"
    ROLLBACK_ACTION = "rollback_action"
    RUN_UNATTENDED_JOB = "run_unattended_job"


_HUMAN = frozenset(Capability) - {Capability.RUN_UNATTENDED_JOB}

# A support session must not be able to satisfy an approval. An approval is the
# OWNER's signature, and support staff do not hold it. Without this rule,
# impersonation becomes a way for an operator to authorise spending in a
# tenant's name, and no amount of stamping makes that acceptable.
_IMPERSONATED = _HUMAN - {Capability.RESPOND_TO_APPROVAL}

# A scheduled job is strictly LESS powerful than any human, never more. A job
# that could answer its own proposal would make the approval gate decorative.
_SYSTEM = frozenset({Capability.READ_WORKSPACE, Capability.RUN_UNATTENDED_JOB})

# Module-private. A third construction site can still reach it as
# `app.auth.scope._MINT` — this is not a seal, it is the difference between an
# oversight and a deliberate, greppable act.
_MINT = object()


@dataclass(frozen=True, slots=True)
class Principal:
    subject: uuid.UUID | None        # whose RLS view this is
    actor: uuid.UUID | None          # who is answerable; differs only when impersonating
    claims: dict[str, Any]
    capabilities: frozenset[Capability]
    impersonation_session_id: str | None = None
    impersonation_org_id: str | None = None

    def tx(self):
        return tenant_tx(self.claims, self.impersonation_session_id)

    def require(self, cap: Capability) -> None:
        if cap not in self.capabilities:
            raise HTTPException(403, f"{cap.value} is not permitted for this principal")


@dataclass(frozen=True, slots=True)
class AuthorizedWorkspace:
    """A workspace id that has been PROVED to belong to the caller.

    The underscore-prefixed first field is not decoration: it makes the
    positional constructor unusable by accident, and ``__post_init__`` refuses
    anything built outside this module. A privileged read of a caller-supplied
    string stops being a representable mistake.
    """

    _mint: Any
    id: str
    org_id: str
    principal: Principal

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise RuntimeError(
                "AuthorizedWorkspace may only be built by app.auth.scope. A third "
                "construction site is a third place the membership proof is skipped."
            )


# ---------------------------------------------------------------------------
# The principal
# ---------------------------------------------------------------------------


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "a bearer token is required")
    return token


def current_principal(
    request: Request,
    token: Annotated[str, Depends(_bearer)],
    impersonation: Annotated[str | None, Header(alias="X-Impersonation-Session")] = None,
) -> Principal:
    # The middleware verifies once and stashes the result; this reads the stash
    # so the token is not parsed twice. It re-verifies when the stash is absent,
    # because the dependency has to remain correct in isolation — a route tested
    # without the middleware must not become unauthenticated.
    verified: VerifiedToken | None = getattr(request.state, "verified_token", None)
    if verified is None:
        try:
            verified = verify(token)
        except KeyServerUnavailable as exc:
            request.state.auth_error = f"key server: {exc}"
            raise HTTPException(503, "authentication is temporarily unavailable") from exc
        except TokenRejected as exc:
            request.state.auth_error = str(exc)
            raise HTTPException(401, "invalid or expired token") from exc

    # Only `role` and `sub` travel onto the connection, in exactly the shape
    # packages/saas-core-db/tests/conftest.py::as_tenant uses. Forwarding the
    # whole claim set would put attacker-influenced `app_metadata` inside
    # `request.jwt.claims`, where a future policy might read it — and a policy
    # reading a tenant-writable value is the defect the compliance gate already
    # had once.
    claims = {"role": "authenticated", "sub": str(verified.subject)}
    principal = Principal(
        subject=verified.subject,
        actor=verified.subject,
        claims=claims,
        capabilities=_HUMAN,
    )
    if impersonation is None:
        return principal
    return _resolve_impersonation(principal, impersonation)


def _resolve_impersonation(operator: Principal, session_id: str) -> Principal:
    """Re-read on EVERY request, from the session row, on the tenant connection.

    ``core.platform_users.is_superadmin`` already carries the rule in its own
    comment — "read from the database on every check, never trusted from a JWT
    claim alone". Impersonation is the same rule one level up and worse if
    broken: a claim outlives the session row, so a support session ended at
    minute five would keep working until the token expired.
    """
    with operator.tx() as cur:
        cur.execute(
            """
            select s.org_id::text as org_id, s.target_user_id::text as target
              from core.impersonation_sessions s
             where s.id = %s::uuid
               and s.superadmin_id = auth.uid()
               and s.ended_at is null
               and s.expires_at > now()
               -- A session with no target cannot reach tenant data. Enforced
               -- here rather than by a CHECK, because the FK is `on delete set
               -- null` and a constraint would make deleting a user fail on an
               -- old support session.
               and s.target_user_id is not null
               -- Impersonating another superadmin would fire every
               -- `or core.is_superadmin()` branch in every policy and turn a
               -- scoped errand into unbounded god mode.
               and not core.is_superadmin(s.target_user_id)
            """,
            (session_id,),
        )
        row = cur.fetchone()

    if row is None:
        raise HTTPException(403, "no live impersonation session")

    return Principal(
        # `sub` becomes the TARGET, so RLS reproduces exactly what that tenant
        # sees — which is the point of support access and needs no new policy.
        # Keeping `sub` as the superadmin would instead fire every
        # is_superadmin() branch and show them every tenant at once.
        subject=uuid.UUID(row["target"]),
        actor=operator.subject,
        claims={"role": "authenticated", "sub": row["target"]},
        capabilities=_IMPERSONATED,
        impersonation_session_id=session_id,
        impersonation_org_id=row["org_id"],
    )


# ---------------------------------------------------------------------------
# The producers
# ---------------------------------------------------------------------------

_RESOLVE_WORKSPACE = """
select w.org_id::text                       as org_id,
       t_advit.is_workspace_member(w.id)    as is_member,
       (select u.is_active
          from core.platform_users u
         where u.id = auth.uid())           as actor_active
  from t_advit.workspaces w
 where w.id = %(workspace)s::uuid
"""


def authorized_workspace(
    workspace_id: Annotated[uuid.UUID, Path()],
    principal: Annotated[Principal, Depends(current_principal)],
) -> AuthorizedWorkspace:
    with principal.tx() as cur:
        cur.execute(_RESOLVE_WORKSPACE, {"workspace": str(workspace_id)})
        row = cur.fetchone()

    # THE PREDICATE IS is_workspace_member, NOT "the SELECT returned a row".
    #
    # This is the single most important line in the file. `workspaces_select`
    # uses the WIDER core.is_org_member, while ad_sets, actions, approvals and
    # every other product policy use t_advit.is_workspace_member. An
    # organisation `member` with no workspace_members row passes the SELECT and
    # fails membership — the ANALYST fixture in the database suite exists to
    # demonstrate exactly that.
    #
    # Treating the returned row as proof would let any member of an organisation
    # drive the tool pipeline against a sibling workspace they cannot read,
    # because everything downstream runs on the service connection with the id
    # "already proved".
    if row is None or not row["is_member"]:
        # 404, never 403. A 403 confirms the workspace exists, which rebuilds
        # the enumeration oracle 20260910000001 was written to close.
        raise HTTPException(404, "workspace not found")

    # Deactivation takes effect on the next request rather than at token expiry.
    # Folded into this query rather than costing its own round trip.
    if not row["actor_active"]:
        raise HTTPException(403, "this account is not active")

    # A support session names ONE organisation. Without this, swapping `sub` to
    # the target user would give the operator everything that user can see
    # across EVERY organisation they belong to, and
    # core.impersonation_sessions.org_id would be advisory.
    if principal.impersonation_org_id and principal.impersonation_org_id != row["org_id"]:
        raise HTTPException(404, "workspace not found")

    return AuthorizedWorkspace(
        _mint=_MINT, id=str(workspace_id), org_id=row["org_id"], principal=principal
    )


def _resolve_owned(principal: Principal, sql: str, label: str, ident: str) -> AuthorizedWorkspace:
    """Shared body of the three entity-keyed resolvers.

    Each SELECT runs on the TENANT connection, so ``actions_select`` /
    ``approvals_select`` / ``meta_connections_select`` — all of which use
    ``t_advit.is_workspace_member`` — decide whether the row exists at all. Zero
    rows is a 404 for the same reason as above.
    """
    with principal.tx() as cur:
        cur.execute(sql, {"ident": ident})
        row = cur.fetchone()

    if row is None:
        raise HTTPException(404, f"{label} not found")
    if principal.impersonation_org_id and principal.impersonation_org_id != row["org_id"]:
        raise HTTPException(404, f"{label} not found")

    return AuthorizedWorkspace(
        _mint=_MINT, id=row["workspace_id"], org_id=row["org_id"], principal=principal
    )


def authorized_action(
    action_id: Annotated[uuid.UUID, Path()],
    principal: Annotated[Principal, Depends(current_principal)],
) -> AuthorizedWorkspace:
    """``POST /api/actions/{action_id}/rollback``.

    The highest-consequence route in the product: it reads the action row and
    hands its workspace to ``ToolPipeline.invoke``, which mutates Meta. A
    cross-tenant rollback by UUID is a stranger pausing your campaigns.
    """
    principal.require(Capability.ROLLBACK_ACTION)
    return _resolve_owned(
        principal,
        """select a.workspace_id::text as workspace_id, w.org_id::text as org_id
             from t_advit.actions a
             join t_advit.workspaces w on w.id = a.workspace_id
            where a.id = %(ident)s::uuid""",
        "action",
        str(action_id),
    )


def authorized_approval(
    approval_id: Annotated[uuid.UUID, Path()],
    principal: Annotated[Principal, Depends(current_principal)],
) -> AuthorizedWorkspace:
    principal.require(Capability.RESPOND_TO_APPROVAL)
    return _resolve_owned(
        principal,
        """select a.workspace_id::text as workspace_id, w.org_id::text as org_id
             from t_advit.approvals a
             join t_advit.workspaces w on w.id = a.workspace_id
            where a.id = %(ident)s::uuid""",
        "approval",
        str(approval_id),
    )


def authorized_ad_account(
    ad_account_id: Annotated[str, Path()],
    principal: Annotated[Principal, Depends(current_principal)],
) -> AuthorizedWorkspace:
    """``GET /api/audit/account/{ad_account_id}`` takes an ad account with no
    workspace at all and calls the Meta driver directly.
    ``meta_connections_select`` is what decides ownership; this is the line that
    asks it."""
    return _resolve_owned(
        principal,
        """select c.workspace_id::text as workspace_id, w.org_id::text as org_id
             from t_advit.meta_connections c
             join t_advit.workspaces w on w.id = c.workspace_id
            where c.ad_account_id = %(ident)s""",
        "ad account",
        ad_account_id,
    )


@dataclass(frozen=True, slots=True)
class Superadmin:
    """A platform operator, proved by a row and not by a claim.

    core.platform_users.is_superadmin carries the rule in its own comment -
    "read from the database on every check, never trusted from a JWT claim
    alone" - and this is where the API honours it. A session that was
    superadmin an hour ago and is not now is refused now.
    """

    _mint: Any
    principal: Principal

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise RuntimeError("Superadmin may only be built by app.auth.scope")


def authorized_superadmin(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Superadmin:
    """The producer for routes about the PLATFORM rather than a workspace -
    coupons, plans, the Platform Watch inbox.

    Refused with 404, not 403, for the same reason as a foreign workspace: a
    403 on /api/admin/... tells a tenant the admin surface exists at that path.
    """
    with principal.tx() as cur:
        cur.execute("select core.is_superadmin() as ok")
        row = cur.fetchone()
    if not row or not row["ok"]:
        raise HTTPException(404, "not found")
    if principal.impersonation_session_id:
        # An operator inside a support session is acting AS the tenant and has
        # the tenant's reach, deliberately. Platform administration is not part
        # of that reach: leave the session to manage the platform.
        raise HTTPException(403, "platform administration is not available inside an impersonation session")
    return Superadmin(_mint=_MINT, principal=principal)


def system_workspace(workspace_id: str, org_id: str) -> AuthorizedWorkspace:
    """``app/jobs/`` only.

    The proof it offers is "this id came from a SELECT on our own table", not
    "a caller named it". There is no input channel into it, which is why the
    scheduler cannot be used to bypass the per-request check.
    """
    return AuthorizedWorkspace(
        _mint=_MINT,
        id=workspace_id,
        org_id=org_id,
        principal=Principal(subject=None, actor=None, claims={}, capabilities=_SYSTEM),
    )
