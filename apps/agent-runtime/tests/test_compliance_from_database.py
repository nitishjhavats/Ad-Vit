"""The compliance gate driven by the seeded ruleset.

The unit suite proves the gate's logic against a fixed ruleset. This proves the
ruleset that actually ships behaves the same way once loaded out of Postgres -
including the Postgres-to-Python regex translation, which is the one place the
two dialects could silently diverge and let a violation through.

This is PRD Appendix D.3 end to end, against the real Ayurveda pack.
"""

from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from app.agents.compliance import (
    ComplianceGate,
    CreativeBundle,
    LicencePosture,
    Severity,
    Verdict,
)
from app.policy.rules import PolicyRuleLoader

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
)


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason="local Supabase Postgres is not running"
)


@pytest.fixture(scope="module")
def ayurveda_ruleset():
    return PolicyRuleLoader().load("ayurveda")


@pytest.fixture(scope="module")
def general_ruleset():
    return PolicyRuleLoader().load("general_d2c")


@pytest.fixture(scope="module")
def gate(ayurveda_ruleset) -> ComplianceGate:
    return ComplianceGate(list(ayurveda_ruleset))


def bundle(**overrides) -> CreativeBundle:
    """The licence facts travel as a resolved catalogue posture. See the same
    helper in test_compliance_gate.py."""
    base = dict(
        primary_text="An Ayurvedic formulation prepared in the traditional manner.",
        headline="Traditional Ayurvedic care",
        cta_type="WHATSAPP",
        destination_url="https://example.in/product",
        business_type="ayurveda",
        has_media=False,
    )
    base["licence_posture"] = overrides.pop(
        "licence_posture",
        LicencePosture(
            source="catalogue",
            sku=overrides.pop("sku", "PC-001"),
            ayush_licence_no=overrides.pop("ayush_licence_no", "UP-AYUR-12345"),
            classification=overrides.pop("product_classification", "ayurvedic_drug"),
        ),
    )
    base.update(overrides)
    return CreativeBundle(**base)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_ruleset_loads_both_layers(ayurveda_ruleset):
    layers = {r.jurisdiction for r in ayurveda_ruleset}
    assert layers == {"meta", "in"}
    assert len(ayurveda_ruleset) >= 10


def test_pack_scoping_is_honoured_by_the_query(ayurveda_ruleset, general_ruleset):
    """Schedule J is scoped to the Ayurveda pack. A General D2C workspace must
    not load it at all - not merely skip it at evaluation time."""
    ayur_codes = {r.code for r in ayurveda_ruleset}
    general_codes = {r.code for r in general_ruleset}

    assert "IN_DMRA_SCHEDULE_J" in ayur_codes
    assert "IN_DMRA_SCHEDULE_J" not in general_codes
    # Universal Meta rules apply to both.
    assert "META_PA_SECOND_PERSON_HEALTH" in general_codes


def test_every_loaded_rule_carries_provenance(ayurveda_ruleset):
    for rule in ayurveda_ruleset:
        assert rule.source_url.startswith("https://"), rule.code
        assert isinstance(rule.as_of, date), rule.code


def test_loader_reports_staleness_rather_than_hiding_it(ayurveda_ruleset):
    """A policy record past its freshness window must be flagged, not asserted
    (PRD 14.7).

    This is a live signal, not a hypothetical: the Meta policy rules carry
    as_of 2026-03-01, which is beyond the 90-day window Meta policy gets. The
    agent must therefore say it needs to verify the current rule rather than
    assert a six-month-old one.
    """
    assert ayurveda_ruleset.oldest_as_of is not None
    assert ayurveda_ruleset.has_stale_rules is True
    assert any(code.startswith("META_") for code in ayurveda_ruleset.stale_codes)


def test_indian_statute_gets_a_longer_freshness_window(ayurveda_ruleset):
    """Meta's policy changed materially twice in 2026; the DMR Act did not.
    A single flat window would either miss a Meta change or cry stale about
    primary legislation every quarter."""
    from app.policy.rules import freshness_window

    assert freshness_window("meta") < freshness_window("in")

    schedule_j = next(r for r in ayurveda_ruleset if r.code == "IN_DMRA_SCHEDULE_J")
    assert schedule_j.code not in ayurveda_ruleset.stale_codes, (
        "a statute dated within the year must not be reported stale"
    )


def test_loader_caches_and_can_be_invalidated():
    loader = PolicyRuleLoader(ttl_s=300)
    first = loader.load("ayurveda")
    assert loader.load("ayurveda") is first        # served from cache
    loader.invalidate()
    assert loader.load("ayurveda") is not first    # refetched


# ---------------------------------------------------------------------------
# Appendix D.3, against the shipped ruleset
# ---------------------------------------------------------------------------


def test_schedule_j_claim_is_blocked_by_the_seeded_rule(gate):
    result = gate.check(bundle(primary_text="Ye churna piles ko jad se khatam karta hai."))

    assert result.verdict is Verdict.BLOCK
    finding = next(f for f in result.findings if f.rule_code == "IN_DMRA_SCHEDULE_J")
    assert finding.instrument == "dmr_act_schedule_j"
    assert finding.offending_span == "piles"
    assert finding.source_url.startswith("https://www.indiacode.nic.in")
    assert finding.as_of == date(2026, 8, 1)
    assert finding.suggested_rewrite
    assert finding.needs_legal_verification is True


