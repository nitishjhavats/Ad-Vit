"""The first writer into ``t_advit.metrics_daily``.

The table had no writer, which is why the learning ladder, the analytics tab and
every report were correct schema over zero rows.

Three properties, each of which is a defect this repository has already had
somewhere else:

  * a missing number is not zero — the schema could not even *say* "not
    reported" until 20260911000011, because every measured column was
    ``NOT NULL DEFAULT 0``;
  * a re-sync must not double-count, or running it twice doubles the figure the
    spend cap reads;
  * per-account spend must reconcile against the account total, which is the
    only check capable of noticing that a sync silently dropped a campaign —
    every row it wrote would be individually correct and the sum would simply be
    short.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from app.ingest.metrics import (
    RECONCILIATION_TOLERANCE_PCT,
    SyncReport,
    reconcile,
    sync_ad_account,
    sync_workspace,
)
from app.meta.driver import EntityLevel, Insight, MetaError, MetaErrorKind
from app.meta.fixture import FixtureDriver
from conftest import BROADMATE_WORKSPACE as WORKSPACE

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
WRITABLE_ACCOUNT = "1000000000000003"
POPULATED_ACCOUNT = "1000000000000001"

# Every sync in this file targets a window ninety days back, and that is not
# arbitrary caution.
#
# The seed writes account-level rows for the last five days, and several other
# suites depend on them: `month_basis_known` is an EXISTS over account rows in
# the current month, and without it the monthly cap refuses every mutation.
# metrics_daily is keyed on (workspace, date, level, entity), so a sync over
# today's dates OVERWRITES those rows - and this file's own cleanup then deletes
# them, leaving 23 failures in two other suites that look nothing like an
# ingestion bug.
#
# Ninety days back is in a different month and outside every other fixture's
# reach, so the cleanup can delete exactly what it wrote.
WINDOW_END = date.today() - timedelta(days=90)


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _reachable(), reason="local Postgres is not running")


@pytest.fixture
def driver() -> FixtureDriver:
    return FixtureDriver(write_allowlist={WRITABLE_ACCOUNT})


@pytest.fixture
def clean_metrics():
    """Remove exactly the window this file writes, and nothing else.

    Scoped by DATE rather than by `ingested_at`, because the seeded rows are
    re-ingested by any sync that overlaps them and would then match a
    "written recently" predicate - which is how the first version of this
    fixture deleted the seed and broke two other suites.
    """
    yield
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "delete from t_advit.metrics_daily where workspace_id = %s and date <= %s",
            (WORKSPACE, WINDOW_END),
        )


def rows(sql: str, params: tuple = ()):
    with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Reconciliation — pure, so it needs no database and no driver
# ---------------------------------------------------------------------------


def insight(day: str, level: EntityLevel, entity: str, spend):
    return Insight(
        date=day, level=level, entity_id=entity, ad_account_id="acct",
        source="fixture", spend_inr=spend,
    )


def test_levels_that_agree_report_no_discrepancy():
    account = [insight("2026-09-01", EntityLevel.ACCOUNT, "acct", 1000.0)]
    campaigns = [
        insight("2026-09-01", EntityLevel.CAMPAIGN, "c1", 600.0),
        insight("2026-09-01", EntityLevel.CAMPAIGN, "c2", 400.0),
    ]
    assert reconcile(account, campaigns) == []


def test_a_dropped_campaign_shows_up_as_drift():
    """The failure this check exists for. Every row written is individually
    correct; the sum is simply short, and nothing else in the system knows what
    the total should have been."""
    account = [insight("2026-09-01", EntityLevel.ACCOUNT, "acct", 1000.0)]
    campaigns = [insight("2026-09-01", EntityLevel.CAMPAIGN, "c1", 600.0)]

    found = reconcile(account, campaigns)
    assert len(found) == 1
    assert found[0]["account_spend_inr"] == 1000.0
    assert found[0]["campaign_spend_inr"] == 600.0
    assert found[0]["drift_pct"] == 40.0


def test_a_rounding_difference_is_not_reported_as_drift():
    """Meta's own restatements arrive at different levels on different days. A
    check that fired on every sub-1% difference would be noise, and noise is
    what people learn to scroll past."""
    account = [insight("2026-09-01", EntityLevel.ACCOUNT, "acct", 1000.0)]
    within = 1000.0 * (1 - RECONCILIATION_TOLERANCE_PCT / 200)
    assert reconcile(account, [insight("2026-09-01", EntityLevel.CAMPAIGN, "c1", within)]) == []


def test_a_day_whose_account_spend_is_unknown_is_skipped_not_flagged():
    """There is nothing to reconcile against. Reporting 100% drift would be
    inventing the finding - the same shape as the coalesce-to-zero defects this
    module's docstring lists."""
    account = [insight("2026-09-01", EntityLevel.ACCOUNT, "acct", None)]
    assert reconcile(account, []) == []


