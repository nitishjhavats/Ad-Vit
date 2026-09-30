"""The unattended process. ``python -m app.jobs.runner``.

A separate process with **no HTTP surface at all** — not a router on the API
app, not a protected endpoint, none. That is the anti-bypass property and a test
asserts it: this module must not import FastAPI, because the moment there is an
endpoint that runs a job, there is an endpoint that takes a workspace id from a
caller and hands it to the privileged path. The whole of `app/auth/scope.py`
exists to stop exactly that.

It authenticates by holding the ``advit_jobs`` Postgres password. ``auth.uid()``
is null on that connection, so ``core.log_audit`` takes its trusted branch and
honours ``actor_type => 'automation'`` — attribution a tenant is structurally
forbidden from producing.

**Why there is no cron library here.** A cron scheduler holds "fire at 07:30" in
memory, and this loop instead asks the database "which workspaces have not had
today's brief yet, in their own local day". That is not a smaller version of
cron; it is a different and stronger guarantee:

  * two replicas cannot both run a job, because ``job_runs`` has a unique
    constraint on (workspace, job, local date) and the loser gets 23505;
  * a window missed during a deploy runs LATE rather than being skipped, because
    the question is "has it happened" rather than "is it that time now";
  * 07:30 means 07:30 in the workspace's own timezone, which is the only
    reading a customer would recognise.

Adding APScheduler on top would give a second source of truth about when things
should run, and two sources of truth about timing disagree in production and
nowhere else.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

import psycopg

from app.auth.scope import AuthorizedWorkspace, system_workspace
from app.db import expectations
from app.db.pools import close_pools, open_pools, service_conn
from app.deps import get_driver
from app.ingest.metrics import sync_workspace
from app.learning.outcomes import measure_due
from app.learning.promote import promote
from app.billing import invoices as billing_invoices
from app.billing import settle as billing_settle
from app.watch import platform as platform_watch

log = logging.getLogger("advit.jobs")

# How often to ask. Not how often anything runs - that is decided by the due
# query. A minute is short enough that a 07:30 brief goes out by 07:31 and long
# enough that the query costs nothing.
TICK_SECONDS = int(os.environ.get("JOBS_TICK_SECONDS", "60"))

# A job still 'running' after this did not finish. Generous, because a morning
# brief makes model calls.
ABANDONED_AFTER = os.environ.get("JOBS_ABANDONED_AFTER", "1 hour")


@dataclass(frozen=True, slots=True)
class Job:
    """One piece of scheduled work.

    `local_hour` / `local_minute` are in the WORKSPACE's timezone. `handler`
    takes an AuthorizedWorkspace built by `system_workspace` - the only
    producer that is not a request resolver, because its ids come from a SELECT
    over our own tables and never from a caller.
    """

    name: str
    local_hour: int
    local_minute: int
    handler: Callable[[AuthorizedWorkspace], dict[str, Any]]
    description: str


# ---------------------------------------------------------------------------
# The jobs
# ---------------------------------------------------------------------------


def ingest_metrics(ws: AuthorizedWorkspace) -> dict[str, Any]:
    """Read yesterday's performance before anybody looks at a dashboard.

    Runs first, and early, because everything else in the day reads what it
    writes: the morning brief's facts, the blended economics, the scaling
    verdict. A brief computed before the sync is a brief about the day before
    yesterday.
    """
    reports = [r.as_dict() for r in sync_workspace(get_driver(), workspace_id=ws.id)]
    return {
        "accounts": len(reports),
        "rows_written": sum(r["rows_written"] for r in reports),
        "failed": [r["ad_account_id"] for r in reports if r["error"]],
        # Carried into job_runs.detail_json rather than only logged. A
        # campaign-level total that does not add up to the account total is the
        # only evidence available that a sync dropped something, and a log line
        # is not somewhere anybody looks a week later.
        "discrepancies": [d for r in reports for d in r["discrepancies"]],
        "reports": reports,
    }


def close_the_loop(ws: AuthorizedWorkspace) -> dict[str, Any]:
    """Measure what yesterday's decisions did, then learn from it.

    Measuring and learning are one job rather than two, deliberately. They are
    strictly ordered - a learning is computed from measured outcomes - and
    splitting them across two schedule slots would mean a day on which the
    outcomes were measured and the learning was not, with nothing in the run
    record to say which half had happened.

    No model call anywhere in here. The verdict is arithmetic over two windows
    and the statement is rendered from the numbers, so this job is
    deterministic, costs nothing, and cannot hallucinate a finding. A model is
    the right tool for EXPLAINING a learning to an owner; it is the wrong tool
    for deciding whether one exists.
    """
    measured = measure_due(ws.id)
    learned = promote(ws.id)
    return {
        "outcomes_considered": measured["considered"],
        "verdicts": measured["counts"],
        "measured": measured["measured"],
        "claims_with_enough_evidence": learned["claims"],
        "learnings": learned["written"],
    }


JOBS: tuple[Job, ...] = (
    Job(
        name="ingest_metrics",
        # 06:00 local, ninety minutes before the brief. Meta's own figures for
        # the previous day settle overnight, and reading at 00:05 would ingest
        # numbers that are still moving.
        local_hour=6,
        local_minute=0,
        handler=ingest_metrics,
        description="pull yesterday's performance from every connected ad account",
    ),
    Job(
        name="close_the_loop",
        # 06:30 local: after the sync, before the brief. The order is the point.
        # Measuring a horizon that ended yesterday needs yesterday's metrics
        # ingested, and the brief should be able to say what was learned this
        # morning rather than what was learned a day ago.
        local_hour=6,
        local_minute=30,
        handler=close_the_loop,
        description="measure decisions whose horizon has elapsed, and learn from them",
    ),
)


# ---------------------------------------------------------------------------
# Jobs that belong to the platform, not to a workspace
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformJob:
    """Work about the platform's own state - its rules, its sources.

    No AuthorizedWorkspace, because there is no workspace: the handler takes
    nothing and enumerates nothing a caller could supply. Same claim mechanism
    as a workspace job (the INSERT into job_runs with a NULL workspace_id, which
    job_runs_platform_once_per_day decides), so it runs once a day on however
    many replicas there are.

    The clock is one fixed timezone rather than a workspace's. Every workspace
    so far is IST and the operators are; "06:00" should mean the same thing to
    the person reading job_runs as it does to the person reading a brief.
    """

    name: str
    local_hour: int
    local_minute: int
    handler: Callable[[], dict[str, Any]]
    description: str
    timezone: str = "Asia/Kolkata"


PLATFORM_JOBS: tuple[PlatformJob, ...] = (
    PlatformJob(
        name="platform_watch",
        # 05:00 IST, before any workspace job. If a source moved overnight, the
        # finding should exist before the compliance gate judges the morning's
        # creatives against a rule somebody may want to re-read.
        local_hour=5,
        local_minute=0,
        handler=platform_watch.run,
        description="re-fetch every rule's source and report what moved or went stale",
    ),
    PlatformJob(
        name="settle_subscriptions",
        # 00:15 IST, fifteen minutes BEFORE raise_invoices. The walk rolls a
        # period whose end has passed - or ends a trial - by setting a new
        # current_period_start; raise_invoices then invoices any subscription
        # whose current_period_start is today or earlier with no invoice for
        # it. Settle first, and a period rolled tonight is invoiced tonight;
        # the other way round, it is invoiced tomorrow night and the owner's
        # window to pay is a day shorter than the plan says.
        local_hour=0,
        local_minute=15,
        handler=billing_settle.settle_subscriptions,
        description="move each subscription one step: trial ended, period ended, invoice overdue, grace exhausted",
    ),
    PlatformJob(
        name="raise_invoices",
        # 00:30 IST. A period that starts today is invoiced today, in the
        # night, so the owner finds it in the morning rather than mid-day.
        local_hour=0,
        local_minute=30,
        handler=billing_invoices.raise_invoices,
        description="raise an invoice for every subscription whose period has started",
    ),
)


PLATFORM_DUE = """
select (now() at time zone %(tz)s)::date as local_date
 where (now() at time zone %(tz)s)::time >= make_time(%(hour)s, %(minute)s, 0)
   and not exists (
         select 1 from t_advit.job_runs r
          where r.workspace_id is null
            and r.job = %(job)s
            and r.local_date = (now() at time zone %(tz)s)::date
       )
