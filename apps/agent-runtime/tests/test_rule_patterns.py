"""A rule that cannot run is not a rule, and must not report the bundle clean.

`_match_regex` swallowed `re.error` and returned no findings, and `_apply`
wrapped that as ``_RuleOutput(..., evaluated=True)``. So a BLOCK rule whose
pattern did not compile reported the creative clean **and** marked its stage
checked — and the gate then certified something nobody had adjudicated. The
comment beside the swallow said "A malformed rule must not silently pass the
bundle", which is what it did.

That exact fix had already been made two lines away, for state checks: *"the
caller used to hard-code `evaluated=True`, which turned 'we could not look' into
'we looked and it was fine'"*. It did not reach the regex branch.

The trigger was `\\y`. Postgres ARE spells a word boundary `\\y` (either end),
`\\m` (start) and `\\M` (end); the translator handled the two directional forms
and not the ordinary one. So the natural way to write a boundary was the one
that silently disabled the rule — and now that industry packs are data, the
person writing a rule is not necessarily someone who would know that.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from app.agents.compliance import (
    ComplianceGate,
    CreativeBundle,
    PolicyRule,
    RuleType,
    Severity,
    Verdict,
)
from app.policy.rules import MalformedRule, PolicyRuleLoader

COPY = "Piles ka permanent ilaj guaranteed"


def regex_rule(pattern: str | None, *, stage: int = 2) -> PolicyRule:
    return PolicyRule(
        code="PROBE",
        jurisdiction="in",
        instrument="probe",
        gate_stage=stage,
        rule_type=RuleType.REGEX,
        title="probe",
        severity=Severity.BLOCK,
        explanation="a BLOCK rule",
        source_url="https://example.test",
        as_of=date(2026, 9, 11),
        pattern=pattern,
        remedy_template="fix it",
    )


def check(pattern: str | None):
    return ComplianceGate([regex_rule(pattern)]).check(CreativeBundle(primary_text=COPY))


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "are,python",
    [
        (r"\yguaranteed\y", r"\bguaranteed\b"),   # the one that was missing
        (r"\Yfoo\Y", r"\Bfoo\B"),                 # and its negation
        (r"\mfoo\M", r"\bfoo\b"),                 # the directional forms, already handled
        ("[[:space:]]+", r"\s+"),
        ("[[:digit:]]{2}", r"\d{2}"),
        ("[[:alpha:]]+", "[a-zA-Z]+"),
    ],
)
def test_postgres_are_constructs_translate(are, python):
    assert PolicyRuleLoader._translate_pattern(are, code="PROBE") == python


def test_a_translated_are_boundary_actually_matches():
    """Translating is only half of it. A translation that produced a valid
    pattern matching nothing would pass the test above and disable the rule
    just as thoroughly."""
    translated = PolicyRuleLoader._translate_pattern(r"\yguaranteed\y", code="PROBE")
    assert re.search(translated, COPY, re.IGNORECASE)
    assert check(translated).verdict is Verdict.BLOCK


# ---------------------------------------------------------------------------
# Load time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [r"\yfoo(\y", "(unclosed", "a{2,1}", "[z-a]"])
def test_a_pattern_that_cannot_compile_is_refused_at_load(bad):
    """Once, with the rule code in the message, rather than silently on every
    request. A pattern that cannot compile is broken for every bundle."""
    with pytest.raises(MalformedRule) as exc:
        PolicyRuleLoader._translate_pattern(bad, code="IN_PROBE_BAD")
    assert "IN_PROBE_BAD" in str(exc.value)


def test_the_refusal_shows_both_the_stored_and_the_translated_pattern():
    """The author wrote the stored one and has to debug the translated one.
    Showing only the translated form makes them hunt for a string that appears
    nowhere in their migration."""
    with pytest.raises(MalformedRule) as exc:
        PolicyRuleLoader._translate_pattern(r"\yfoo(\y", code="IN_PROBE_BAD")
    message = str(exc.value)
    assert r"\yfoo(\y" in message
    assert r"\bfoo(\b" in message


def test_every_seeded_pattern_compiles():
    """Over the real packs, so a rule added by seed is covered by this file
    without anyone remembering to add a case."""
    import os

    import psycopg
    from psycopg.rows import dict_row

    dsn = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
    try:
        conn = psycopg.connect(dsn, connect_timeout=3, row_factory=dict_row)
    except Exception:
        pytest.skip("local Postgres is not running")

    with conn, conn.cursor() as cur:
        cur.execute(
            "select code, pattern from t_advit.policy_rules where pattern is not null"
        )
        rows = cur.fetchall()

    assert rows, "no seeded regex rules; this test would pass vacuously"
    for row in rows:
        # Raises MalformedRule if it does not, naming the code.
        PolicyRuleLoader._translate_pattern(row["pattern"], code=row["code"])


# ---------------------------------------------------------------------------
# Match time
# ---------------------------------------------------------------------------


def test_a_rule_that_cannot_compile_leaves_its_stage_unevaluated():
    """The finding itself. It used to report the stage EVALUATED with no
    findings, so the gate certified a creative nobody had adjudicated."""
    result = check(r"\yguaranteed\y")

    assert result.findings == []
    assert 2 in result.stages_skipped
    assert 2 not in result.stages_evaluated
    assert result.verdict is not Verdict.PASS


def test_a_rule_with_no_pattern_at_all_is_unevaluated_rather_than_clean():
    """The database refuses to store one (policy_rules_has_matcher), so this is
    only reachable from a PolicyRule built in code - and "unreachable" is a
    property of today's callers, not of this function."""
    result = check(None)
    assert 2 in result.stages_skipped
    assert result.verdict is not Verdict.PASS


def test_a_working_rule_still_evaluates_and_blocks():
    """The counterpart. A gate that refuses to evaluate anything is not
    cautious, it is broken - and it would pass every test above."""
    result = check("guaranteed")
    assert result.verdict is Verdict.BLOCK
    assert [f.rule_code for f in result.findings] == ["PROBE"]
    assert 2 in result.stages_evaluated
    assert 2 not in result.stages_skipped


def test_a_working_rule_that_simply_does_not_match_is_still_evaluated():
    """"Ran and found nothing" must stay distinguishable from "could not run".
    Collapsing them in the other direction would make the gate refuse to
    certify anything clean."""
    result = ComplianceGate([regex_rule("kabhi-nahi-milega")]).check(
        CreativeBundle(primary_text=COPY)
    )
    assert result.findings == []
    assert 2 in result.stages_evaluated
    assert 2 not in result.stages_skipped
