"""Two pools, two login roles, two passwords.

    advit_tenant  -> becomes `authenticated` with the caller's JWT claims. The
                     RLS policies in supabase/migrations decide what is visible.
                     Byte-identical in shape to
                     packages/saas-core-db/tests/conftest.py::as_tenant, so the
                     database suite's isolation tests are also this API's
                     authorization tests.

    advit_service -> the governance spine: t_advit.actions, guardrail_events,
                     outcomes, t_advit.secrets, compute_blended_daily, and
                     core.log_audit with actor_type 'agent'. Every one of those
                     is SELECT-only or ungranted for `authenticated`.

The one hard rule this module exists to encode:

    A read that decides what the CALLER MAY SEE runs on the tenant connection.
    A read or write that decides what the SYSTEM MAY DO runs on the service
    connection, with a workspace id something else has already proved.

The second half is not a hedge, and it is the reason the split exists at all.
``PostgresPolicyStore.workspace_policy`` computes committed daily spend as a
``sum()`` over ``t_advit.ad_sets`` and ``t_advit.actions``. Under RLS a row the
caller cannot see is not an error - it is **absent**, and absent sums to zero.
``t_advit.workspaces.spend_basis_known`` exists because a coalesce cannot tell
"spent nothing" from "never ingested"; RLS adds a third indistinguishable case,
"not yours to see". A cap computed from a filtered sum is not a cap, so a safety
computation must never vary with who is asking.

The asymmetry is worth stating plainly because it is this design's real
long-term liability: picking the wrong pool for a WRITE fails loudly, because
the grant matrix raises `permission denied`. Picking the wrong pool for a READ
fails SILENTLY - the aggregate just comes back smaller and the guardrail passes.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, ContextManager, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

_tenant_pool: ConnectionPool | None = None
_service_pool: ConnectionPool | None = None


class PoolsNotOpen(RuntimeError):
    """Raised rather than falling back to a connection nobody chose."""


def _scrub(conn: psycopg.Connection) -> None:
    """Pool reset hook, run on every check-in.

    psycopg_pool inspects ``transaction_status`` AFTER this callback and
    DISCARDS any connection left ``INTRANS``. With autocommit off - which
    ``tenant_tx`` asserts - these statements would open an implicit transaction
    nobody commits, every check-in would destroy the connection, and the pool
    would quietly become a connection factory: correct results, a new TCP
    handshake per request, and nothing in the logs. Hence the flip.

    Deliberately NOT ``discard all``. psycopg3 prepares statements above
    ``prepare_threshold`` and caches their names per connection, so deallocating
    them behind its back makes the next checkout fail with
    ``prepared statement "_pg3_0" does not exist``. Only the three things this
    module ever sets are cleared.
    """
    previous = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("reset role")
            cur.execute("select set_config('request.jwt.claims', '', false)")
            cur.execute("select set_config('request.impersonation_session_id', '', false)")
    finally:
        conn.autocommit = previous


def open_pools(tenant_dsn: str | None = None, service_dsn: str | None = None) -> None:
    """Open both pools. Called from the lifespan handler so a wrong credential
    fails at BOOT rather than on the first request that happens to need it.

    The arguments exist for tests, which point both pools at the local Docker
    Postgres. Production passes neither and reads configuration.
    """
    global _tenant_pool, _service_pool
    settings = get_settings()
    tenant_dsn = tenant_dsn or settings.tenant_database_url
    service_dsn = service_dsn or settings.service_database_url

    if not tenant_dsn or not service_dsn:
        raise PoolsNotOpen(
            "TENANT_DATABASE_URL and SERVICE_DATABASE_URL are both required. There "
            "is deliberately no fallback to DATABASE_URL: connecting as the "
            "superuser is the vulnerability, not the workaround."
        )

    # The check that actually matters. A local default pointing at a
    # correctly-privileged 127.0.0.1 credential is fine; silently running the
    # API as `postgres` is the thing this whole migration exists to stop, and it
    # is exactly what a copy-paste into the wrong environment variable produces.
    superuser_dsn = settings.database_url
    if superuser_dsn and service_dsn == superuser_dsn:
        raise PoolsNotOpen(
            "SERVICE_DATABASE_URL is the same DSN as DATABASE_URL. DATABASE_URL is "
            "the superuser connection kept for `supabase db reset` and the test "
            "suites; on it, RLS is not weakened, it is absent."
        )

    close_pools()
    _tenant_pool = ConnectionPool(
        tenant_dsn, min_size=1, max_size=10,
        kwargs={"row_factory": dict_row}, reset=_scrub, open=True, timeout=5,
    )
    _service_pool = ConnectionPool(
        service_dsn, min_size=1, max_size=5,
        kwargs={"row_factory": dict_row}, reset=_scrub, open=True, timeout=5,
    )
    _tenant_pool.wait(timeout=10)
    _service_pool.wait(timeout=10)


def close_pools() -> None:
    global _tenant_pool, _service_pool
    for pool in (_tenant_pool, _service_pool):
        if pool is not None:
            pool.close()
    _tenant_pool = _service_pool = None


def pools_are_open() -> bool:
    return _tenant_pool is not None and _service_pool is not None


def service_dsn() -> str:
    """The DSN itself, for the one component that must not be pooled.

    ``PostgresLockManager`` takes a *session* advisory lock and holds it across
    ``audit.pre`` (which commits), the driver call and ``audit.post`` - spanning
    transactions deliberately. A session lock does not survive a transaction
    pooler, so that component keeps its own direct connection.
    """
    return get_settings().service_database_url


# ---------------------------------------------------------------------------
# The two transactions
# ---------------------------------------------------------------------------


@contextmanager
def tenant_tx(
    claims: dict[str, Any], impersonation_id: str | None = None
) -> Iterator[psycopg.Cursor]:
    """One SHORT transaction acting as the caller.

    ``set local``, never ``set``: the role and the claims unwind with the
    transaction, so a connection returning to the pool cannot carry one caller's
    identity into the next caller's request.

    The autocommit assertion is not decoration. Under autocommit each statement
    is its own transaction, ``SET LOCAL`` evaporates with a warning nobody
    reads, and the query then runs as the bare NOINHERIT login role - which
    raises ``permission denied for schema t_advit``. Fail-closed, but
    confusingly, and ``PostgresLockManager`` already sets ``autocommit = True``
    on its own connection, so this is a mistake that exists in this codebase
    today and would otherwise spread.

    Deliberately short. ``/api/chat`` runs model calls for seconds; holding an
    open transaction across one would exhaust a pool of ten under trivial
    concurrency and pin an idle-in-transaction snapshot. The PRINCIPAL is
    request-scoped; the TRANSACTION is query-scoped.
    """
    if _tenant_pool is None:
        raise PoolsNotOpen("open_pools() has not run")

    with _tenant_pool.connection() as conn:
        assert not conn.autocommit, "tenant work must run in an explicit transaction"
        with conn.transaction():
            with conn.cursor() as cur:
                # Belt. Proves the previous checkout left nothing behind, which
                # turns "somebody wrote `set` where `set local` was meant" from
                # unlikely into impossible-and-testable. The cost is one GUC
                # read per transaction; the failure it catches is one tenant
                # served another tenant's rows by a route behaving correctly.
                cur.execute("select current_setting('request.jwt.claims', true) as c")
                if cur.fetchone()["c"]:
                    raise RuntimeError(
                        "a pooled connection carried JWT claims across a checkout"
                    )

                cur.execute(
                    "select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),)
                )
                if impersonation_id is not None:
                    cur.execute(
                        "select set_config('request.impersonation_session_id', %s, true)",
                        (impersonation_id,),
                    )
                cur.execute("set local role authenticated")
                yield cur


@contextmanager
def service_conn(impersonation_id: str | None = None) -> Iterator[psycopg.Connection]:
    """The privileged connection, as a connection rather than a cursor.

    The adapters in ``app/policy/store.py`` commit mid-block on purpose - the
    pre-call audit row is committed BEFORE the driver is invoked so a process
    that dies mid-write leaves the retry path a record to consult. That needs
    the connection, not just a cursor.

    No ``set role`` here, deliberately. ``advit_service`` INHERITs
    ``advit_backend``, so ``current_setting('role')`` stays ``'none'`` - which
    is exactly the shape ``core.assert_org_visible`` recognises as the backend
    (a role of 'anon' or 'authenticated' with no subject is refused), and
    ``auth.uid()`` is null so ``core.log_audit`` takes its trusted branch and
    will honour actor_type 'agent'.

    Import this ONLY from app/policy/store.py, app/policy/rules.py,
    app/models/router.py and app/jobs/. The privileged connection is safe only
    while its call sites stay a small enumerable set rather than a habit, and
    tests/test_service_connection_surface.py walks the import graph to say so.
    """
    if _service_pool is None:
        raise PoolsNotOpen("open_pools() has not run")

    with _service_pool.connection() as conn:
        if impersonation_id is not None:
            with conn.cursor() as cur:
                # So an action taken inside a support session is stamped on the
                # privileged path too. It is the path that moves money, and it
                # would otherwise be the one unstamped record.
                cur.execute(
                    "select set_config('request.impersonation_session_id', %s, true)",
                    (impersonation_id,),
                )
        yield conn


@contextmanager
def service_tx(impersonation_id: str | None = None) -> Iterator[psycopg.Cursor]:
    """``service_conn`` for callers that only want a cursor."""
    with service_conn(impersonation_id) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                yield cur


# ---------------------------------------------------------------------------
# Request-scoped tenant transaction, for code we do not want to re-thread.
#
# app/orchestrator/graph.py's Orchestrator is constructed once and its LangGraph
# compiled once, but the principal changes per request. A ContextVar is the
# standard way to carry request scope through such code, and Starlette copies
# the context into the threadpool that runs a sync `def` route, so it survives.
#
# It is unset by default and reading it RAISES - so a graph node that runs
# outside a bound request fails closed rather than falling back to a privileged
# connection, which is the one failure mode that would be invisible.
# ---------------------------------------------------------------------------

_CURRENT_TENANT_TX: ContextVar[Callable[[], ContextManager[psycopg.Cursor]] | None] = ContextVar(
    "current_tenant_tx", default=None
)


@contextmanager
def bind_tenant(tx_factory: Callable[[], ContextManager[psycopg.Cursor]]) -> Iterator[None]:
    token = _CURRENT_TENANT_TX.set(tx_factory)
    try:
        yield
    finally:
        _CURRENT_TENANT_TX.reset(token)


def current_tenant_tx() -> ContextManager[psycopg.Cursor]:
    factory = _CURRENT_TENANT_TX.get()
    if factory is None:
        raise PoolsNotOpen(
            "no tenant transaction is bound for this request. A read that decides "
            "what the caller may see must run under the caller's claims; it must "
            "not silently fall back to the service connection."
        )
    return factory()
