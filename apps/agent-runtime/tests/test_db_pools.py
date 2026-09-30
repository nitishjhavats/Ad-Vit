"""The pool's own invariants, which are not visible from any route.

Three of these fail in ways that produce **correct results**. A pool that
discards every connection still answers every query; a connection carrying the
previous caller's claims still returns rows; a session advisory lock taken
through a transaction pooler still returns true. That is why they are asserted
here rather than left to an integration test to notice.
"""

from __future__ import annotations

import psycopg
import pytest

from app.db.pools import (
    PoolsNotOpen,
    bind_tenant,
    current_tenant_tx,
    open_pools,
    service_conn,
    service_dsn,
    tenant_tx,
)
from conftest import SERVICE_DSN, TENANT_DSN

OWNER = "00000000-0000-4000-8000-000000000002"
OUTSIDER = "00000000-0000-4000-8000-000000000004"


def _claims(sub: str) -> dict:
    return {"role": "authenticated", "sub": sub}


# ---------------------------------------------------------------------------
# The reset hook
# ---------------------------------------------------------------------------


def test_the_reset_hook_returns_the_connection_idle_so_the_pool_stays_a_pool():
    """psycopg_pool checks ``transaction_status`` AFTER the reset callback and
    discards anything left ``INTRANS``.

    The naive hook — the same three statements without the autocommit flip —
    opens an implicit transaction nobody commits, so every check-in destroys the
    connection and the pool silently becomes a connection factory: identical
    answers, a full TCP and auth handshake per request, and nothing in any
    response to say so.

    The naive hook is built here as a positive control. A test that only asserts
    the good case cannot tell "the hook is correct" from "this assertion cannot
    detect the bug", and that distinction is the whole value of the test.
    """
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from app.db import pools

    def naive(conn):
        with conn.cursor() as cur:
            cur.execute("reset role")
            cur.execute("select set_config('request.jwt.claims', '', false)")

    broken = ConnectionPool(
        TENANT_DSN, min_size=1, max_size=10,
        kwargs={"row_factory": dict_row}, reset=naive, open=True, timeout=5,
    )
    try:
        broken.wait(timeout=10)
        for _ in range(5):
            with broken.connection() as conn, conn.cursor() as cur:
                cur.execute("select 1")
        assert broken.get_stats().get("returns_bad", 0) > 0, (
            "the positive control did not trip, so this test cannot detect the bug "
            "it exists to detect — psycopg_pool's behaviour has changed"
        )
    finally:
        broken.close()

    before = pools._tenant_pool.get_stats().get("returns_bad", 0)
    pids = []
    for _ in range(5):
        with tenant_tx(_claims(OWNER)) as cur:
            cur.execute("select pg_backend_pid() as pid")
            pids.append(cur.fetchone()["pid"])
    after = pools._tenant_pool.get_stats().get("returns_bad", 0)

    assert after == before, f"{after - before} connections were discarded on check-in"
    assert len(set(pids)) < len(pids), (
        f"five checkouts used {len(set(pids))} distinct backends; connections are "
        "not being reused"
    )


def test_claims_do_not_survive_a_checkout():
    """The guarantee pooling rests on.

    A connection returning to the pool carrying the previous caller's ``sub``
    would serve the next caller that tenant's rows — a cross-tenant read with
    every route behaving correctly, which no route test could ever catch. The
    belt-and-braces check inside ``tenant_tx`` raises if it ever happens; this
    proves the braces work so the belt is never needed.
    """
    with tenant_tx(_claims(OWNER)) as cur:
        cur.execute("select auth.uid()::text as who")
        assert cur.fetchone()["who"] == OWNER

    with tenant_tx(_claims(OUTSIDER)) as cur:
        cur.execute("select auth.uid()::text as who")
        assert cur.fetchone()["who"] == OUTSIDER


def test_two_tenants_in_sequence_see_only_their_own_rows():
    """The same property, stated as the thing a customer would care about."""
    seen = {}
    for who in (OWNER, OUTSIDER):
        with tenant_tx(_claims(who)) as cur:
            cur.execute("select name from t_advit.workspaces order by name")
            seen[who] = [r["name"] for r in cur.fetchall()]

    assert seen[OWNER] and seen[OUTSIDER]
    assert not set(seen[OWNER]) & set(seen[OUTSIDER])


def test_the_role_unwinds_with_the_transaction():
    """``set local``, never ``set``. Checked from outside the helper, because
    the helper is what would be wrong."""
    from app.db import pools

    with pools._tenant_pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("select current_user as u")
            assert cur.fetchone()["u"] == "advit_tenant"


# ---------------------------------------------------------------------------
# The service connection
# ---------------------------------------------------------------------------


def test_the_service_connection_looks_like_the_backend_to_the_guards():
    """``core.assert_org_visible`` and ``core.log_audit`` both decide "is this
    the trusted backend?" by requiring **no subject and no PostgREST role**.
    ``advit_service`` INHERITs its privileges, so it never calls ``set role``
    and the GUC stays ``none``. If that changed, every backend audit write would
    start being attributed to an anonymous caller."""
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute("select current_user as u, current_setting('role', true) as r")
        row = cur.fetchone()
    assert row["u"] == "advit_service"
    assert row["r"] == "none"


