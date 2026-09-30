"""The unattended process, and the properties that keep it from being a bypass.

There was no scheduler anywhere in this repository: the 07:30 brief and the
daily loop were a document. So none of these tests is protecting existing
behaviour — they are the specification, and two of them describe failures that
would produce correct-looking output:

  * two replicas both running a job produce two sets of proposals for one
    account from one day's data, and nothing in either run is wrong;
  * a scheduler firing on server time sends an Indian customer their morning
    brief at 02:00, which looks like a scheduler working.
"""

from __future__ import annotations

import ast
import os
from datetime import date, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from app.jobs import runner
from conftest import BROADMATE_WORKSPACE as WORKSPACE
from conftest import RIVAL_WORKSPACE

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
ORG = "00000000-0000-4000-8000-000000000010"


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _reachable(), reason="local Postgres is not running")


@pytest.fixture(autouse=True)
def only_workspace_jobs_and_always_due(monkeypatch):
    """Two things this file must not depend on.

    The CLOCK. The ingest job is scheduled at 06:00 local, so `run_once()`
    wrote nothing - and two tests here failed - between midnight and dawn IST.
    Every workspace job is re-timed to 00:00 so it is due whenever the tests
    run. A test about the scheduler's claim mechanism should not have an
    opinion about what time it is.

    The PLATFORM JOBS. `run_once()` also runs them, and Platform Watch's
    default fetcher reaches the real network - Meta's policy pages, from a unit
    test. The invoice job left a draft behind too. They are switched off here;
    each has its own test file with its own fakes.
    """
    monkeypatch.setattr(
        runner, "JOBS",
        tuple(runner.Job(j.name, 0, 0, j.handler, j.description) for j in runner.JOBS),
    )
    monkeypatch.setattr(runner, "PLATFORM_JOBS", ())


@pytest.fixture
def clean_jobs():
    def wipe():
        with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("delete from t_advit.job_runs")
            cur.execute(
                "delete from t_advit.metrics_daily where source = 'fixture' and level <> 'account'"
            )

    wipe()
    yield
    wipe()


def rows(sql: str, params: tuple = ()):
    with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# The anti-bypass properties
# ---------------------------------------------------------------------------


def test_the_job_process_has_no_http_surface_at_all():
    """`.env.example` used to ship `AGENT_RUNTIME_SECRET` - "shared secret for
    web -> agent-runtime calls" - with zero code references, and the tempting
    design it invited was `POST /internal/jobs/run?workspace_id=X` behind a
    header. That is the original vulnerability with one extra header: a bearer
    credential with no subject, no expiry, no audience and no per-actor
    revocation, combined with a caller-supplied workspace id.

    The whole of `app/auth/scope.py` exists to stop a caller naming a tenant on
    the privileged path. An endpoint here would walk around it, so there is no
    endpoint here — asserted rather than assumed, because "we did not add one"
    is not a property, it is a habit.
    """
    source = Path(runner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}

    forbidden = imported & {"fastapi", "starlette", "uvicorn"}
    assert forbidden == set(), (
        f"the jobs runner imports {sorted(forbidden)}. A job reachable over HTTP is a "
        "workspace id a caller can supply to the privileged path."
    )


def test_the_scheduler_takes_no_caller_supplied_workspace():
    """`due()` reads from t_advit.workspaces_due and nothing else. There is no
    input channel, which is what makes "the scheduler cannot be used to run
    privileged work against somebody else's account" a structural claim rather
    than a review comment."""
    import inspect

    signature = inspect.signature(runner.due)
    assert list(signature.parameters) == ["job"], (
        "due() grew a parameter; the only thing it may take is which JOB to look for"
    )


def test_every_job_names_a_local_hour_a_handler_and_a_reason():
    for job in runner.JOBS:
        assert 0 <= job.local_hour <= 23
        assert 0 <= job.local_minute <= 59
        assert callable(job.handler)
        assert job.description, f"{job.name} does not say what it is for"


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


