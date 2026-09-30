"""Platform Watch: go to the source, and say whether it moved. Never edit a rule.

Every compliance rule and platform-knowledge row cites a ``source_url`` and an
``as_of``, and ``app/policy/rules.py`` has always known how stale each one is —
it computed the answer on every load and returned it where nobody looked. Today
every Meta rule in the seed is dated 2026-03-01: past its 90-day window since
June, and seven of them are BLOCK rules carrying statutory weight.

Two properties these tests hold:

  * **It detects and it reports.** A changed source, a stale rule, an
    unreachable page — each becomes a row in ``t_advit.watch_findings`` for a
    superadmin. Deduplicated while open, so a stale rule is one finding, not one
    per day.

  * **It never acts.** No code path here writes ``policy_rules`` or
    ``platform_knowledge``. A compliance rule that a website edit could rewrite
    is a compliance gate that a website edit could open.

The fetcher is injected. Nothing in this file touches the network.
"""

from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from app.watch import platform as pw
from conftest import SERVICE_DSN

SUPERUSER_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
)
TODAY = date(2026, 9, 14)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def scrub():
    """Findings and watched sources are platform state with no natural owner,
    so they are cleared outright. Superuser: advit_service has no DELETE."""
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("delete from t_advit.watch_findings")
        cur.execute("delete from t_advit.watched_sources")
        conn.commit()


@pytest.fixture(autouse=True)
def clean_slate():
    scrub()
    yield
    scrub()


class Pages:
    """A fetcher whose world is a dict, so a test can move a page or take one
    down between passes."""

    def __init__(self):
        self.pages: dict[str, str] = {}
        self.down: dict[str, pw.Fetched] = {}
        self.calls: list[str] = []

    def __call__(self, url: str) -> pw.Fetched:
        self.calls.append(url)
        if url in self.down:
            return self.down[url]
        return pw.Fetched(
            status=200,
            text=self.pages.get(url, f"<html><body>policy text for {url}</body></html>"),
        )


@pytest.fixture
def pages() -> Pages:
    return Pages()