"""


def platform_due(job: PlatformJob) -> date | None:
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            PLATFORM_DUE,
            {"tz": job.timezone, "hour": job.local_hour, "minute": job.local_minute, "job": job.name},
        )
        row = cur.fetchone()
        return row["local_date"] if row else None


def claim_platform(job: str, local_date: date) -> str | None:
    """The INSERT is the claim, exactly as for a workspace job."""
    try:
        with service_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.job_runs (workspace_id, job, local_date)
                values (null, %s, %s::date)
                returning id::text
                """,
                (job, local_date),
            )
            claimed = cur.fetchone()["id"]
            conn.commit()
            return claimed
    except psycopg.errors.UniqueViolation:
        return None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def claim(workspace_id: str, job: str, local_date: date) -> str | None:
    """Insert the job_runs row, or find out somebody else already has.

    The INSERT is the claim. No advisory lock, no lease, no leader election:
    the unique constraint on (workspace, job, local date) already decides, it
    decides the same way on every replica, and it survives a restart - which an
    in-memory guard does not.
    """
    try:
        with service_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.job_runs (workspace_id, job, local_date)
                values (%s::uuid, %s, %s::date)
                returning id::text
                """,
                (workspace_id, job, local_date),
            )
            claimed = cur.fetchone()["id"]
            conn.commit()
            return claimed
    except psycopg.errors.UniqueViolation:
        # Another replica got there first, or this workspace already had today's
        # run. Both are the same fact and neither is an error.
        return None


def finish(run_id: str, *, status: str, detail: dict[str, Any]) -> None:
    import json

    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            update t_advit.job_runs
               set status = %s, ended_at = now(), detail_json = %s::jsonb
             where id = %s::uuid
            """,
            (status, json.dumps(detail, default=str), run_id),
        )
        conn.commit()