# ---------------------------------------------------------------------------
# The driver's half
# ---------------------------------------------------------------------------


def test_the_account_figure_is_the_sum_of_its_campaigns(driver):
    """Not cosmetic. If the levels disagreed by construction, the
    reconciliation check would fire on every single day of every fixture run,
    and a check that always fires is worse than no check."""
    since, until = date(2026, 9, 1), date(2026, 9, 3)
    account = driver.get_insights(POPULATED_ACCOUNT, since=since, until=until)
    campaigns = driver.get_insights(
        POPULATED_ACCOUNT, since=since, until=until, level=EntityLevel.CAMPAIGN
    )

    assert reconcile(account, campaigns) == []
    assert round(sum(r.spend_inr for r in account), 2) == round(
        sum(r.spend_inr for r in campaigns), 2
    )


def test_every_fixture_row_says_it_is_a_fixture(driver):
    """The snapshot's own note says metric fixtures "go stale silently and
    invite false confidence". They cannot, if they are labelled and the label is
    what `has_measured_spend` reads."""
    rows_ = driver.get_insights(POPULATED_ACCOUNT, since=date(2026, 9, 1), until=date(2026, 9, 2))
    assert {r.source for r in rows_} == {"fixture"}


def test_insights_are_deterministic(driver):
    """A random fixture makes every assertion either trivial or flaky."""
    args = dict(since=date(2026, 9, 1), until=date(2026, 9, 5))
    first = driver.get_insights(POPULATED_ACCOUNT, **args)
    second = FixtureDriver(write_allowlist={WRITABLE_ACCOUNT}).get_insights(
        POPULATED_ACCOUNT, **args
    )
    assert [r.spend_inr for r in first] == [r.spend_inr for r in second]


def test_some_days_report_no_results_at_all(driver):
    """Absent, not zero. Meta omits the field while a conversion window is open,
    and a pipeline that has never seen the difference will collapse them the
    first time it meets one."""
    rows_ = driver.get_insights(
        POPULATED_ACCOUNT, since=date(2026, 9, 1), until=date(2026, 9, 21)
    )
    assert any(r.results is None for r in rows_), "no fixture day exercises the absent case"
    assert any(r.results is not None for r in rows_)


def test_a_backwards_date_range_is_refused(driver):
    with pytest.raises(MetaError) as exc:
        driver.get_insights(POPULATED_ACCOUNT, since=date(2026, 9, 5), until=date(2026, 9, 1))
    assert exc.value.kind is MetaErrorKind.VALIDATION


def test_a_range_beyond_metas_own_limit_is_refused(driver):
    """Meta caps one insights call at 92 days. A driver that quietly accepted
    more would truncate in production and not here, which is the worst place for
    the two to differ."""
    with pytest.raises(MetaError) as exc:
        driver.get_insights(
            POPULATED_ACCOUNT, since=date(2026, 1, 1), until=date(2026, 12, 31)
        )
    assert "92" in exc.value.message


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


@needs_db
def test_a_sync_writes_rows_and_names_where_they_came_from(driver, clean_metrics):
    until = WINDOW_END
    report = sync_ad_account(
        driver,
        workspace_id=WORKSPACE,
        ad_account_id=POPULATED_ACCOUNT,
        since=until - timedelta(days=6),
        until=until,
    )

    assert report.ok, report.error
    assert report.rows_written > 0
    assert report.source == "fixture"
    assert report.levels["account"] == 7

    stored = rows(
        """
        select count(*) as n, count(distinct source) as sources
          from t_advit.metrics_daily
         where workspace_id = %s and ad_account_id = %s and level = 'campaign'
        """,
        (WORKSPACE, POPULATED_ACCOUNT),
    )
    assert stored[0]["n"] > 0
    assert stored[0]["sources"] == 1


