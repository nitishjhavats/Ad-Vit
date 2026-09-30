"""The build says what it needs from the database, and refuses to serve
without it.

The defect this closes was found in production on 2026-09-16: the API
container was built from a commit that called ``core.access_mode(uuid)``, and
the database had since applied the migration that turns that overload into
one that always raises. Every workspace request failed; no deploy had failed;
nothing named the cause. Migrations and containers reach production on two
separate triggers, so the code has to carry its own statement of what the
schema must already contain - and the health check has to say NO, not
"degraded", when it does not.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

from app.db import expectations
from conftest import SUPERADMIN, auth

MIGRATIONS = Path(__file__).resolve().parents[3] / "supabase" / "migrations"
SERVICE_DSN = "postgresql://advit_service:advit_service_local@127.0.0.1:54322/postgres"


def test_every_expectation_holds_on_a_freshly_reset_database():
    """The probes are true on the schema the migrations build. A probe that
    is wrong - a misspelt object, a type that never existed - would make every
    deploy fail its health check, so this is the test that keeps the probes
    honest rather than merely present."""
    with psycopg.connect(SERVICE_DSN) as conn, conn.cursor() as cur:
        assert expectations.missing(cur) == []


def test_every_expectation_names_a_real_migration():
    stems = {p.stem for p in MIGRATIONS.glob("*.sql")}
    for version, _ in expectations.EXPECTATIONS:
        assert version in stems, f"{version} is not a migration on disk"


def test_the_newest_migration_on_disk_is_the_newest_expectation():
    """The standing guard. Adding a migration means saying, in one line, what
    the code now needs from it - or, if the code needs nothing new, saying
    that by pointing the probe at any object it creates. A migration nobody
    wrote an expectation for is a migration the health check cannot see."""
    newest = max(p.stem for p in MIGRATIONS.glob("*.sql"))
    assert expectations.EXPECTATIONS[-1][0] == newest, (
        f"newest migration on disk is {newest}; newest expectation is "
        f"{expectations.EXPECTATIONS[-1][0]}. Add a probe for it to app/db/expectations.py."
    )


def test_expectations_are_in_migration_order():
    versions = [v for v, _ in expectations.EXPECTATIONS]
    assert versions == sorted(versions)


def test_a_probe_that_cannot_parse_does_not_poison_the_ones_after_it():
    """``to_regprocedure`` raises when the signature names a type that does
    not exist, which is exactly the state of a database that lacks the
    migration creating the type. Under a savepoint per probe, the later probes
    still answer for themselves."""
    with psycopg.connect(SERVICE_DSN) as conn, conn.cursor() as cur:
        original = expectations.EXPECTATIONS
        try:
            expectations.EXPECTATIONS = (
                ("00000000000000_a_type_that_does_not_exist",
                 "select to_regprocedure('core.nope(core.no_such_type)') is not null"),
            ) + original
            behind = expectations.missing(cur)
        finally:
            expectations.EXPECTATIONS = original
        assert behind == ["00000000000000_a_type_that_does_not_exist"]
        # And the transaction is still usable.
        cur.execute("select 1")
        assert cur.fetchone()[0] == 1


def test_health_answers_503_while_the_database_is_behind(monkeypatch):
    """Not 200-with-"degraded": the container health check is `curl -f`, and
    a 200 would let Coolify route traffic to code the schema cannot serve."""
    from app.main import app

    client = TestClient(app)
    assert client.get("/health").status_code == 200

    monkeypatch.setattr(app.state, "schema_behind", ["20990101000001_the_future"], raising=False)
    monkeypatch.setattr(expectations, "missing", lambda cur: ["20990101000001_the_future"])
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "behind"

    detail = client.get("/api/health/detail", headers=auth(SUPERADMIN)).json()
    assert detail["schema"] == {"ok": False, "missing": ["20990101000001_the_future"]}

    # The moment the migration lands, the answer flips without a restart.
    monkeypatch.setattr(expectations, "missing", lambda cur: [])
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    monkeypatch.setattr(app.state, "schema_behind", [], raising=False)