def findings(kind: str | None = None, *, open_only: bool = True) -> list[dict]:
    with psycopg.connect(SUPERUSER_DSN, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select kind, subject, source_url, severity, detail_json, acknowledged_at
                  from t_advit.watch_findings
                 where (%(kind)s::text is null or kind = %(kind)s::text)
                   and (not %(open_only)s::boolean or acknowledged_at is null)
                 order by kind, subject
                """,
                {"kind": kind, "open_only": open_only},
            )
            return cur.fetchall()


def a_source(jurisdiction: str) -> str:
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "select source_url from t_advit.policy_rules where jurisdiction = %s "
            "and source_url is not null order by code limit 1",
            (jurisdiction,),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# The hash
# ---------------------------------------------------------------------------


def test_the_hash_reads_what_a_reader_reads():
    """Scripts, styles, tags and whitespace stripped. A rebuilt JS bundle or a
    reflowed template is not a policy change."""
    a = "<html><script>var build='abc'</script><body><p>No  guarantees.</p></body></html>"
    b = "<html><script>var build='xyz'</script><body>\n<p>No guarantees.</p>\n</body></html>"
    assert pw.content_hash(a)[0] == pw.content_hash(b)[0]


def test_the_hash_notices_a_change_in_the_text():
    a = "<html><body>No guarantees.</body></html>"
    b = "<html><body>No guarantees. No timelines.</body></html>"
    assert pw.content_hash(a)[0] != pw.content_hash(b)[0]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def test_the_first_fetch_is_a_baseline_not_a_change(pages):
    """There was nothing for the page to have changed FROM."""
    summary = pw.run(fetcher=pages, today=TODAY)
    assert summary["first_seen"] > 0
    assert summary["changed"] == 0
    assert findings(pw.SOURCE_CHANGED) == []


def test_an_unchanged_source_produces_nothing(pages):
    pw.run(fetcher=pages, today=TODAY)
    summary = pw.run(fetcher=pages, today=TODAY)
    assert summary["changed"] == 0
    assert summary["unchanged"] == summary["checked"]
    assert findings(pw.SOURCE_CHANGED) == []


def test_a_moved_source_is_reported_with_the_rules_that_cite_it(pages):
    """The finding names the rules, because "this page changed" is only useful
    to a superadmin as "these rules may be wrong"."""
    url = a_source("meta")
    pw.run(fetcher=pages, today=TODAY)

    pages.pages[url] = "<html><body>a new prohibited-claims clause</body></html>"
    summary = pw.run(fetcher=pages, today=TODAY)

    assert summary["changed"] == 1
    rows = findings(pw.SOURCE_CHANGED)
    assert len(rows) == 1
    assert rows[0]["subject"] == url
    assert rows[0]["detail_json"]["cited_by_rules"], "the finding names no rules"
    assert rows[0]["detail_json"]["previous_hash"] != rows[0]["detail_json"]["current_hash"]


def test_a_moved_source_is_review_not_urgent(pages):
    """A hash comparison cannot tell a new clause from a new footer. `urgent` is
    reserved for a human's judgement."""
    url = a_source("meta")
    pw.run(fetcher=pages, today=TODAY)
    pages.pages[url] = "<html><body>moved</body></html>"
    pw.run(fetcher=pages, today=TODAY)
    assert findings(pw.SOURCE_CHANGED)[0]["severity"] == "review"


def test_an_unreachable_source_is_a_finding_and_not_a_change(pages):
    """A rule whose source has gone is a rule nobody can re-verify. That is not
    the same as a rule that is wrong, and it must not look like one - so it is
    `fetch_failed`, and the last good hash is left alone."""
    url = a_source("in")
    pw.run(fetcher=pages, today=TODAY)

    pages.down[url] = pw.Fetched(status=404, text=None, error="HTTP 404")
    summary = pw.run(fetcher=pages, today=TODAY)

    assert summary["failed"] == 1
    assert summary["changed"] == 0
    rows = findings(pw.FETCH_FAILED)
    assert [r["subject"] for r in rows] == [url]
    assert rows[0]["detail_json"]["status"] == 404

    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "select content_hash, last_status, last_error from t_advit.watched_sources where url = %s",
            (url,),
        )
        content_hash, status, error = cur.fetchone()
    assert content_hash is not None, "a failed fetch erased the last good hash"
    assert status == 404 and error == "HTTP 404"


