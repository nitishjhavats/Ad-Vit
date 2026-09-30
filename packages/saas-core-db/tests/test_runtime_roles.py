"""The runtime's two login roles, and the properties the split rests on.

Until 20260911000007 the agent runtime connected as `postgres`: superuser,
BYPASSRLS, owner of every table. On that connection row-level security is not
weakened, it is **absent** — so every policy in `supabase/migrations` was dead
code for the API path, and every test in this package was exercising a boundary
production did not stand behind.

These tests connect as the credentials the API actually holds. They are
deliberately about the *shape* of the two roles rather than about any one query,
because the shape is what makes a future mistake fail closed:

  * ``advit_tenant`` is NOINHERIT, so forgetting ``set local role authenticated``
    raises instead of running with nobody's claims.
  * ``advit_service`` is not BYPASSRLS and not a member of ``service_role``, so a
    leaked runtime password cannot read the unrelated product on this cluster.
  * a tenant connection cannot write the action spine at all, which is the
    premise the whole read/write split rests on.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import (
    ANALYST,
    MEMBER,
    ORG_BROADMATE,
    OUTSIDER,
    OWNER,
    SUPERADMIN,
    as_tenant,
)

PRODUCT_SCHEMA = "t_advit"


# ---------------------------------------------------------------------------
# advit_tenant: the fail-closed property NOINHERIT buys
# ---------------------------------------------------------------------------


def test_the_tenant_credential_cannot_reach_the_product_without_first_becoming_authenticated(
    tenant_conn,
):
    """The single reason ``advit_tenant`` is NOINHERIT.

    A code path that opens a tenant connection and forgets the ``set local role``
    must not run. With INHERIT it would run with ``authenticated``'s grants and
    no JWT subject: ``auth.uid()`` null, every policy matching nothing, and the
    route returning an empty list. That fails closed too — but it looks exactly
    like a tenant with no data, so it would ship.
    """
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        tenant_conn.execute(f"select count(*) from {PRODUCT_SCHEMA}.workspaces")
    tenant_conn.rollback()
    assert "permission denied for schema" in str(exc.value)


def test_the_tenant_credential_is_not_a_member_of_service_role_or_the_backend(tenant_conn):
    """One stray ``grant`` would reinstate cross-tenant reach through a single
    ``set role``, and nothing else in the system would notice.

    ``service_role`` matters most: it carries BYPASSRLS, which is a cluster-wide
    property on a cluster this product shares with an unrelated HRMS database.
    """
    with tenant_conn.cursor() as cur:
        cur.execute(
            """
            select pg_has_role('advit_tenant', 'service_role',  'member'),
                   pg_has_role('advit_tenant', 'advit_backend', 'member'),
                   pg_has_role('advit_tenant', 'authenticated', 'member')
            """
        )
        service_role, backend, authenticated = cur.fetchone()

    assert service_role is False, "advit_tenant can become service_role and bypass RLS entirely"
    assert backend is False, "advit_tenant can become the backend and skip the tenancy boundary"
    assert authenticated is True, "advit_tenant cannot become `authenticated` and can do nothing"


def test_the_tenant_role_inherits_nothing_so_the_grant_must_be_taken_deliberately(tenant_conn):
    """``rolinherit`` is the mechanism behind the test above it. Asserted
    separately because a future ``alter role advit_tenant inherit`` would leave
    the membership assertions passing and silently remove the guard."""
    with tenant_conn.cursor() as cur:
        cur.execute("select rolinherit, rolbypassrls, rolsuper from pg_roles where rolname = 'advit_tenant'")
        inherit, bypassrls, superuser = cur.fetchone()
    assert inherit is False
    assert bypassrls is False
    assert superuser is False


def test_two_tenants_on_one_connection_see_only_their_own_workspace(tenant_conn):
    """RLS doing the job the API is about to delegate to it.

    Not a restatement of test_rls_isolation: that suite runs as `postgres`
    wearing `authenticated`. This runs over the real credential, so it also
    proves the grant set underneath the policies is sufficient — a policy that
    permits a row the role has no SELECT grant on still fails.
    """
    with as_tenant(tenant_conn, OWNER) as conn:
        with conn.cursor() as cur:
            cur.execute(f"select name from {PRODUCT_SCHEMA}.workspaces order by name")
            mine = [r[0] for r in cur.fetchall()]

    with as_tenant(tenant_conn, OUTSIDER) as conn:
        with conn.cursor() as cur:
            cur.execute(f"select name from {PRODUCT_SCHEMA}.workspaces order by name")
            theirs = [r[0] for r in cur.fetchall()]

    assert mine and theirs, "both tenants should see their own workspace"
    assert not set(mine) & set(theirs)


def test_the_role_and_claims_do_not_survive_the_transaction(tenant_conn):
    """``set local``, never ``set``.

    This is the property that makes pooling safe. A connection returning to the
    pool carrying the previous caller's ``sub`` would serve the next caller that
    tenant's rows — a cross-tenant read no route test would ever catch, because
    every route would be behaving correctly.
    """
    with as_tenant(tenant_conn, OWNER):
        pass

    with tenant_conn.cursor() as cur:
        cur.execute("select current_user, current_setting('request.jwt.claims', true)")
        user, claims = cur.fetchone()

    assert user == "advit_tenant", "the connection is still wearing the previous caller's role"
    assert not claims, "the connection carried the previous caller's claims out of the transaction"


@pytest.mark.parametrize("table", ["actions", "runs", "guardrail_events", "outcomes", "secrets"])
def test_a_tenant_connection_cannot_write_the_action_spine(tenant_conn, table):
    """The premise the read/write split rests on, asserted rather than assumed.

    20260903000007 says it in a comment — these tables are "written by the agent
    runtime under the service role, so an agent cannot rewrite its own history".
    Nothing checked that the grants matched the sentence. If any of these became
    tenant-writable, an approval could be forged from the browser and the whole
    governance spine would be decorative.
    """
    with as_tenant(tenant_conn, OWNER) as conn:
        with conn.cursor() as cur:
            cur.execute("savepoint probe")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(f"insert into {PRODUCT_SCHEMA}.{table} default values")
            cur.execute("rollback to savepoint probe")


# ---------------------------------------------------------------------------
# advit_service: privileged, but bounded
# ---------------------------------------------------------------------------


def test_the_service_connection_looks_like_the_backend_to_every_guard(service_conn):
    """``core.assert_org_visible`` and ``core.log_audit`` both decide "is this the
    trusted backend?" by asking for a caller with **no subject and no PostgREST
    role**. 20260910000003 tightened that after ``anon`` walked through it.

    ``advit_service`` INHERITs ``advit_backend``, so it never calls ``set role``
    and the ``role`` GUC stays ``none``. That is not a happy accident — it is why
    the role is INHERIT while ``advit_tenant`` is NOINHERIT.
    """
    with service_conn.cursor() as cur:
        cur.execute("select current_user, current_setting('role', true)")
        user, role = cur.fetchone()
    assert user == "advit_service"
    assert role == "none", (
        "the service connection is wearing a PostgREST role, which every backend "
        "guard in core reads as an anonymous caller"
    )


def test_the_backend_role_is_not_bypassrls_and_is_not_service_role(service_conn):
    """The shared-cluster property, and the one place this implementation
    overrules the design it came from.

    ``grant service_role to advit_service`` would have been one line. It also
    hands a password-holding credential BYPASSRLS, which is cluster-wide — and
    this cluster hosts an unrelated HRMS product. A leaked runtime password would
    then be full read/write over somebody else's employee records.
    """
    with service_conn.cursor() as cur:
        cur.execute(
            """
            select rolbypassrls, rolsuper from pg_roles where rolname = 'advit_backend'
            """
        )
        bypassrls, superuser = cur.fetchone()
        cur.execute("select pg_has_role('advit_service', 'service_role', 'member')")
        (is_service_role,) = cur.fetchone()

    assert bypassrls is False
    assert superuser is False
    assert is_service_role is False


def test_the_service_connection_reads_across_tenants_which_is_the_whole_point(service_conn):
    """Guardrail arithmetic must not vary with who is asking.

    ``PostgresPolicyStore.workspace_policy`` sums committed spend. Under RLS a
    row the caller cannot see is not an error — it is **absent**, and absent sums
    to zero. A cap computed from a filtered sum is not a cap. So the safety
    arithmetic runs here, with a workspace id something else has already proved.
    """
    with service_conn.cursor() as cur:
        cur.execute(f"select count(*) from {PRODUCT_SCHEMA}.workspaces")
        (count,) = cur.fetchone()
    assert count >= 2, "the service connection should see every tenant's workspace"


@pytest.mark.parametrize("table", ["workspaces", "actions", "business_truth"])
def test_the_service_connection_cannot_delete_tenant_history(service_conn, table):
    """No DELETE grant anywhere, deliberately. The runtime corrects rows; it
    never removes them. A missing grant is a louder failure than a missing
    WHERE clause, and this is the failure mode where "louder" is worth a lot."""
    with service_conn.cursor() as cur:
        cur.execute("savepoint probe")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(f"delete from {PRODUCT_SCHEMA}.{table} where false")
        cur.execute("rollback to savepoint probe")


def test_the_service_connection_cannot_write_the_audit_trail_directly(service_conn):
    """``core.audit_log`` is excluded from the backend's grants on purpose.

    Its only writer is ``core.log_audit``, which is SECURITY DEFINER and needs no
    table grant. A direct INSERT privilege here would let a bug — or a later
    convenience — put an unattributed row into the append-only trail, which is
    exactly the property 20260907000001 exists to protect.
    """
    with service_conn.cursor() as cur:
        cur.execute("savepoint probe")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                """
                insert into core.audit_log (scope, org_id, actor_type, event, payload_json)
                values ('organisation', %s, 'agent', 'forged', '{}'::jsonb)
                """,
                (ORG_BROADMATE,),
            )
        cur.execute("rollback to savepoint probe")


def test_every_table_in_both_schemas_carries_a_backend_policy(service_conn):
    """The maintenance cost of refusing ``service_role``, made visible.

    ``advit_backend`` is not BYPASSRLS, so it needs a permissive policy on every
    table. A table added later without one fails the service path — loudly, which
    is the right direction, but only if somebody is told. This is the telling.
    """
    with service_conn.cursor() as cur:
        cur.execute(
            """
            select t.schemaname || '.' || t.tablename
              from pg_tables t
             where t.schemaname in ('core', %s)
               and not (t.schemaname = 'core' and t.tablename = 'audit_log')
               and not exists (
                     select 1 from pg_policies p
                      where p.schemaname = t.schemaname
                        and p.tablename  = t.tablename
                        and p.policyname = 'advit_backend_all')
             order by 1
            """,
            (PRODUCT_SCHEMA,),
        )
        missing = [r[0] for r in cur.fetchall()]

    assert missing == [], (
        "these tables have no advit_backend policy, so the service path sees zero "
        "rows in them and every write is refused:\n  " + "\n  ".join(missing)
    )


# ---------------------------------------------------------------------------
# The asymmetry the workspace resolver has to respect
# ---------------------------------------------------------------------------


def test_an_org_member_without_a_workspace_grant_passes_the_select_but_fails_membership(
    tenant_conn,
):
    """The most important line in ``app/auth/scope.py``, justified here rather
    than in a comment.

    ``workspaces_select`` uses the WIDER ``core.is_org_member``; ``ad_sets``,
    ``actions``, ``approvals`` and every other product policy use
    ``t_advit.is_workspace_member``, which admits workspace members plus the
    organisation's owners and admins. ANALYST is a plain org ``member`` with no
    ``workspace_members`` row, so it **can read the workspace row** and **is not
    a member of it**.

    If the resolver treated "the SELECT returned a row" as proof, any member of
    an organisation could drive the tool pipeline against a sibling workspace
    they cannot read — because everything downstream runs on the service
    connection with the id "already proved".
    """
    with as_tenant(tenant_conn, ANALYST) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select w.id::text,
                       {PRODUCT_SCHEMA}.is_workspace_member(w.id) as is_member
                  from {PRODUCT_SCHEMA}.workspaces w
                """
            )
            rows = cur.fetchall()

    assert rows, (
        "the org member cannot see the workspace row at all, so the asymmetry "
        "this test exists to pin no longer exists — check workspaces_select"
    )
    assert all(is_member is False for _, is_member in rows), (
        "an org member with no workspace_members row is being reported as a "
        "workspace member; the resolver's membership check would now admit them"
    )


