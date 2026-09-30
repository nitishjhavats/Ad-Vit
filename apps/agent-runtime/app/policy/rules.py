"""Loads the compliance ruleset for one industry from ``t_advit.policy_rules``.

The gate itself (``app.agents.compliance``) is deliberately free of database
and network dependencies. This module is the only bridge, which keeps the
adjudication logic unit-testable against a fixed ruleset while production runs
against the seeded one.

Rules are cached per industry with a short TTL. Policy knowledge is the
one kind of state that must never go stale silently: PRD 14.7 requires a record
past its freshness window to be flagged rather than asserted, so the loader
surfaces the oldest ``as_of`` it saw alongside the rules.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

from app.agents.compliance import PolicyRule, RuleType, Severity
from app.db.pools import service_conn


class MalformedRule(ValueError):
    """A policy rule's pattern does not compile.

    Raised at load rather than swallowed at match, because a rule that cannot
    run is not a rule - and a compliance gate silently missing one of its BLOCK
    rules is the worst possible way to find out which.
    """


class UnknownIndustry(LookupError):
    """The requested industry key is not a row in ``t_advit.industries``.

    Raised rather than answered, because the honest answer would be the eight
    rules scoped to every pack - which is the Meta layer with the whole Indian
    statutory layer missing, and indistinguishable from a legitimately
    unregulated pack. A guard with nothing to compare against refuses.
    """

# A rule whose source has not been re-checked within its window is reported as
# stale. Platform Watch is what should keep it fresh (PRD 13.5); until that
# agent exists, the staleness is at least visible.
#
# The window differs by jurisdiction because the two bodies of rule move at
# completely different speeds. Meta's advertising policy changed materially in
# January and again in March 2026 (PRD 2.1); a primary statute does not. One
# flat window would either miss a Meta change for months or cry stale about the
# DMR Act every quarter - and a staleness signal nobody believes is worse than
# none.
FRESHNESS_WINDOWS: dict[str, timedelta] = {
    "meta": timedelta(days=90),
    "in": timedelta(days=365),
}
DEFAULT_FRESHNESS_WINDOW = timedelta(days=180)


def freshness_window(jurisdiction: str) -> timedelta:
    return FRESHNESS_WINDOWS.get(jurisdiction, DEFAULT_FRESHNESS_WINDOW)


@dataclass(frozen=True, slots=True)
class LoadedRuleset:
    rules: tuple[PolicyRule, ...]
    oldest_as_of: date | None
    stale_codes: tuple[str, ...]

    # Which pack judged the bundle, so a verdict can say so. `ayurveda@1` is
    # derived from t_advit.industries, replacing the per-workspace
    # industry_pack_id string that had already drifted from the column beside
    # it.
    industry_key: str = ""
    pack_id: str = ""
    industry_status: str = ""

    @property
    def has_stale_rules(self) -> bool:
        return bool(self.stale_codes)

    def __iter__(self) -> Iterator[PolicyRule]:
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self.rules)


class PolicyRuleLoader:
    """Global rule data on the service connection.

    ``t_advit.policy_rules`` is platform data, not tenant data - the statutory
    layer is the same for every workspace in an industry, and a ruleset that
    varied with who was asking would be a compliance gate a tenant could
    narrow.
    """

    def __init__(self, *, ttl_s: int = 300) -> None:
        self._ttl_s = ttl_s
        self._cache: dict[str, tuple[float, LoadedRuleset]] = {}

    def load(self, industry_key: str, *, refresh: bool = False) -> LoadedRuleset:
        """Raises UnknownIndustry if the key is not a seeded pack."""
        now = time.monotonic()
        cached = self._cache.get(industry_key)
        if cached and not refresh and now - cached[0] < self._ttl_s:
            return cached[1]

        ruleset = self._fetch(industry_key)
        self._cache[industry_key] = (now, ruleset)
        return ruleset

    def invalidate(self) -> None:
        self._cache.clear()

    def _fetch(self, industry_key: str) -> LoadedRuleset:
        with service_conn() as conn, conn.cursor() as cur:
            # Resolve the pack first. An unseeded key used to fall through to
            # `business_types = '{}'` and return only the rules scoped to every
            # pack - a typo, or a workspace pointing at a pack that had been
            # renamed, silently produced a ruleset with no Indian layer in it.
            cur.execute(
                """
                select key, pack_id, status::text as status
                  from t_advit.industries
                 where key = %s
                """,
                (industry_key,),
            )
            industry = cur.fetchone()
            if industry is None:
                raise UnknownIndustry(
                    f"{industry_key!r} is not a row in t_advit.industries; "
                    "refusing to serve a partial ruleset"
                )

            # Scope is declared on the rule and listed in the junction table.
            # An `exists` rather than a join, so a rule scoped to three
            # industries still returns exactly once.
            cur.execute(
                """
                select r.code, r.jurisdiction, r.instrument, r.gate_stage,
                       r.rule_type::text as rule_type,
                       r.severity::text  as severity,
                       r.scope::text     as scope,
                       r.title, r.explanation, r.remedy_template,
                       r.source_url, r.as_of, r.pattern, r.terms,
                       r.required_facts, r.state_handler,
                       coalesce(
                         array(
                           select i.industry_key
                             from t_advit.policy_rule_industries i
                            where i.rule_code = r.code
                            order by i.industry_key
                         ),
                         '{}'::text[]
                       ) as industry_keys
                  from t_advit.policy_rules r
                 where r.is_active
                   and (
                        r.scope = 'all_industries'
                     or exists (
                          select 1
                            from t_advit.policy_rule_industries i
                           where i.rule_code = r.code
                             and i.industry_key = %s
                        )
                   )
                 order by r.gate_stage, r.code
                """,
                (industry_key,),
            )
            rows = cur.fetchall()

        rules: list[PolicyRule] = []
        stale: list[str] = []
        oldest: date | None = None
        today = date.today()

        for row in rows:
            as_of: date = row["as_of"]
            if oldest is None or as_of < oldest:
                oldest = as_of
            if as_of < today - freshness_window(row["jurisdiction"]):
                stale.append(row["code"])

            rules.append(
                PolicyRule(
                    code=row["code"],
                    jurisdiction=row["jurisdiction"],
                    instrument=row["instrument"],
                    gate_stage=int(row["gate_stage"]),
                    rule_type=RuleType(row["rule_type"]),
                    title=row["title"],
                    severity=Severity(row["severity"]),
                    explanation=row["explanation"],
                    source_url=row["source_url"],
                    as_of=as_of,
                    pattern=self._translate_pattern(row["pattern"], code=row["code"]),
                    terms=tuple(row["terms"] or ()),
                    remedy_template=row["remedy_template"],
                    # The field keeps its name for now: with scope resolved in
                    # SQL above, `applies_to` is a second, redundant filter and
                    # an empty tuple still means "every pack". Renaming it to
                    # industry_keys is a mechanical follow-up in
                    # app/agents/compliance.py, not a behaviour change.
                    business_types=tuple(row["industry_keys"] or ()),
                    required_facts=tuple(row["required_facts"] or ()),
                    state_handler=row["state_handler"],
                )
            )

        return LoadedRuleset(
            rules=tuple(rules),
            oldest_as_of=oldest,
            stale_codes=tuple(stale),
            industry_key=industry["key"],
            pack_id=industry["pack_id"],
            industry_status=industry["status"],
        )

    @staticmethod
    def _translate_pattern(pattern: str | None, *, code: str = "?") -> str | None:
        """Postgres ARE to Python ``re``.

        The seeded patterns are written for Postgres so they can be exercised by
        SQL tests directly, and a few constructs differ.

        ``\\y`` and ``\\Y`` are the ones that were missing, and they are the
        ordinary ARE spellings - ``\\y`` is a word boundary at either end and
        ``\\M`` / ``\\m`` are the directional forms. So the natural way to write
        a boundary was the one that did not survive translation, and now that
        industry packs are data, the person writing a rule is not necessarily
        someone who would know that.

        What happened to an untranslated one was worse than a crash:
        ``re.compile`` raised, ``_match_regex`` swallowed it and returned no
        findings, and the caller marked the stage EVALUATED - so a BLOCK rule
        that could not run reported the bundle clean. Hence the compile below.
        """
        if pattern is None:
            return None
        translated = (
            pattern.replace(r"\y", r"\b")   # ARE: boundary at either end
            .replace(r"\Y", r"\B")          # ARE: NOT a boundary
            .replace(r"\m", r"\b")          # ARE: boundary at the start
            .replace(r"\M", r"\b")          # ARE: boundary at the end
            .replace("[[:space:]]", r"\s")
            .replace("[[:digit:]]", r"\d")
            .replace("[[:alpha:]]", "[a-zA-Z]")
            .replace("[[:alnum:]]", r"\w")
        )

        # Compiled HERE, at load, rather than per bundle.
        #
        # A pattern that cannot compile is broken for every bundle, so finding
        # out once - with the rule code in the message - beats finding out
        # silently on every request. This is the same refusal as UnknownIndustry
        # above: a ruleset that cannot be served in full is not served at all,
        # because the alternative is a compliance gate that is quietly missing
        # one of its rules.
        try:
            re.compile(translated, re.IGNORECASE)
        except re.error as exc:
            raise MalformedRule(
                f"policy rule {code} has a pattern that does not compile after "
                f"translation from Postgres ARE: {exc}\n"
                f"  stored:     {pattern}\n"
                f"  translated: {translated}"
            ) from exc

        return translated