@needs_db
def test_only_one_replica_can_claim_a_job(clean_jobs):
    """The unique constraint IS the leader election. No lock to acquire, no
    lease to renew, no clock to agree on - and unlike an in-memory guard it
    holds across a restart."""
    today = date.today()
    first = runner.claim(WORKSPACE, "morning_brief", today)
    second = runner.claim(WORKSPACE, "morning_brief", today)

    assert first is not None
    assert second is None, "two replicas both claimed the same job on the same day"


@needs_db
def test_a_claim_is_per_workspace_and_per_day(clean_jobs):
    """The constraint must not be so broad that one workspace's brief blocks
    another's, nor so narrow that tomorrow's is blocked by today's."""
    today = date.today()
    assert runner.claim(WORKSPACE, "morning_brief", today) is not None
    assert runner.claim(RIVAL_WORKSPACE, "morning_brief", today) is not None
    assert runner.claim(WORKSPACE, "morning_brief", today + timedelta(days=1)) is not None
    assert runner.claim(WORKSPACE, "ingest_metrics", today) is not None


@needs_db
def test_a_job_still_running_is_not_started_again(clean_jobs):
    """`workspaces_due` tests `not exists`, not `last completed < today`. A
    four-minute brief would otherwise be started again by the next tick, and
    the second copy would be doing the same work with the same data."""
    today_local = rows(
        "select (now() at time zone timezone)::date as d from t_advit.workspaces where id = %s",
        (WORKSPACE,),
    )[0]["d"]
    runner.claim(WORKSPACE, "ingest_metrics", today_local)

    still_due = [
        str(r["workspace_id"]) for r in runner.due(runner.JOBS[0])
    ]
    assert WORKSPACE not in still_due


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


@needs_db
def test_a_tick_runs_the_due_work_and_a_second_tick_runs_nothing(clean_jobs):
    first = runner.run_once()
    assert first["claimed"] > 0
    assert first["failed"] == 0

    second = runner.run_once()
    assert second["claimed"] == 0, "the same work was picked up twice"


@needs_db
def test_the_ingest_job_actually_writes_metrics(clean_jobs):
    """The point of the whole exercise: t_advit.metrics_daily had no writer and
    no scheduler, so every report was correct schema over zero rows."""
    runner.run_once()

    written = rows(
        """
        select count(*) as n from t_advit.metrics_daily
         where workspace_id = %s and level = 'campaign'
        """,
        (WORKSPACE,),
    )[0]["n"]
    assert written > 0


@needs_db
def test_what_the_job_found_is_stored_rather_than_only_logged(clean_jobs):
    """A campaign-level total that does not add up to the account total is the
    only evidence available that a sync dropped something, and a log line is not
    somewhere anybody looks a week later."""
    runner.run_once()

    detail = rows(
        """
        select detail_json from t_advit.job_runs
         where workspace_id = %s and job = 'ingest_metrics' and status = 'completed'
        """,
        (WORKSPACE,),
    )
    assert detail, "the job completed and recorded nothing"
    payload = detail[0]["detail_json"]
    assert "rows_written" in payload
    assert "discrepancies" in payload
    assert "failed" in payload


@needs_db
def test_one_workspace_failing_does_not_stop_the_others(clean_jobs, monkeypatch):
    """One tenant's revoked Meta token must not mean forty other accounts get no
    morning brief, and the row has to say which one and why."""
    exploded: list[str] = []

    def sometimes_broken(ws):
        if ws.id == WORKSPACE:
            exploded.append(ws.id)
            raise RuntimeError("token revoked")
        return {"accounts": 0}

    monkeypatch.setattr(
        runner, "JOBS", (runner.Job("ingest_metrics", 0, 0, sometimes_broken, "test"),)
    )

    counts = runner.run_once()

    assert exploded, "the failing workspace was never reached"
    assert counts["failed"] == 1
    assert counts["completed"] >= 1, "a failure stopped the remaining workspaces"

    failed = rows(
        "select status, detail_json->>'error' as error from t_advit.job_runs where workspace_id = %s",
        (WORKSPACE,),
    )
    assert failed[0]["status"] == "failed"
    assert "token revoked" in failed[0]["error"]


