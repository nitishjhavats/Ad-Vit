"""Test harness for the Common SaaS Core database layer.

Row-level security is the tenancy boundary (PRD 17.4): a forgotten filter in a
new code path must return nothing, never the wrong thing. These tests exercise
the policies the way PostgREST does - as the ``authenticated`` role with a JWT
``sub`` claim - rather than as the table owner, because a test that runs as a
superuser proves nothing about RLS.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager

import psycopg
import pytest

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
)

# Deterministic identifiers from supabase/seed.sql.
SUPERADMIN = "00000000-0000-4000-8000-000000000001"
OWNER = "00000000-0000-4000-8000-000000000002"          # Broadmate Global, owner
MEMBER = "00000000-0000-4000-8000-000000000003"         # Broadmate Global, member
OUTSIDER = "00000000-0000-4000-8000-000000000004"       # Rival Wellness, owner
ANALYST = "00000000-0000-4000-8000-000000000005"        # Broadmate org member, NO workspace grant

ORG_BROADMATE = "00000000-0000-4000-8000-000000000010"
ORG_RIVAL = "00000000-0000-4000-8000-000000000011"

PRODUCT_MARKETING = "00000000-0000-4000-8000-000000000020"
PLAN_STANDARD = "00000000-0000-4000-8000-000000000030"
SUB_BROADMATE = "00000000-0000-4000-8000-000000000040"
SUB_RIVAL = "00000000-0000-4000-8000-000000000041"


@pytest.fixture(scope="session")
def dsn() -> str:
    return DATABASE_URL


@pytest.fixture
def conn(dsn: str):
    """A connection with autocommit off, rolled back after every test.

    Nothing a test writes survives it, so the seeded fixture set stays stable
    and tests may run in any order.
    """
    with psycopg.connect(dsn) as connection:
        connection.autocommit = False
        try:
            yield connection
        finally:
            connection.rollback()


@contextmanager
def acting_as(connection: psycopg.Connection, user_id: str | None):
    """Run statements as ``authenticated`` with ``auth.uid()`` bound to user_id.

    Uses a SAVEPOINT so a policy denial that raises does not poison the outer
    transaction, and ``set local`` so the role and claims unwind with it.
    Passing ``None`` simulates an anonymous caller.
    """
    claims = {"role": "authenticated"}
    if user_id is not None:
        claims["sub"] = user_id

    # `Connection.transaction()` is a SAVEPOINT only when a transaction is
    # already open. As the FIRST statement on a fresh connection it opens a real
    # one and COMMITS it on exit - so every write inside acting_as survived the
    # fixture's rollback and landed in the shared development database.
    # Verified: a test that set daily_cap_inr to 7500 left it at 7500, which
    # then failed a cap test in another suite entirely, and three throwaway
    # workspaces accumulated from a test asserting that INSERT is self-service.
    #
    # One statement is enough to put a transaction underneath.
    connection.execute("select 1")

    try:
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("select set_config('request.jwt.claims', %s, true)",
                            (json.dumps(claims),))
                cur.execute("set local role authenticated")
            yield connection
    finally:
        # SET LOCAL is undone by a savepoint ROLLBACK but not by a savepoint
        # RELEASE. Committing used to reset it as a side effect; now that the
        # block is genuinely nested, a test whose acting_as succeeded would
        # otherwise keep running as the tenant afterwards - and the next plain
        # `conn.cursor()` write in the same test would be refused by a policy it
        # was never meant to be subject to.
        with connection.cursor() as cur:
            cur.execute("reset role")
            cur.execute("select set_config('request.jwt.claims', null, true)")


def rows_as(connection: psycopg.Connection, user_id: str | None, sql: str, params=None):
    """Execute a read as the given user and return all rows."""
    with acting_as(connection, user_id):
        with connection.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def scalar_as(connection: psycopg.Connection, user_id: str | None, sql: str, params=None):
    result = rows_as(connection, user_id, sql, params)
    return result[0][0] if result else None


def id_set(connection: psycopg.Connection, user_id: str | None, sql: str, params=None) -> set[str]:
    """Read a single id column as a set of strings.

    psycopg returns uuid columns as ``uuid.UUID``; the fixture constants above
    are strings. Normalising here keeps the assertions about ISOLATION rather
    than about type coercion.
    """
    return {str(r[0]) for r in rows_as(connection, user_id, sql, params)}


# ---------------------------------------------------------------------------
# The runtime's own login roles (20260911000007).
#
# `DATABASE_URL` above is the superuser DSN, which is what the schema tests
# want: they set up fixtures, disable triggers and read pg_catalog. These two
# are the credentials the API actually holds in production, and the point of
# testing through them is that a test running as `postgres` proves nothing
# about a boundary `postgres` does not stand behind.
#
# The passwords come from supabase/seeds/05_runtime_roles_local.sql and are
# local-only by construction - the migration creates all three roles NOLOGIN.
# ---------------------------------------------------------------------------

TENANT_DSN = os.environ.get(
    "TENANT_DATABASE_URL",
    "postgresql://advit_tenant:advit_tenant_local@127.0.0.1:54322/postgres",
)
SERVICE_DSN = os.environ.get(
    "SERVICE_DATABASE_URL",
    "postgresql://advit_service:advit_service_local@127.0.0.1:54322/postgres",
)


@pytest.fixture
def tenant_conn():
    """A connection as `advit_tenant`, rolled back after every test.

    Autocommit stays off: under autocommit each statement is its own
    transaction, so the `set local role` in `as_tenant` would evaporate with a
    warning nobody reads and the query would run as the bare NOINHERIT login
    role instead.
    """
    with psycopg.connect(TENANT_DSN) as connection:
        connection.autocommit = False
        try:
            yield connection
        finally:
            connection.rollback()


@pytest.fixture
def service_conn():
    with psycopg.connect(SERVICE_DSN) as connection:
        connection.autocommit = False
        try:
            yield connection
        finally:
            connection.rollback()


@contextmanager
def as_tenant(connection: psycopg.Connection, user_id: str | None):
    """`acting_as`, but over a real `advit_tenant` connection.

    Same two statements in the same order as app/db/pools.py::tenant_tx, so
    what these tests exercise is the shape the API uses rather than a
    convenient approximation of it.
    """
    claims = {"role": "authenticated"}
    if user_id is not None:
        claims["sub"] = user_id

    connection.execute("select 1")
    try:
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute(
                    "select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),)
                )
                cur.execute("set local role authenticated")
            yield connection
    finally:
        with connection.cursor() as cur:
            cur.execute("reset role")
            cur.execute("select set_config('request.jwt.claims', null, true)")