def test_the_service_connection_sees_every_tenant_which_is_why_the_caps_live_there():
    """Guardrail arithmetic must not vary with who is asking. Under RLS a row
    the caller cannot see is ABSENT, and absent sums to zero — so a cap computed
    on a tenant connection would get quietly larger as the caller got less
    privileged."""
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute("select count(*) as n from t_advit.workspaces")
        assert cur.fetchone()["n"] >= 2


def test_the_same_aggregate_returns_a_smaller_number_on_the_tenant_connection():
    """The failure this split exists to prevent, demonstrated rather than
    described.

    One aggregate, one table, two connections. ``t_advit.meta_connections`` is
    the ad accounts a workspace may spend through, and every seeded row belongs
    to the Broadmate workspace — so to the rival tenant they are not merely
    filtered, they are **absent**, and absent sums to zero.

    That is the whole argument for running guardrail arithmetic on the service
    connection: a cap computed from a filtered sum gets quietly larger as the
    caller gets less privileged, and nothing raises.
    """
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute("select count(*) as n from t_advit.meta_connections")
        privileged = cur.fetchone()["n"]

    with tenant_tx(_claims(OUTSIDER)) as cur:
        cur.execute("select count(*) as n from t_advit.meta_connections")
        as_outsider = cur.fetchone()["n"]

    assert privileged > 0, (
        "the fixture set has no meta_connections rows, so this test proves nothing; "
        "it needs a table where one tenant has rows and another has none"
    )
    assert as_outsider == 0, (
        f"a tenant with no claim on these ad accounts sees {as_outsider} of them"
    )


# ---------------------------------------------------------------------------
# The mutation lock
# ---------------------------------------------------------------------------


def test_the_mutation_lock_uses_its_own_unpooled_connection():
    """``pg_try_advisory_lock`` takes a SESSION lock, held across ``audit.pre``
    (which commits), the driver call and ``audit.post`` — spanning transactions
    on purpose, because the lease has to outlive the write it protects.

    ``SET LOCAL`` survives a transaction pooler; a session advisory lock does
    not. ``[db.pooler] enabled = false`` today, so this is a landmine that
    introducing Supavisor would create rather than a present bug — and this test
    is what stops someone stepping on it later.
    """
    import inspect

    from app.policy.store import PostgresLockManager

    source = inspect.getsource(PostgresLockManager)
    assert "psycopg.connect(" in source, (
        "PostgresLockManager is using a pooled connection. Its advisory lock is a "
        "session lock held across three transactions; a pooler may unlock it on a "
        "different backend, and nothing would fail visibly."
    )
    assert "service_conn" not in source


def test_the_lock_actually_excludes_a_second_holder():
    """The lock working at all, over the real credential, so a missing grant
    shows up here rather than the first time two mutations race."""
    from app.policy.store import PostgresLockManager

    locks = PostgresLockManager()
    with locks.acquire("test-account-1", timeout_s=1) as first:
        assert first is True
        with locks.acquire("test-account-1", timeout_s=1) as second:
            assert second is False, "two callers hold the same per-account mutation lock"


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


def test_an_unbound_tenant_transaction_raises_rather_than_falling_back():
    """The single most important line in ``pools.py``.

    A graph node that runs outside a bound request must fail, not quietly reach
    for the privileged connection. A fallback here would mean the orchestrator
    read every tenant's rows whenever the binding was forgotten, and would look
    exactly like it working.
    """
    with pytest.raises(PoolsNotOpen):
        current_tenant_tx()


def test_a_bound_tenant_transaction_is_the_one_that_gets_used():
    with bind_tenant(lambda: tenant_tx(_claims(OUTSIDER))):
        with current_tenant_tx() as cur:
            cur.execute("select auth.uid()::text as who")
            assert cur.fetchone()["who"] == OUTSIDER

    with pytest.raises(PoolsNotOpen):
        current_tenant_tx()


def test_opening_the_pools_on_the_superuser_dsn_is_refused():
    """The mistake this guard exists for is one environment variable copied into
    the wrong name — after which the service runs as ``postgres`` and every
    policy in the repository becomes decorative, with nothing failing."""
    from app.config import get_settings

    try:
        with pytest.raises(PoolsNotOpen, match="superuser"):
            open_pools(tenant_dsn=TENANT_DSN, service_dsn=get_settings().database_url)
    finally:
        open_pools(tenant_dsn=TENANT_DSN, service_dsn=SERVICE_DSN)


def test_the_tenant_credential_cannot_read_anything_without_becoming_authenticated():
    """``advit_tenant`` is NOINHERIT, so a path that forgets the role switch
    raises instead of running with nobody's claims and returning an empty list
    that looks like a tenant with no data."""
    with psycopg.connect(TENANT_DSN) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select count(*) from t_advit.workspaces")


def test_the_service_dsn_helper_does_not_hand_out_the_superuser_connection():
    from app.config import get_settings

    assert service_dsn() != get_settings().database_url
    assert "advit_service" in service_dsn()