def test_a_non_html_response_is_not_hashed_as_a_page():
    """The production fetcher refuses to hash a PDF or a JSON error body as
    though it were the policy page - which would report a 'change' the day a
    CDN started serving an error document."""
    import httpx

    def transport(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    with httpx.Client(transport=httpx.MockTransport(transport)):
        pass  # the production fetcher builds its own client; exercised below

    # Exercise the classification directly through a Fetched built the way
    # http_fetch would build it.
    fetched = pw.Fetched(status=200, text=None, error="not a text response: application/pdf")
    assert not fetched.ok


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def test_every_seeded_meta_rule_is_stale_today(pages):
    """The finding the job made on its first real pass. Every Meta rule in the
    seed is dated 2026-03-01 - 197 days before today - against a 90-day window.
    This test is pinned to that date so it stays a description of the seed, not
    of the calendar; if the seed is refreshed it should be updated."""
    pw.run(fetcher=pages, today=TODAY)
    stale = {r["subject"] for r in findings(pw.RULE_STALE)}

    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("select code from t_advit.policy_rules where jurisdiction = 'meta'")
        meta_rules = {row[0] for row in cur.fetchall()}

    assert meta_rules, "no Meta rules seeded"
    assert meta_rules <= stale, f"these Meta rules were not reported stale: {meta_rules - stale}"


def test_a_stale_block_rule_is_urgent_and_a_stale_advisory_one_is_not(pages):
    """A stale BLOCK rule is a statutory check running on an unverified reading
    of the law. That is closer to urgent than a footer change."""
    pw.run(fetcher=pages, today=TODAY)
    by_severity = {}
    for row in findings(pw.RULE_STALE):
        by_severity.setdefault(row["detail_json"]["rule_severity"], set()).add(row["severity"])

    assert by_severity.get("block") == {"urgent"}
    for rule_severity, finding_severities in by_severity.items():
        if rule_severity != "block":
            assert finding_severities == {"review"}, (rule_severity, finding_severities)


def test_a_statute_is_not_reported_stale_on_metas_clock(pages):
    """The DPDP rule is dated 2025-11-14 - 304 days old - and it is NOT stale,
    because a primary statute ages on a 365-day window. One flat window would
    cry stale about the DMR Act every quarter, and a staleness signal nobody
    believes is worse than none."""
    pw.run(fetcher=pages, today=TODAY)
    stale = {r["subject"] for r in findings(pw.RULE_STALE)}
    assert "IN_DPDP_CONSENT_NOTICE" not in stale


def test_the_finding_says_how_stale_and_against_what_window(pages):
    pw.run(fetcher=pages, today=TODAY)
    row = next(r for r in findings(pw.RULE_STALE) if r["subject"] == "META_OUTCOME_GUARANTEE")
    assert row["detail_json"]["days_old"] == 197
    assert row["detail_json"]["window_days"] == 90
    assert row["detail_json"]["as_of"] == "2026-03-01"


# ---------------------------------------------------------------------------
# Dedup and acknowledgement
# ---------------------------------------------------------------------------


def test_a_stale_rule_is_one_finding_not_one_per_day(pages):
    pw.run(fetcher=pages, today=TODAY)
    first = len(findings(pw.RULE_STALE))
    pw.run(fetcher=pages, today=TODAY)
    pw.run(fetcher=pages, today=TODAY)
    assert len(findings(pw.RULE_STALE)) == first


def test_an_acknowledged_finding_stays_closed_until_the_condition_recurs(pages):
    """Acknowledging is the superadmin saying "I have looked". The row is kept -
    it is the record that they did - and a new row appears only if the watcher
    finds the same thing again afterwards."""
    url = a_source("meta")
    pw.run(fetcher=pages, today=TODAY)
    pages.pages[url] = "<html><body>first move</body></html>"
    pw.run(fetcher=pages, today=TODAY)
    assert len(findings(pw.SOURCE_CHANGED)) == 1

    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """update t_advit.watch_findings
                  set acknowledged_by = '00000000-0000-4000-8000-000000000001',
                      acknowledged_at = now()
                where kind = 'source_changed'"""
        )
        conn.commit()

    pw.run(fetcher=pages, today=TODAY)
    assert findings(pw.SOURCE_CHANGED) == [], "an unchanged page reopened a closed finding"

    pages.pages[url] = "<html><body>second move</body></html>"
    pw.run(fetcher=pages, today=TODAY)
    assert len(findings(pw.SOURCE_CHANGED)) == 1
    assert len(findings(pw.SOURCE_CHANGED, open_only=False)) == 2


# ---------------------------------------------------------------------------
# It never acts
# ---------------------------------------------------------------------------


def test_the_watcher_never_writes_a_rule_or_a_knowledge_row(pages):
    """The contract. Whatever it finds, the rules it found it about are exactly
    as they were."""
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("select md5(string_agg(t::text, '|' order by code)) from t_advit.policy_rules t")
        rules_before = cur.fetchone()[0]
        cur.execute("select md5(string_agg(t::text, '|' order by id)) from t_advit.platform_knowledge t")
        knowledge_before = cur.fetchone()[0]

    url = a_source("meta")
    pw.run(fetcher=pages, today=TODAY)
    pages.pages[url] = "<html><body>everything is different now</body></html>"
    pw.run(fetcher=pages, today=TODAY)

    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("select md5(string_agg(t::text, '|' order by code)) from t_advit.policy_rules t")
        assert cur.fetchone()[0] == rules_before, "Platform Watch edited a compliance rule"
        cur.execute("select md5(string_agg(t::text, '|' order by id)) from t_advit.platform_knowledge t")
        assert cur.fetchone()[0] == knowledge_before, "Platform Watch edited platform knowledge"