def test_bawasir_is_caught_as_well_as_piles(gate):
    """The account advertises in Hinglish, so the English term alone would miss
    most of its real copy."""
    result = gate.check(bundle(primary_text="Bawasir ke liye ayurvedic upchar."))
    assert any(f.rule_code == "IN_DMRA_SCHEDULE_J" for f in result.findings)


def test_hinglish_timeline_claim_survives_regex_translation(gate):
    """The seeded pattern is written in Postgres ARE. If the translation to
    Python dropped a word boundary or a POSIX class, this claim would pass."""
    result = gate.check(bundle(primary_text="Sirf 7 din mein result dekhein."))
    assert any(f.rule_code == "META_OUTCOME_TIMELINE" for f in result.findings)


def test_english_outcome_first_timeline_claim_is_caught(gate):
    result = gate.check(bundle(primary_text="Results in 15 days, naturally."))
    assert any(f.rule_code == "META_OUTCOME_TIMELINE" for f in result.findings)


def test_second_person_health_framing_is_caught(gate):
    result = gate.check(bundle(primary_text="Kya aap piles ki problem se pareshan hain?"))
    codes = {f.rule_code for f in result.findings}
    assert "META_PA_SECOND_PERSON_HEALTH" in codes


def test_layers_are_adjudicated_independently(gate):
    """Third-person, no timeline, no personal attribute: clean under Meta
    policy, still illegal under the DMR Act."""
    result = gate.check(
        bundle(primary_text="A traditional preparation used in the management of piles.")
    )
    assert result.layer_verdict("meta") is Verdict.PASS
    assert result.layer_verdict("india") is Verdict.BLOCK
    assert result.verdict is Verdict.BLOCK


def test_compliant_ayurveda_copy_reports_not_evaluated_not_pass(gate):
    """Stages 7 and 8 are unimplemented, so the gate cannot certify clean copy
    (PRD 4.5). It says so rather than implying approval."""
    result = gate.check(bundle())
    assert [f for f in result.findings if f.severity is Severity.BLOCK] == []
    assert result.verdict is Verdict.NOT_EVALUATED
    assert 7 in result.stages_skipped and 8 in result.stages_skipped


def test_logistics_copy_is_not_falsely_blocked(gate):
    """A false-block rate that is too high teaches owners to override the gate,
    which is as much a defect as a miss (PRD 13.4)."""
    result = gate.check(
        bundle(primary_text="Delivered in 3 days across India. 30 day return window.")
    )
    assert not any(f.rule_code == "META_OUTCOME_TIMELINE" for f in result.findings)


def test_word_boundaries_hold_through_translation(gate):
    result = gate.check(bundle(primary_text="Our team compiles customer feedback weekly."))
    assert not any(f.rule_code == "IN_DMRA_SCHEDULE_J" for f in result.findings)


def test_general_pack_does_not_inherit_schedule_j(general_ruleset):
    general_gate = ComplianceGate(list(general_ruleset))
    result = general_gate.check(
        CreativeBundle(
            primary_text="We cleared our piles of surplus stock.",
            business_type="general_d2c",
        )
    )
    assert not any(f.rule_code == "IN_DMRA_SCHEDULE_J" for f in result.findings)


def test_missing_licence_blocks_an_ayurveda_bundle(gate):
    result = gate.check(bundle(ayush_licence_no=None))
    finding = next(f for f in result.findings if f.rule_code == "IN_AYUSH_LICENCE_ON_FILE")
    assert "AYUSH licence number" in finding.offending_span


def test_seeded_workspace_placeholder_classification_is_flagged(gate):
    """The seeded catalogue records classification 'unverified' precisely so
    this fires: misclassification is the most common root cause of an
    unfixable rejection (PRD 13.3)."""
    result = gate.check(bundle(product_classification="unverified"))
    finding = next(f for f in result.findings if f.rule_code == "IN_AYUSH_LICENCE_ON_FILE")
    assert "product classification" in finding.offending_span


def test_stage_coverage_sets_are_disjoint(gate):
    """A stage reported as both evaluated and skipped reads as a contradiction.
    Stage 4 is genuinely half-covered - regex outcome rules always run, the
    classical-text judgement needs a model - so it is reported as partial."""
    result = gate.check(bundle(primary_text="Results in 15 days.", has_media=True))

    evaluated = set(result.stages_evaluated)
    partial = set(result.stages_partial)
    skipped = set(result.stages_skipped)

    assert evaluated & partial == set()
    assert evaluated & skipped == set()
    assert partial & skipped == set()

    # Stage 4 fired a regex rule but could not run its llm_judge rule.
    assert 4 in partial
    # Stages 7 and 8 are wholly unimplemented.
    assert {7, 8} <= skipped
    # Nothing is silently lost.
    assert result.stages_not_fully_checked == sorted(partial | skipped)