def due(job: Job) -> list[dict[str, Any]]:
    """Which workspaces are due, from a SELECT over our own tables.

    This is the anti-bypass property in one line: there is no input channel. A
    caller cannot name a workspace here, so the scheduler cannot be used to run
    privileged work against an account the caller does not hold.
    """
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "select * from t_advit.workspaces_due(%s, %s, %s)",
            (job.name, job.local_hour, job.local_minute),
        )
        return cur.fetchall()


def run_once() -> dict[str, int]:
    """One tick. Returns what it did, so a test can assert it rather than
    reading logs."""
    counts = {"claimed": 0, "completed": 0, "failed": 0, "reaped": 0}

    with service_conn() as conn, conn.cursor() as cur:
        cur.execute("select t_advit.reap_abandoned_jobs(%s::interval) as n", (ABANDONED_AFTER,))
        counts["reaped"] = cur.fetchone()["n"]
        conn.commit()

    for job in JOBS:
        for row in due(job):
            run_id = claim(str(row["workspace_id"]), job.name, row["local_date"])
            if run_id is None:
                continue
            counts["claimed"] += 1

            workspace = system_workspace(str(row["workspace_id"]), str(row["org_id"]))
            try:
                detail = job.handler(workspace)
            except Exception as exc:  # noqa: BLE001
                # Caught per workspace, deliberately. One tenant's broken Meta
                # token must not stop the other forty from getting their brief,
                # and the row records which one and why rather than the loop
                # dying with a traceback nobody sees until morning.
                log.exception("job %s failed for workspace %s", job.name, row["workspace_id"])
                finish(run_id, status="failed", detail={"error": str(exc)})
                counts["failed"] += 1
                continue

            finish(run_id, status="completed", detail=detail)
            counts["completed"] += 1

    for job in PLATFORM_JOBS:
        local_date = platform_due(job)
        if local_date is None:
            continue
        run_id = claim_platform(job.name, local_date)
        if run_id is None:
            continue
        counts["claimed"] += 1
        try:
            detail = job.handler()
        except Exception as exc:  # noqa: BLE001
            log.exception("platform job %s failed", job.name)
            finish(run_id, status="failed", detail={"error": str(exc)})
            counts["failed"] += 1
            continue
        finish(run_id, status="completed", detail=detail)
        counts["completed"] += 1

    return counts


class _Stop:
    """SIGTERM means a deploy is replacing this container.

    Finishing the tick in flight and then exiting is the difference between a
    job_runs row closed properly and one left 'running' for the reaper to find
    an hour later - during which that workspace's brief cannot be retried,
    because the unique constraint is doing its job.
    """

    def __init__(self) -> None:
        self.requested = False
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, *_: Any) -> None:
        log.info("shutdown requested; finishing the current tick")
        self.requested = True


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("JOBS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    open_pools()

    # The same expectations the API's /health serves 503 on. This process has
    # no health route, so it refuses to start: a runner on a database that
    # lacks a table it writes would fail every tick with a stack trace naming
    # the table and nothing naming the migration. Exit code 3 keeps the
    # container restarting - visibly failing in Coolify - until the operator
    # applies what is listed here.
    with service_conn() as conn, conn.cursor() as cur:
        behind = expectations.missing(cur)
    if behind:
        log.error("database is behind this build: %d migration(s) not applied - %s",
                  len(behind), ", ".join(behind))
        close_pools()
        return 3

    stop = _Stop()
    log.info(
        "jobs runner started: %s, tick %ss",
        ", ".join(f"{j.name}@{j.local_hour:02d}:{j.local_minute:02d} local" for j in JOBS)
        + " | platform: "
        + ", ".join(f"{j.name}@{j.local_hour:02d}:{j.local_minute:02d} {j.timezone}"
                    for j in PLATFORM_JOBS),
        TICK_SECONDS,
    )

    try:
        while not stop.requested:
            try:
                counts = run_once()
            except Exception:  # noqa: BLE001
                # The loop outlives a bad tick. A database blip at 07:29 must
                # not mean no briefs go out at all today - the next tick will
                # find the same workspaces still due, because "due" is a query
                # rather than a fired timer.
                log.exception("tick failed; continuing")
            else:
                if any(counts.values()):
                    log.info("tick: %s", counts)

            for _ in range(TICK_SECONDS):
                if stop.requested:
                    break
                time.sleep(1)
    finally:
        close_pools()
        log.info("jobs runner stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
