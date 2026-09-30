"""The nightly walk of the subscription state machine.

``core.settle_subscriptions`` (20260918000002) moves every subscription one
step along the machine the enum comments in 20260903000001 drew: a trial that
has ended becomes pending_payment; an active period that has ended rolls
forward and becomes pending_payment; a pending_payment whose issued invoice
is overdue becomes past_due with a grace deadline; a past_due whose grace has
run out becomes expired. And it closes every payment window nobody used.

The function does the walking. This module is the platform job that calls it
- from the jobs process, where no principal is bound, on the service
connection, because the walk is the platform's act on every subscription and
not any caller's. It is granted to ``advit_backend`` alone, and a session
calling it gets ``permission denied`` from the grant before the function's
own guard says the same thing.

It runs at 00:15 IST, before ``raise_invoices`` at 00:30, so a period rolled
tonight is invoiced tonight: ``raise_invoices`` invoices any subscription
whose current_period_start is today or earlier and has no invoice yet.

Nothing here decides anything. What it returns is what the function returned,
so the ``job_runs`` row says which subscriptions moved and why.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.db.pools import service_conn

log = logging.getLogger(__name__)


def settle_subscriptions(*, now: datetime | None = None, org_id: str | None = None) -> dict[str, Any]:
    """Run the walk and report it. ``now`` and ``org_id`` exist for a test to
    move the clock and narrow the walk; the scheduled job passes neither, and
    the SQL coalesces a missing clock to the database's rather than passing a
    NULL the function would refuse."""
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select subscription_id::text as subscription_id, org_id::text as org_id,
                   from_status, to_status, reason
              from core.settle_subscriptions(coalesce(%s::timestamptz, now()), %s::uuid)
            """,
            (now, org_id),
        )
        transitions = cur.fetchall()
        conn.commit()

    log.info("settle: %d subscription(s) moved", len(transitions))
    return {"transitions": transitions, "count": len(transitions)}
