"""Platform Watch: go to the source, and say whether it moved.

Three checks, run once a day for the platform as a whole rather than per
workspace, because the rules belong to no tenant:

  1. **Source re-check.** Every distinct ``source_url`` a compliance rule or a
     platform-knowledge row cites is fetched and its text hashed. A hash that
     differs from the last one on file is a finding. This is the "authentic"
     half of the brief: it does not ask a search engine what changed, it reads
     the page the rule was written from.

  2. **Freshness.** A rule whose ``as_of`` is older than its jurisdiction's
     window is a finding. ``app/policy/rules.py`` has always computed this and
     returned it alongside the ruleset, where nobody looked; this writes it
     down. Today every Meta rule in the seed is past its window.

  3. **Reachability.** A source that cannot be fetched is a finding too. A rule
     whose source has gone is a rule nobody can re-verify, which is not the same
     as a rule that is wrong, and it should not look like one.

What comes out is a row in ``t_advit.watch_findings`` for a superadmin. Nothing
here writes to ``policy_rules`` or ``platform_knowledge``, and that is the
design rather than a gap: the rules carry statutory weight, and the thing that
changes them has to be a person who read the source.

The fetcher is injected so the tests never touch the network and the job can be
run in a build with no outbound access at all - it will report every source as
unreachable, which is the truth of that build.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from app.db.pools import service_conn
from app.policy.rules import freshness_window

log = logging.getLogger(__name__)

SOURCE_CHANGED = "source_changed"
RULE_STALE = "rule_stale"
KNOWLEDGE_STALE = "knowledge_stale"
FETCH_FAILED = "fetch_failed"


@dataclass(frozen=True, slots=True)
class Fetched:
    """What a fetch produced. `text` is None when nothing usable came back."""

    status: int | None
    text: str | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.text is not None


Fetcher = Callable[[str], Fetched]


def http_fetch(url: str, *, timeout_s: float = 20.0) -> Fetched:
    """The production fetcher. Follows redirects, because Meta's policy URLs
    redirect to transparency.meta.com, and a hash of a 301 page is a hash of
    nothing."""
    import httpx

    try:
        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=timeout_s,
            headers={
                # A named agent, so an operator reading their access log can
                # see who is checking and why.
                "User-Agent": "ad-vit PlatformWatch/1.0 (+https://broadmate.org)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
    except httpx.HTTPError as exc:
        return Fetched(status=None, text=None, error=f"{type(exc).__name__}: {exc}")

    if response.status_code != 200:
        return Fetched(status=response.status_code, text=None, error=f"HTTP {response.status_code}")

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return Fetched(
            status=response.status_code, text=None,
            error=f"not a text response: {content_type or 'no content-type'}",
        )
    return Fetched(status=response.status_code, text=response.text)


_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def content_hash(html: str) -> tuple[str, int]:
    """A hash of what a reader would read.

    Scripts, styles and tags are stripped and whitespace collapsed before
    hashing, so a rebuilt bundle or a reflowed template does not read as a
    policy change. Crude, and stated as crude: a page carrying a live timestamp
    in its text will still differ every day. The finding side deduplicates, so
    that produces one open row rather than one per day - and a finding that
    returns the day after it was acknowledged is itself the information that the
    page is dynamic.
    """
    text = _SCRIPT_STYLE.sub(" ", html)
    text = _TAGS.sub(" ", text)
    text = _SPACE.sub(" ", text).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), len(text)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

_RECORD = """
insert into t_advit.watch_findings (kind, subject, source_url, severity, detail_json)
values (%(kind)s, %(subject)s, %(source_url)s, %(severity)s, %(detail)s::jsonb)
on conflict (kind, subject) where acknowledged_at is null
do update set
  -- The finding is the same finding; the detail is today's.
  detail_json = excluded.detail_json,
  detected_at = t_advit.watch_findings.detected_at
returning id::text, (xmax = 0) as inserted
"""


def _record(cur, *, kind: str, subject: str, severity: str,
            source_url: str | None = None, **detail: Any) -> bool:
    cur.execute(
        _RECORD,
        {
            "kind": kind,
            "subject": subject,
            "source_url": source_url,
            "severity": severity,
            "detail": json.dumps(detail, default=str),
        },
    )
    return bool(cur.fetchone()["inserted"])


# ---------------------------------------------------------------------------
# 1. Sources
# ---------------------------------------------------------------------------

_SOURCES = """
select distinct source_url as url
  from (
    select source_url from t_advit.policy_rules
     where source_url is not null and source_url <> ''
    union
    select source_url from t_advit.platform_knowledge
     where source_url is not null and source_url <> ''
       and status = 'active'
  ) s
 order by 1