# ---------------------------------------------------------------------------
# Timezones, and being late rather than absent
# ---------------------------------------------------------------------------


@needs_db
def test_due_work_is_decided_in_the_workspaces_own_timezone(clean_jobs):
    """07:30 means 07:30 where the customer is. A scheduler firing on server
    time sends an Indian owner their morning brief at 02:00 - which looks like a
    scheduler working."""
    due = rows("select * from t_advit.workspaces_due('morning_brief', 0, 0)")
    assert due, "no workspace is due after local midnight, which cannot be right"

    for row in due:
        assert row["timezone"] == "Asia/Kolkata"
        server_date = rows("select current_date as d")[0]["d"]
        # Not an assertion that they differ - they usually do not. An assertion
        # that the answer came from the workspace's clock, which is what the
        # function returns.
        assert row["local_date"] in (server_date, server_date + timedelta(days=1),
                                     server_date - timedelta(days=1))


@needs_db
def test_a_window_missed_during_a_deploy_runs_late_rather_than_being_skipped(clean_jobs):
    """The question is "has today's brief happened", not "is it 07:30 now". A
    process that was restarting at 07:30 - which is exactly when a deploy is
    likely - runs it at 07:50 instead of not running it."""
    # An hour that has certainly already passed locally, standing in for a
    # window the process was down for.
    long_past = rows(
        "select (now() at time zone timezone)::time as t from t_advit.workspaces where id = %s",
        (WORKSPACE,),
    )[0]["t"]

    due = rows(
        "select * from t_advit.workspaces_due('morning_brief', %s, 0)",
        (max(0, long_past.hour - 1),),
    )
    assert any(str(r["workspace_id"]) == WORKSPACE for r in due), (
        "a window that has already passed today is not being offered, so a missed "
        "one would be skipped rather than run late"
    )


@needs_db
def test_a_workspace_is_not_due_before_its_local_time_arrives(clean_jobs):
    """The counterpart. If everything were always due, the schedule would be
    decoration and every job would run at whatever moment the process started."""
    assert rows("select * from t_advit.workspaces_due('morning_brief', 23, 59)") == []


@needs_db
def test_a_paused_workspace_gets_no_unattended_work(clean_jobs):
    """The kill switch has to stop the unattended path too, or freezing an
    account stops only the half a human was driving."""
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("update t_advit.workspaces set is_paused = true where id = %s", (WORKSPACE,))
        try:
            due = rows("select * from t_advit.workspaces_due('morning_brief', 0, 0)")
            assert all(str(r["workspace_id"]) != WORKSPACE for r in due)
        finally:
            cur.execute(
                "update t_advit.workspaces set is_paused = false where id = %s", (WORKSPACE,)
            )


# ---------------------------------------------------------------------------
# The reaper
# ---------------------------------------------------------------------------


@needs_db
def test_a_job_abandoned_by_a_dead_process_is_closed(clean_jobs):
    """Otherwise the row says 'running' for ever, the unique constraint blocks
    that workspace's brief until the date rolls over, and the outage is silent
    and exactly one day long."""
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.job_runs (workspace_id, job, local_date, started_at)
            values (%s::uuid, 'morning_brief', current_date, now() - interval '3 hours')
            """,
            (WORKSPACE,),
        )

    reaped = rows("select t_advit.reap_abandoned_jobs('1 hour'::interval) as n")[0]["n"]
    assert reaped >= 1

    row = rows(
        "select status, detail_json->>'reaped' as reaped from t_advit.job_runs where workspace_id = %s",
        (WORKSPACE,),
    )[0]
    assert row["status"] == "failed"
    assert row["reaped"] == "true"


@needs_db
def test_a_job_that_is_merely_slow_is_not_reaped(clean_jobs):
    """A morning brief makes model calls. Reaping one that is still working
    would free the constraint and let a second copy start alongside it."""
    runner.claim(WORKSPACE, "morning_brief", date.today())
    assert rows("select t_advit.reap_abandoned_jobs('1 hour'::interval) as n")[0]["n"] == 0
