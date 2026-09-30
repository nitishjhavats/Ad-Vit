"""The identity helpers answer about you, and refuse to answer about anyone else.

``core.my_org_ids``, ``is_org_member``, ``org_role``, ``has_org_role`` and
``is_superadmin`` all take ``p_user uuid default auth.uid()``. The default is
what every RLS policy passes and it is safe. The *argument* was not: any
signed-in user could name somebody else.

Reproduced against the running database before the fix, as an outsider with no
relationship to the organisation at all::

    select core.org_role('<Broadmate>', '<owner>');   -- 'owner'

That is a membership **and role** oracle: which organisations a stranger belongs
to and, in each, whether they are the owner. It is the question an attacker asks
before deciding whom to phish, and ``core.is_superadmin(p_user)`` answers the
same question about the operators.

The obvious fix — revoke the grant — was tried first and broke every tenant read
with ``permission denied for function has_org_role``, because an RLS policy
expression is evaluated with the privileges of the querying user and there is
only one function per name: ``core.is_org_member(p_org)`` in a policy body is
this same two-argument function with its default applied. So the guard lives
inside the function, and these tests pin both halves — the refusal, and the
callers that must keep working.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import (
    MEMBER,
    ORG_BROADMATE,
    ORG_RIVAL,
    OUTSIDER,
    OWNER,
    SUPERADMIN,
    as_tenant,
    rows_as,
    scalar_as,
)

PRODUCT_SCHEMA = "t_advit"

# Every shape that names somebody other than the caller.
PROBES = [
    ("core.is_org_member", "select core.is_org_member(%s, %s)", (ORG_BROADMATE, OWNER)),
    ("core.org_role", "select core.org_role(%s, %s)", (ORG_BROADMATE, OWNER)),
    ("core.my_org_ids", "select count(*) from core.my_org_ids(%s)", (OWNER,)),
    ("core.is_superadmin", "select core.is_superadmin(%s)", (SUPERADMIN,)),
    (
        "core.has_org_role",
        "select core.has_org_role(%s, array['owner','admin']::core.org_role[], %s)",
        (ORG_BROADMATE, OWNER),
    ),
]


@pytest.mark.parametrize("name,sql,params", PROBES, ids=[p[0] for p in PROBES])
def test_a_tenant_cannot_ask_an_identity_question_about_another_user(conn, name, sql, params):
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        rows_as(conn, OUTSIDER, sql, params)
    conn.rollback()
    assert exc.value.diag.message_hint == "identity_probe_refused", name


@pytest.mark.parametrize("name,sql,params", PROBES, ids=[p[0] for p in PROBES])
def test_not_even_a_fellow_member_of_the_same_organisation_may_ask(conn, name, sql, params):
    """Not a tenancy question, an identity one.

    Sharing an organisation with somebody does not entitle you to enumerate the
    rest of their organisations, and ``core.my_org_ids(colleague)`` would do
    exactly that. The refusal is about the subject of the question, not about
    whether the asker happens to be nearby.
    """
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        rows_as(conn, MEMBER, sql, params)
    conn.rollback()
    assert exc.value.diag.message_hint == "identity_probe_refused", name


def test_the_refusal_does_not_answer_the_question_it_refuses(conn):
    """A message reading "user X is not a member of org Y" would refuse and
    answer in the same breath. Two probes that differ only in their truth must
    produce the same text."""
    messages = set()
    for org in (ORG_BROADMATE, ORG_RIVAL):
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
            rows_as(conn, OUTSIDER, "select core.org_role(%s, %s)", (org, OWNER))
        conn.rollback()
        messages.add(str(exc.value).strip())
    assert len(messages) == 1, f"the refusal varies with the answer: {messages}"


# ---------------------------------------------------------------------------
# ...and the callers that must keep working
# ---------------------------------------------------------------------------


def test_asking_about_yourself_is_still_answered(conn):
    """The defaulted argument. This is the branch every RLS policy takes, so if
    it broke, every tenant read in the product would fail — which is what the
    first attempt at this fix actually did."""
    assert scalar_as(conn, OWNER, "select core.is_org_member(%s)", (ORG_BROADMATE,)) is True
    assert scalar_as(conn, OWNER, "select core.is_org_member(%s)", (ORG_RIVAL,)) is False
    assert str(scalar_as(conn, OWNER, "select core.org_role(%s)", (ORG_BROADMATE,))) == "owner"
    assert scalar_as(conn, OWNER, "select count(*) from core.my_org_ids()") == 1


def test_naming_yourself_explicitly_is_the_same_as_defaulting(conn):
    """``core.log_audit`` and ``core.assert_org_visible`` both pass
    ``auth.uid()`` through as an explicit argument. If the guard only recognised
    the default, the audit trail would start raising."""
    assert scalar_as(conn, OWNER, "select core.is_org_member(%s, %s)", (ORG_BROADMATE, OWNER)) is True


def test_a_superadmin_may_ask_about_anyone(conn):
    """The support console legitimately answers "who owns this account".

    Read from ``core.platform_users`` on every call rather than trusted from a
    claim — the rule that column's own comment already states.
    """
    assert str(scalar_as(conn, SUPERADMIN, "select core.org_role(%s, %s)", (ORG_BROADMATE, OWNER))) == "owner"
    assert scalar_as(conn, SUPERADMIN, "select count(*) from core.my_org_ids(%s)", (OWNER,)) == 1


def test_the_backend_may_ask_about_anyone(service_conn):
    """The service path resolves "is this user a member of that org" while acting
    for nobody in particular, and is not reachable from an HTTP caller."""
    with service_conn.cursor() as cur:
        cur.execute("select core.org_role(%s, %s)::text", (ORG_BROADMATE, OWNER))
        assert cur.fetchone()[0] == "owner"


def test_an_anonymous_caller_is_refused_rather_than_treated_as_the_backend(conn):
    """The specific hole 20260910000003 closed one level down.

    A caller with no JWT subject is only the backend if it also has no PostgREST
    role. ``acting_as(conn, None)`` is ``role=authenticated`` with no subject —
    anonymous in every sense that matters — and the guard must not read the
    absent subject as "this is the trusted service".
    """
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        rows_as(conn, None, "select core.org_role(%s, %s)", (ORG_BROADMATE, OWNER))
    conn.rollback()
    assert exc.value.diag.message_hint == "identity_probe_refused"


def test_every_rls_policy_still_resolves_over_a_real_tenant_connection(tenant_conn):
    """The regression that the first attempt caused, pinned.

    Seventeen policies in both schemas name these functions. Revoking EXECUTE
    made every one of them raise ``permission denied for function
    has_org_role``. A read over the real credential is the cheapest thing that
    would have caught it.
    """
    with as_tenant(tenant_conn, OWNER) as conn:
        with conn.cursor() as cur:
            for table in (
                "core.organisations",
                "core.organisation_members",
                "core.subscriptions",
                f"{PRODUCT_SCHEMA}.workspaces",
                f"{PRODUCT_SCHEMA}.campaigns",
                f"{PRODUCT_SCHEMA}.approvals",
            ):
                cur.execute(f"select count(*) from {table}")
                cur.fetchone()


def test_the_workspace_membership_oracle_is_closed_transitively(conn):
    """``t_advit.is_workspace_member(p_workspace, p_user)`` is the same question
    one level up, and it takes an arbitrary user too.

    It is not guarded directly. It does not need to be: its body asks
    ``core.has_org_role(..., p_user)``, which now refuses. Asserted rather than
    reasoned, because "closed by a call it happens to make" is exactly the kind
    of property that a later refactor removes without noticing.
    """
    workspace = scalar_as(
        conn, OWNER, f"select id::text from {PRODUCT_SCHEMA}.workspaces limit 1"
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        rows_as(
            conn,
            OUTSIDER,
            f"select {PRODUCT_SCHEMA}.is_workspace_member(%s::uuid, %s::uuid)",
            (workspace, OWNER),
        )
    conn.rollback()
    assert exc.value.diag.message_hint == "identity_probe_refused"
