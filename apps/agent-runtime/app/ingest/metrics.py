"""The first writer into ``t_advit.metrics_daily``.

Until this module existed the table had no writer at all, which is why the
learning ladder, the analytics tab and every report were correct schema over
zero rows - and why `spend_basis_known` was a column nothing could ever satisfy.

Three properties are worth stating before the code, because each one is a defect
this repository has already had in a different place:

**A missing number is not zero.** Meta omits a field it has no data for. Writing
``coalesce(value, 0)`` turns "not reported" into "measured as zero", and every
rate computed from it inherits the lie - a `results = 0` day looks like an ad set
to pause, when the truth may be that a conversion window has not closed. The
insert below writes NULL through, and the column types allow it.

**A re-sync must not double-count.** ``metrics_daily``'s primary key is
(workspace_id, date, level, entity_id) and this writes ``on conflict ... do
update``. Without that, running a sync twice would double every figure the spend
cap reads. The keying is the idempotency; there is no separate bookkeeping to
get wrong.

**Per-account spend must reconcile against the account total.** An ingestion
that silently drops a campaign produces a perfectly plausible set of rows whose
sum is quietly short. Nothing else in the system can detect that, because
nothing else knows what the total should have been. So the sync fetches both
levels, compares them, and reports the discrepancy rather than deciding on the
caller's behalf what to do about it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.db.pools import service_conn
from app.meta.driver import EntityLevel, Insight, MetaDriver, MetaError

# Meta restates the last few days as attribution windows close and late
# conversions land. Re-reading only today would leave those restatements
# permanently unapplied; re-reading a month on every run is wasted quota.
DEFAULT_LOOKBACK_DAYS = 7

# Beyond this the numbers stop agreeing for reasons that are not a bug -
# different currencies, a mid-period account transfer, Meta's own restatements
# arriving at different levels on different days. Below it, a gap is worth
# someone looking at.
RECONCILIATION_TOLERANCE_PCT = 1.0


@dataclass(slots=True)
class SyncReport:
    """What a sync did, in terms an operator can act on.

    Deliberately not a bare row count. "Wrote 340 rows" answers no question
    anybody has; "spend at campaign level is Rs 4,100 short of the account
    total on three days" is the one that matters, because it is the only
    available evidence that the ingestion dropped something.
    """

    workspace_id: str
    ad_account_id: str
    since: date
    until: date
    rows_written: int = 0
    levels: dict[str, int] = field(default_factory=dict)
    source: str = "meta"
    # Per day: how far the campaign-level sum is from the account-level figure.
    discrepancies: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "ad_account_id": self.ad_account_id,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "rows_written": self.rows_written,
            "levels": self.levels,
            "source": self.source,
            "discrepancies": self.discrepancies,
            "gaps": self.gaps,
            "error": self.error,
        }


_UPSERT = """
insert into t_advit.metrics_daily
  (date, workspace_id, level, entity_id, ad_account_id, source,
   spend_inr, impressions, reach, clicks, link_clicks, results,
   purchases, purchase_value_inr, attribution_regime, ingested_at)
values (%(date)s::date, %(workspace)s::uuid, %(level)s, %(entity)s, %(account)s,
        %(source)s::t_advit.metric_source,
        %(spend)s, %(impressions)s, %(reach)s, %(clicks)s, %(link_clicks)s,
        %(results)s, %(purchases)s, %(purchase_value)s,
        %(regime)s::t_advit.attribution_regime, now())
on conflict (workspace_id, date, level, entity_id) do update set
  -- `excluded`, not `coalesce(excluded, stored)`, and the difference from the
  -- business-truth upsert is deliberate.
  --
  -- A daily-truth message is PARTIAL by design: the owner reports orders at
  -- 20:30 and delivered orders three days later, so the second message must
  -- merge. An insights response is the WHOLE row for that day as Meta currently
  -- believes it - a restatement that dropped a field is Meta saying it no
  -- longer has one, and coalescing would preserve a number the source has
  -- retracted.
  spend_inr          = excluded.spend_inr,
  impressions        = excluded.impressions,
  reach              = excluded.reach,
  clicks             = excluded.clicks,
  link_clicks        = excluded.link_clicks,
  results            = excluded.results,
  purchases          = excluded.purchases,
  purchase_value_inr = excluded.purchase_value_inr,
  attribution_regime = excluded.attribution_regime,
  source             = excluded.source,
  ingested_at        = now()