@needs_db
def test_running_the_same_sync_twice_does_not_double_the_spend(driver, clean_metrics):
    """The primary key is the idempotency. Without `on conflict do update`, a
    second sync would double every figure the daily cap is checked against."""
    until = WINDOW_END
    window = dict(since=until - timedelta(days=4), until=until)

    sync_ad_account(driver, workspace_id=WORKSPACE, ad_account_id=POPULATED_ACCOUNT, **window)
    first = rows(
        "select count(*) as n, sum(spend_inr) as spend from t_advit.metrics_daily where workspace_id = %s",
        (WORKSPACE,),
    )[0]

    sync_ad_account(driver, workspace_id=WORKSPACE, ad_account_id=POPULATED_ACCOUNT, **window)
    second = rows(
        "select count(*) as n, sum(spend_inr) as spend from t_advit.metrics_daily where workspace_id = %s",
        (WORKSPACE,),
    )[0]

    assert first == second, f"a re-sync changed the totals: {first} -> {second}"


@needs_db
def test_not_reported_is_stored_as_null_and_not_as_zero(driver, clean_metrics):
    """The property `t_advit.metrics_daily` could not express at all until
    20260911000011, because every measured column was NOT NULL DEFAULT 0.

    Stored as 0, a day with an open conversion window reads as "the ads ran and
    nobody converted" - a reason to pause an ad set that may be performing.
    """
    until = WINDOW_END
    sync_ad_account(
        driver,
        workspace_id=WORKSPACE,
        ad_account_id=POPULATED_ACCOUNT,
        since=until - timedelta(days=20),
        until=until,
    )

    absent = rows(
        """
        select count(*) as n from t_advit.metrics_daily
         where workspace_id = %s and ad_account_id = %s and results is null
        """,
        (WORKSPACE, POPULATED_ACCOUNT),
    )[0]["n"]
    assert absent > 0, "every day stored a number for results; the NULL path is not exercised"


@needs_db
def test_a_fixture_sync_does_not_satisfy_the_spend_basis(driver, clean_metrics):
    """The whole reason `source` exists.

    A developer's offline loop must not look, to the spend cap, exactly like a
    synced production account - which is what would happen if synthetic rows
    counted as measurement.
    """
    until = WINDOW_END
    sync_ad_account(
        driver,
        workspace_id=WORKSPACE,
        ad_account_id=POPULATED_ACCOUNT,
        since=until - timedelta(days=3),
        until=until,
    )

    measured = rows("select t_advit.has_measured_spend(%s) as m", (WORKSPACE,))[0]["m"]
    assert measured is False, "fixture rows are being counted as real ingested spend"


@needs_db
def test_a_workspace_sync_reads_its_accounts_from_our_own_tables(driver, clean_metrics):
    """The caller says which WORKSPACE. It does not say which ad accounts - the
    same rule `system_workspace` states for the unattended path. A caller that
    could name an account could make this process read one the workspace does
    not hold."""
    reports = sync_workspace(driver, workspace_id=WORKSPACE, lookback_days=3, today=WINDOW_END)

    connected = {
        r["ad_account_id"]
        for r in rows(
            "select ad_account_id from t_advit.meta_connections where workspace_id = %s",
            (WORKSPACE,),
        )
    }
    assert {r.ad_account_id for r in reports} == connected


@needs_db
def test_a_workspace_with_no_connections_syncs_nothing_and_says_so(driver):
    assert sync_workspace(
        driver, workspace_id="00000000-0000-4000-8000-0000000000aa", today=WINDOW_END
    ) == []


def test_one_failing_account_does_not_take_down_the_others():
    """A sync that raises for one account must not abandon the two that would
    have succeeded, and the caller needs to know WHICH account and why."""

    class Broken(FixtureDriver):
        def get_insights(self, ad_account_id, **kw):
            raise MetaError(MetaErrorKind.PERMISSION, "token lacks ads_read")

    report = sync_ad_account(
        Broken(write_allowlist=set()),
        workspace_id=WORKSPACE,
        ad_account_id=POPULATED_ACCOUNT,
        since=date(2026, 9, 1),
        until=date(2026, 9, 2),
    )
    assert report.ok is False
    assert "ads_read" in report.error
    assert report.rows_written == 0


def test_a_report_is_serialisable_for_a_route():
    report = SyncReport(
        workspace_id=WORKSPACE, ad_account_id="a", since=date(2026, 9, 1), until=date(2026, 9, 2)
    )
    payload = report.as_dict()
    assert payload["since"] == "2026-09-01"
    assert payload["error"] is None
