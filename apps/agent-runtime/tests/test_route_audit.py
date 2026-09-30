"""The boot check, and the property it is the only defence for.

Every other test in this suite asserts something about a route that exists. This
one asserts something about routes that do not exist yet — that the next one
somebody writes cannot be open, because the process will not start if it is.

Each case builds a small FastAPI app in the shape of the mistake and asserts the
audit refuses it. The positive cases matter just as much: an audit that refuses
everything is not a control, it is an outage.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from pydantic import BaseModel

from app.auth.route_audit import UNSCOPED, assert_every_route_is_guarded
from app.auth.scope import (
    AuthorizedWorkspace,
    Principal,
    authorized_action,
    authorized_workspace,
    current_principal,
)
from app.main import app as real_app


# Module level, not inside the test functions.
#
# This file carries `from __future__ import annotations`, so every annotation is
# a string that FastAPI resolves against the defining module's globals. A model
# declared inside a test function is not in those globals, so the annotation
# quietly fails to resolve, the parameter is not recognised as a body field, and
# the audit finds nothing to complain about - a false PASS in a test whose whole
# job is to prove the audit can fail.


class WorkspaceIdInBody(BaseModel):
    workspace_id: str
    message: str


class OrgIdInBody(BaseModel):
    org_id: str


def test_the_real_application_passes_its_own_boot_check():
    """The one that would actually stop a deploy. Everything below is about
    whether this check can detect anything."""
    assert_every_route_is_guarded(real_app)


def test_a_route_with_no_principal_dependency_fails_the_boot():
    app = FastAPI()

    @app.get("/api/thing")
    def thing() -> dict:
        return {}

    with pytest.raises(RuntimeError, match="no principal dependency"):
        assert_every_route_is_guarded(app)


def test_an_authenticated_but_unscoped_route_fails_the_boot():
    """The gap between "has a token" and "is scoped to a tenant".

    This is the shape an audit that checks only for authentication waves
    through, and it reads every tenant's rows: the caller is who they say they
    are, and then names whichever workspace they like.
    """
    app = FastAPI()

    @app.get("/api/thing")
    def thing(
        workspace_id: str,
        principal: Annotated[Principal, Depends(current_principal)] = None,
    ) -> dict:
        return {}

    with pytest.raises(RuntimeError, match="authenticated but unscoped"):
        assert_every_route_is_guarded(app)


def test_a_workspace_id_query_parameter_fails_the_boot():
    """The original bug wearing a token.

    The route is properly scoped - it resolves its tenant from the action row -
    and then takes a SECOND, caller-supplied tenant id alongside. Both are
    present, only one is proved, and which one the body uses is a matter of
    which line the author wrote.
    """
    app = FastAPI()

    @app.get("/api/actions/{action_id}/thing")
    def thing(
        ws: Annotated[AuthorizedWorkspace, Depends(authorized_action)],
        workspace_id: str = "",
    ) -> dict:
        return {}

    with pytest.raises(RuntimeError, match="query/header parameter"):
        assert_every_route_is_guarded(app)


def test_a_workspace_id_body_field_fails_the_boot():
    """How `/api/chat` and `/api/daily-truth` used to name their tenant."""
    app = FastAPI()

    @app.post("/api/workspaces/{workspace_id}/thing")
    def thing(
        payload: WorkspaceIdInBody,
        ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
    ) -> dict:
        return {}

    with pytest.raises(RuntimeError, match="body field"):
        assert_every_route_is_guarded(app)


def test_an_org_id_is_caught_the_same_way():
    """A tenant identifier one level up. Naming the organisation is naming every
    workspace in it."""
    app = FastAPI()

    @app.post("/api/workspaces/{workspace_id}/thing")
    def thing(
        payload: OrgIdInBody,
        ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
    ) -> dict:
        return {}

    with pytest.raises(RuntimeError, match="org_id"):
        assert_every_route_is_guarded(app)


def test_a_correctly_scoped_route_passes():
    app = FastAPI()

    @app.get("/api/workspaces/{workspace_id}/thing")
    def thing(ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)]) -> dict:
        return {}

    assert_every_route_is_guarded(app)


def test_an_entity_keyed_route_passes_through_its_own_resolver():
    """RLS gives row visibility and nothing else — it cannot answer "which
    tenant owns action 7f3a?". Every entity-keyed route needs a written line of
    ownership resolution, and this is what makes a missing one a failed boot
    rather than a code review someone was too busy for."""
    app = FastAPI()

    @app.post("/api/actions/{action_id}/rollback")
    def rollback(ws: Annotated[AuthorizedWorkspace, Depends(authorized_action)]) -> dict:
        return {}

    assert_every_route_is_guarded(app)


def test_the_principal_is_found_through_a_transitive_dependency():
    """`authorized_workspace` depends on `current_principal`, so a check that
    only looked one level down would report every correctly written route as
    unauthenticated — and the fix for that false positive would be to weaken the
    check."""
    app = FastAPI()

    @app.get("/api/workspaces/{workspace_id}/thing")
    def thing(ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)]) -> dict:
        return {}

    assert_every_route_is_guarded(app)


def test_an_unscoped_route_passes_only_by_being_named():
    """The escape hatch is a frozenset in `route_audit`, which means opening a
    route is an edit a reviewer sees rather than a line somebody forgot."""
    app = FastAPI()

    @app.post("/api/economics")
    def economics(principal: Annotated[Principal, Depends(current_principal)] = None) -> dict:
        return {}

    assert "/api/economics" in UNSCOPED
    assert_every_route_is_guarded(app)


def test_every_unscoped_path_still_exists_on_the_real_application():
    """A stale exemption is worse than none: it is an allow-list entry nobody is
    reviewing, waiting for a future route to be given the same path."""
    from fastapi.routing import APIRoute

    live = {r.path for r in real_app.routes if isinstance(r, APIRoute)}
    stale = sorted(UNSCOPED - live)
    assert stale == [], f"UNSCOPED names paths that no longer exist: {stale}"


def test_no_route_takes_a_bare_workspace_id_anywhere_but_the_path():
    """The whole product surface, in one assertion, in the terms the reader
    cares about."""
    from fastapi.routing import APIRoute

    for route in (r for r in real_app.routes if isinstance(r, APIRoute)):
        names = {f.name for f in route.dependant.query_params}
        assert not names & {"workspace_id", "org_id"}, route.path