"""


def _params(workspace_id: str, insight: Insight) -> dict[str, Any]:
    return {
        "date": insight.date,
        "workspace": workspace_id,
        # `.db`, never `.value`: Meta says `ad_account` and `adset`, the CHECK
        # constraint says `account` and `ad_set`, and two of the four labels
        # differ - so passing the wire vocabulary through works for `campaign`
        # and `ad` and raises for the other two.
        "level": insight.level.db,
        "entity": insight.entity_id,
        "account": insight.ad_account_id,
        "source": insight.source,
        # No coalesce anywhere below. None reaches the database as NULL, which
        # is this schema's marker for "not reported" and is distinct from a
        # measured zero.
        "spend": insight.spend_inr,
        "impressions": insight.impressions,
        "reach": insight.reach,
        "clicks": insight.clicks,
        "link_clicks": insight.link_clicks,
        "results": insight.results,
        "purchases": insight.purchases,
        "purchase_value": insight.purchase_value_inr,
        "regime": insight.attribution_regime,
    }


def reconcile(
    account_rows: list[Insight], campaign_rows: list[Insight]
) -> list[dict[str, Any]]:
    """Does the campaign-level spend add up to the account-level figure?

    Pure, so it can be tested without a database or a driver. This is the only
    check in the system capable of noticing that a sync silently dropped a
    campaign: every row it wrote would be individually correct, and the total
    would simply be short.

    A day whose account-level spend is NULL is skipped rather than treated as
    zero - there is nothing to reconcile against, and reporting a 100%
    discrepancy would be inventing the finding.
    """
    by_day: dict[str, float] = {}
    for row in campaign_rows:
        if row.spend_inr is None:
            continue
        by_day[row.date] = by_day.get(row.date, 0.0) + row.spend_inr

    out: list[dict[str, Any]] = []
    for row in account_rows:
        if row.spend_inr is None or row.spend_inr <= 0:
            continue
        summed = by_day.get(row.date, 0.0)
        drift = abs(summed - row.spend_inr) / row.spend_inr * 100.0
        if drift > RECONCILIATION_TOLERANCE_PCT:
            out.append(
                {
                    "date": row.date,
                    "account_spend_inr": round(row.spend_inr, 2),
                    "campaign_spend_inr": round(summed, 2),
                    "drift_pct": round(drift, 2),
                }
            )
    return out


def sync_ad_account(
    driver: MetaDriver,
    *,
    workspace_id: str,
    ad_account_id: str,
    since: date,
    until: date,
) -> SyncReport:
    """Read one ad account's performance and write it down.

    On the SERVICE connection: `authenticated` holds SELECT on metrics_daily and
    nothing else, because these are the numbers the spend cap is checked against
    and a tenant that could write them could raise its own ceiling.
    """
    report = SyncReport(
        workspace_id=workspace_id, ad_account_id=ad_account_id, since=since, until=until
    )

    try:
        account_rows = driver.get_insights(
            ad_account_id, since=since, until=until, level=EntityLevel.ACCOUNT
        )
        campaign_rows = driver.get_insights(
            ad_account_id, since=since, until=until, level=EntityLevel.CAMPAIGN
        )
    except MetaError as exc:
        # Named rather than raised. A sync that fails for one account must not
        # take down a run that was also syncing two others, and the caller needs
        # to know WHICH account and why.
        report.error = f"{exc.kind.value}: {exc.message}"
        return report

    rows = account_rows + campaign_rows
    if not rows:
        report.gaps.append("meta returned no rows for this range")
        return report

    sources = {row.source for row in rows}
    # If a driver ever mixed real and synthetic rows in one response, the column
    # would record whichever came last and the whole account would be
    # mislabelled. Refusing is cheap; noticing later is not.
    if len(sources) > 1:
        report.error = f"insights arrived with mixed sources {sorted(sources)}"
        return report
    report.source = sources.pop()

    with service_conn() as conn, conn.cursor() as cur:
        for row in rows:
            cur.execute(_UPSERT, _params(workspace_id, row))
            report.rows_written += 1
            report.levels[row.level.db] = report.levels.get(row.level.db, 0) + 1
        conn.commit()

    report.discrepancies = reconcile(account_rows, campaign_rows)

    missing_results = sorted({r.date for r in account_rows if r.results is None})
    if missing_results:
        # Recorded as a gap, not smoothed over. A day with no reported results
        # is not a day with no conversions, and every rate computed from it
        # would inherit the difference.
        report.gaps.append(
            f"results not reported on {len(missing_results)} day(s): "
            + ", ".join(missing_results[:5])
            + ("…" if len(missing_results) > 5 else "")
        )

    return report


def sync_workspace(
    driver: MetaDriver,
    *,
    workspace_id: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    today: date | None = None,
) -> list[SyncReport]:
    """Sync every connected ad account in a workspace.

    The ad accounts come from a SELECT over our own `meta_connections`, never
    from a caller - the same rule `app/auth/scope.py::system_workspace` states
    for the scheduler. A route may say WHICH workspace to sync; it may not say
    which ad accounts that workspace has.

    `lookback_days` rather than "since the last sync": Meta restates the last
    few days as attribution windows close and late conversions land, so
    re-reading only new days would leave those restatements permanently
    unapplied. Re-reading is safe because the upsert is keyed.
    """
    # The workspace's own timezone, not the server's. A day boundary in IST is
    # five and a half hours from the one in UTC, and the first five and a half
    # hours of every Indian day would otherwise sync as yesterday - the same
    # defect the monthly cap had before 20260911000002.
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select c.ad_account_id,
                   (now() at time zone w.timezone)::date as local_today
              from t_advit.meta_connections c
              join t_advit.workspaces w on w.id = c.workspace_id
             where c.workspace_id = %s::uuid
             -- No health filter, deliberately. Skipping an 'unhealthy'
             -- connection is how it stays unhealthy: the sync is what would
             -- discover that the token was refreshed and the account is
             -- readable again. A connection that genuinely cannot be read
             -- raises a MetaError, which sync_ad_account names in its report
             -- rather than letting it take down the other accounts.
             order by c.ad_account_id
            """,
            (workspace_id,),
        )
        connections = cur.fetchall()

    if not connections:
        return []

    until = today or connections[0]["local_today"]
    since = until - timedelta(days=max(0, lookback_days - 1))

    return [
        sync_ad_account(
            driver,
            workspace_id=workspace_id,
            ad_account_id=row["ad_account_id"],
            since=since,
            until=until,
        )
        for row in connections
    ]
