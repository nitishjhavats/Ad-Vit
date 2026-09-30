"""The process refuses to start if a route is open or unscoped.

A code review can miss a route. A failed boot cannot.

Three assertions rather than one, because "has a principal" and "is scoped to a
tenant" are different properties and only the second one keeps tenants apart. A
route written as::

    @app.get("/api/thing")
    def thing(workspace_id: str, p: Principal = Depends(current_principal)):

is authenticated, passes a naive audit, and reads every tenant's rows.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.auth.scope import (
    authorized_action,
    authorized_ad_account,
    authorized_approval,
    authorized_superadmin,
    authorized_workspace,
    current_principal,
)

# Unauthenticated by design, and each one is a decision.
PUBLIC_PATHS = frozenset(
    {
        "/health",        # a container health check cannot hold a session
        "/api/brand",     # the public lock-up
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)

# Authenticated but deliberately not workspace-scoped: pure functions of their
# input, touching no tenant row. They need a principal (metering, abuse) and
# have no tenant to be scoped to.
UNSCOPED = frozenset(
    {
        "/api/compliance/check",
        "/api/economics",
        "/api/cta/recommend",
        # Process health and the write allowlist: facts about this deployment,
        # not about any tenant. It needs a principal because the allowlist names
        # the ad accounts this process may spend money on, and it has no
        # workspace to be scoped to.
        "/api/health/detail",
    }
)

_PRODUCERS: set[Callable[..., Any]] = {
    authorized_workspace,
    authorized_action,
    authorized_approval,
    authorized_ad_account,
    # Scoped to the PLATFORM rather than to a tenant. A route about coupons or
    # plans has no workspace to be scoped to, and "is this person an operator"
    # is the whole question - answered from core.platform_users on every
    # request, never from a claim.
    authorized_superadmin,
}

# A tenant identifier may be a PATH parameter and nothing else.
TENANT_KEYS = ("workspace_id", "org_id")


def _flatten(dependant: Dependant) -> set[Callable[..., Any]]:
    """Every callable in the dependency tree, at any depth.

    Depth matters: a route depending on `authorized_workspace` gets
    `current_principal` transitively, and a check that only looked one level
    down would report it as unauthenticated.
    """
    found: set[Callable[..., Any]] = set()
    stack = [dependant]
    while stack:
        node = stack.pop()
        if node.call is not None:
            found.add(node.call)
        stack.extend(node.dependencies)
    return found


def assert_every_route_is_guarded(app: FastAPI) -> None:
    problems: list[str] = []

    for route in (r for r in app.routes if isinstance(r, APIRoute)):
        if route.path in PUBLIC_PATHS:
            continue

        calls = _flatten(route.dependant)
        methods = "/".join(sorted(route.methods - {"HEAD", "OPTIONS"}))

        # 1. Authenticated.
        if current_principal not in calls:
            problems.append(f"{methods} {route.path}: no principal dependency")

        # 2. Scoped to a tenant — unless explicitly listed as pure.
        if route.path not in UNSCOPED and not (_PRODUCERS & calls):
            problems.append(
                f"{methods} {route.path}: authenticated but unscoped. Depend on an "
                f"AuthorizedWorkspace producer, or add the path to UNSCOPED with a "
                f"written reason."
            )

        # 3. The tenant id is a PATH parameter or it is nowhere.
        #
        # A query parameter or body field named workspace_id is the original bug
        # wearing a token: the caller still chooses which tenant to act on, and
        # the only thing standing between them and somebody else's account is a
        # resolver that nothing forces the route to call.
        for field in (
            route.dependant.query_params
            + route.dependant.header_params
            + route.dependant.cookie_params
        ):
            if field.name in TENANT_KEYS:
                problems.append(f"{methods} {route.path}: `{field.name}` is a query/header parameter")

        for field in route.dependant.body_params:
            model = getattr(field.field_info, "annotation", None)
            # Best effort, and only one level deep: a nested model carrying a
            # tenant id would slip past this. Said out loud rather than implied,
            # because an audit whose limits are undocumented gets trusted past
            # them.
            for name in getattr(model, "model_fields", {}):
                if name in TENANT_KEYS:
                    problems.append(
                        f"{methods} {route.path}: `{name}` is a body field on "
                        f"{getattr(model, '__name__', model)}"
                    )

    if problems:
        raise RuntimeError("routes are open or unscoped:\n  " + "\n  ".join(problems))