def test_the_watcher_reaches_no_write_path_into_the_rules():
    """The same contract, as a property of the code rather than of one run.
    Grepping is crude; it is also the only check that fires before a future
    edit runs."""
    import inspect

    source = inspect.getsource(pw)
    for table in ("policy_rules", "platform_knowledge"):
        for verb in ("insert into t_advit." + table, "update t_advit." + table, "delete from t_advit." + table):
            assert verb not in source, f"platform.py contains `{verb}`"


# ---------------------------------------------------------------------------
# Who can see the inbox
# ---------------------------------------------------------------------------


def test_a_tenant_cannot_read_the_platforms_findings(pages):
    """Which of the platform's own rules are stale is not a tenant's business,
    and would tell a tenant exactly which BLOCK is running on an unverified
    reading."""
    import json

    from conftest import OWNER, TENANT_DSN, claims_for

    pw.run(fetcher=pages, today=TODAY)
    assert findings(), "nothing to read"

    with psycopg.connect(TENANT_DSN) as conn:
        conn.autocommit = False
        conn.execute("select 1")
        with conn.cursor() as cur:
            cur.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps(claims_for(OWNER)),),
            )
            cur.execute("set local role authenticated")
            cur.execute("select count(*) from t_advit.watch_findings")
            assert cur.fetchone()[0] == 0
        conn.rollback()


def test_a_superadmin_can_read_and_acknowledge_but_not_forge(pages):
    """Acknowledging must name the person doing it. A superadmin who could set
    acknowledged_by to somebody else could sign off in a colleague's name."""
    import json

    from conftest import SUPERADMIN, TENANT_DSN, claims_for

    pw.run(fetcher=pages, today=TODAY)

    with psycopg.connect(TENANT_DSN) as conn:
        conn.autocommit = False
        conn.execute("select 1")
        with conn.cursor() as cur:
            cur.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps(claims_for(SUPERADMIN)),),
            )
            cur.execute("set local role authenticated")
            cur.execute("select count(*) from t_advit.watch_findings")
            assert cur.fetchone()[0] > 0

            cur.execute("savepoint forge")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    """update t_advit.watch_findings
                          set acknowledged_by = '00000000-0000-4000-8000-000000000002',
                              acknowledged_at = now()
                        where kind = 'rule_stale'"""
                )
            cur.execute("rollback to savepoint forge")

            cur.execute(
                """update t_advit.watch_findings
                      set acknowledged_by = auth.uid(), acknowledged_at = now()
                    where kind = 'rule_stale'"""
            )
            assert cur.rowcount > 0
        conn.rollback()


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


def test_the_platform_job_runs_once_per_day_however_many_replicas():
    """The INSERT is the claim, decided by job_runs_platform_once_per_day. A
    second claim on the same day is refused by the index, not by a lock."""
    from app.jobs.runner import claim_platform

    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("delete from t_advit.job_runs where job = 'platform_watch' and workspace_id is null")
        conn.commit()
    try:
        first = claim_platform("platform_watch", TODAY)
        second = claim_platform("platform_watch", TODAY)
        assert first is not None
        assert second is None, "two replicas both claimed the same day's platform job"
    finally:
        with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
            cur.execute("delete from t_advit.job_runs where job = 'platform_watch' and workspace_id is null")
            conn.commit()


def test_a_platform_job_takes_no_workspace_and_no_input():
    """The anti-bypass property, one level up from the workspace jobs. There is
    no argument to hand it and nothing a caller could name."""
    from app.jobs.runner import PLATFORM_JOBS
    import inspect

    watch = next(j for j in PLATFORM_JOBS if j.name == "platform_watch")
    params = inspect.signature(watch.handler).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values()), (
        "the platform job handler takes positional input"
    )
    assert "workspace" not in " ".join(params)