"""

_CITED_BY = """
select coalesce(
  (select array_agg(code order by code) from t_advit.policy_rules where source_url = %(url)s),
  '{}'::text[]
) as rules,
coalesce(
  (select count(*) from t_advit.platform_knowledge
    where source_url = %(url)s and status = 'active'),
  0
) as knowledge_rows
"""


def check_sources(cur, fetcher: Fetcher, *, now: datetime) -> dict[str, int]:
    counts = {"checked": 0, "unchanged": 0, "changed": 0, "first_seen": 0, "failed": 0}

    cur.execute(_SOURCES)
    urls = [row["url"] for row in cur.fetchall()]

    for url in urls:
        counts["checked"] += 1
        cur.execute("select content_hash from t_advit.watched_sources where url = %s", (url,))
        previous = cur.fetchone()
        previous_hash = previous["content_hash"] if previous else None

        fetched = fetcher(url)

        if not fetched.ok:
            counts["failed"] += 1
            cur.execute(
                """
                insert into t_advit.watched_sources (url, last_status, last_error, last_fetched_at)
                values (%s, %s, %s, %s)
                on conflict (url) do update set
                  last_status = excluded.last_status,
                  last_error = excluded.last_error,
                  last_fetched_at = excluded.last_fetched_at
                """,
                (url, fetched.status, fetched.error, now),
            )
            cur.execute(_CITED_BY, {"url": url})
            cited = cur.fetchone()
            _record(
                cur, kind=FETCH_FAILED, subject=url, source_url=url,
                # `review`, not `urgent`. An unreachable source is a rule nobody
                # can re-verify, which is not the same as a rule that is wrong.
                severity="review",
                status=fetched.status, error=fetched.error,
                cited_by_rules=list(cited["rules"]), knowledge_rows=cited["knowledge_rows"],
            )
            continue

        digest, size = content_hash(fetched.text)
        changed = previous_hash is not None and digest != previous_hash

        cur.execute(
            """
            insert into t_advit.watched_sources
              (url, content_hash, content_bytes, last_status, last_error,
               last_fetched_at, last_changed_at)
            values (%s, %s, %s, %s, null, %s, %s)
            on conflict (url) do update set
              content_hash    = excluded.content_hash,
              content_bytes   = excluded.content_bytes,
              last_status     = excluded.last_status,
              last_error      = null,
              last_fetched_at = excluded.last_fetched_at,
              -- Only moves when the hash does. The first fetch is a baseline,
              -- not a change: there was nothing for it to have changed FROM.
              last_changed_at = case
                when t_advit.watched_sources.content_hash is distinct from excluded.content_hash
                 and t_advit.watched_sources.content_hash is not null
                then excluded.last_fetched_at
                else t_advit.watched_sources.last_changed_at
              end
            """,
            (url, digest, size, fetched.status, now, now if previous_hash is None else None),
        )

        if previous_hash is None:
            counts["first_seen"] += 1
        elif changed:
            counts["changed"] += 1
            cur.execute(_CITED_BY, {"url": url})
            cited = cur.fetchone()
            _record(
                cur, kind=SOURCE_CHANGED, subject=url, source_url=url,
                # A BLOCK rule's source moving is the most consequential thing
                # this job can notice, and it is still `review`: the page may
                # have changed a footer. `urgent` is reserved for a human's
                # judgement, not a hash comparison's.
                severity="review",
                previous_hash=previous_hash, current_hash=digest, text_length=size,
                cited_by_rules=list(cited["rules"]), knowledge_rows=cited["knowledge_rows"],
            )
        else:
            counts["unchanged"] += 1

    return counts


# ---------------------------------------------------------------------------
# 2. Freshness
# ---------------------------------------------------------------------------


def check_freshness(cur, *, today: date) -> dict[str, int]:
    counts = {"rules_checked": 0, "rules_stale": 0, "knowledge_checked": 0, "knowledge_stale": 0}

    cur.execute(
        "select code, jurisdiction, as_of, source_url, severity::text as severity "
        "from t_advit.policy_rules order by code"
    )
    for row in cur.fetchall():
        counts["rules_checked"] += 1
        window = freshness_window(row["jurisdiction"])
        age = today - row["as_of"]
        if age <= window:
            continue
        counts["rules_stale"] += 1
        _record(
            cur, kind=RULE_STALE, subject=row["code"], source_url=row["source_url"],
            # A stale BLOCK rule is a statutory check running on an unverified
            # reading of the law. That is closer to urgent than a footer change.
            severity="urgent" if row["severity"] == "block" else "review",
            jurisdiction=row["jurisdiction"], as_of=row["as_of"],
            days_old=age.days, window_days=window.days,
            rule_severity=row["severity"],
        )

    cur.execute(
        "select id::text as id, topic, as_of, source_url from t_advit.platform_knowledge "
        "where status = 'active' order by as_of"
    )
    for row in cur.fetchall():
        counts["knowledge_checked"] += 1
        # Platform knowledge is Meta-shaped - product and policy changes - so it
        # ages on Meta's clock.
        window = freshness_window("meta")
        age = today - row["as_of"]
        if age <= window:
            continue
        counts["knowledge_stale"] += 1
        _record(
            cur, kind=KNOWLEDGE_STALE, subject=row["id"], source_url=row["source_url"],
            severity="review",
            topic=row["topic"], as_of=row["as_of"], days_old=age.days, window_days=window.days,
        )

    return counts


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def run(*, fetcher: Fetcher = http_fetch, today: date | None = None) -> dict[str, Any]:
    """One Platform Watch pass. Returns what it did, for the job_runs record."""
    now = datetime.now(timezone.utc)
    when = today or now.date()

    with service_conn() as conn, conn.cursor() as cur:
        sources = check_sources(cur, fetcher, now=now)
        freshness = check_freshness(cur, today=when)

        cur.execute(
            "select count(*) as n from t_advit.watch_findings where acknowledged_at is null"
        )
        open_findings = cur.fetchone()["n"]

        # The same fact in the audit trail, at platform scope, as automation.
        # This is the superadmin notification this repository can honestly make
        # today: a row in the inbox and a line in the trail. Email and push are
        # infrastructure that does not exist here, and pretending otherwise
        # would be a notification nobody receives.
        cur.execute(
            """
            select core.log_audit('platform', 'platform_watch.completed',
                                  p_actor_type => 'automation',
                                  p_payload    => %s::jsonb)
            """,
            (json.dumps({**sources, **freshness, "open_findings": open_findings}),),
        )
        conn.commit()

    summary = {**sources, **freshness, "open_findings": open_findings}
    log.info("platform watch: %s", ", ".join(f"{k}={v}" for k, v in summary.items()))
    return summary