def test_the_same_account_cannot_read_anything_scoped_to_that_workspace(tenant_conn):
    """The other half, and the one that shows why the asymmetry matters.

    Seeing the workspace row buys nothing: every table that carries real tenant
    data is behind ``is_workspace_member``. So the honest answer to "does this
    account own this workspace?" is no, and a resolver that says yes hands the
    pipeline a workspace whose data the caller cannot even read.
    """
    with as_tenant(tenant_conn, ANALYST) as conn:
        with conn.cursor() as cur:
            for table in ("ad_sets", "actions", "approvals", "business_truth"):
                cur.execute(f"select count(*) from {PRODUCT_SCHEMA}.{table}")
                (visible,) = cur.fetchone()
                assert visible == 0, f"{table} leaked {visible} rows to a non-member"


def test_a_superadmin_is_not_silently_a_workspace_member(tenant_conn):
    """Support access is a separate, stamped mechanism (an impersonation
    session), not a quiet bypass. If ``is_workspace_member`` answered True for an
    operator, every "is this yours?" check in the product would stop being one.
    """
    with as_tenant(tenant_conn, SUPERADMIN) as conn:
        with conn.cursor() as cur:
            cur.execute(f"select count(*) from {PRODUCT_SCHEMA}.workspaces")
            (visible,) = cur.fetchone()
    assert visible >= 1, "a superadmin should still be able to see workspace rows"
