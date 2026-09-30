"""Compliance Guard acceptance tests.

The core of these is PRD Appendix D.3 - "Ye naya creative chala do", where the
copy carries a Schedule J claim. Each criterion there is binary and testable,
and each is asserted below.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.compliance import (
    ComplianceGate,
    CreativeBundle,
    Judge,
    LicencePosture,
    PolicyRule,
    RuleType,
    Severity,
    Verdict,
)

# ---------------------------------------------------------------------------
# A minimal ruleset mirroring supabase/seeds/02_ayurveda_pack.sql
# ---------------------------------------------------------------------------

SCHEDULE_J = PolicyRule(
    code="IN_DMRA_SCHEDULE_J",
    jurisdiction="in",
    instrument="dmr_act_schedule_j",
    gate_stage=2,
    rule_type=RuleType.TERM_LIST,
    title="Schedule J prohibited condition",
    severity=Severity.BLOCK,
    explanation=(
        "The Drugs & Magic Remedies (Objectionable Advertisements) Act prohibits "
        "advertising a remedy for the conditions listed in Schedule J. "
        "needs_legal_verification: confirm against the current Schedule with counsel."
    ),
    source_url="https://www.indiacode.nic.in/handle/123456789/1391",
    as_of=date(2026, 8, 1),
    terms=("piles", "haemorrhoids", "bawasir", "diabetes", "cancer", "aids"),
    remedy_template="Describe the product category without naming the condition.",
    business_types=("ayurveda",),
)

PERSONAL_ATTRIBUTES = PolicyRule(
    code="META_PA_SECOND_PERSON_HEALTH",
    jurisdiction="meta",
    instrument="meta_personal_attributes",
    gate_stage=3,
    rule_type=RuleType.REGEX,
    title="Second-person health framing",
    severity=Severity.BLOCK,
    explanation="Meta prohibits implying knowledge of the viewer's medical condition.",
    source_url="https://www.facebook.com/policies/ads/prohibited_content/personal_attributes",
    as_of=date(2026, 3, 1),
    pattern=r"(are you|do you|aap ?ko|aapko|kya aap)[^.!?]{0,60}(suffer|suffering|pain|problem|piles|bawasir)",
    remedy_template="Rewrite feature-forward and in the third person.",
)

OUTCOME_TIMELINE = PolicyRule(
    code="META_OUTCOME_TIMELINE",
    jurisdiction="meta",
    instrument="meta_misleading_claims",
    gate_stage=4,
    rule_type=RuleType.REGEX,
    title="Timeline-to-result claim",
    severity=Severity.BLOCK,
    explanation="A promised time to result is rejected as a misleading transformation claim.",
    source_url="https://www.facebook.com/policies/ads/prohibited_content/misleading_claims",
    as_of=date(2026, 3, 1),
    # Matched in both orders, because real copy uses both:
    #   duration then outcome - "7 din mein result", "just 30 days relief"
    #   outcome then duration - "Results in 15 days"
    # The leading preposition is optional: Hinglish routinely omits it.
    pattern=(
        r"(?:(?:in|within|just|only|sirf)?\s*\b[0-9]{1,3}\s*(?:days?|weeks?|din)\b"
        r"[^.!?]{0,40}\b(?:result|results|cure|relief|thik|khatam)\b)"
        r"|(?:\b(?:result|results|cure|relief|thik|khatam)\b[^.!?]{0,40}"
        r"\b[0-9]{1,3}\s*(?:days?|weeks?|din)\b)"
    ),
)

IMAGERY = PolicyRule(
    code="META_IMAGERY_BEFORE_AFTER",
    jurisdiction="meta",
    instrument="meta_health_imagery",
    gate_stage=5,
    rule_type=RuleType.LLM_JUDGE,
    title="Before/after or transformation imagery",
    severity=Severity.BLOCK,
    explanation="Before/after imagery is prohibited.",
    source_url="https://www.facebook.com/policies/ads/prohibited_content/adult_health",
    as_of=date(2026, 3, 1),
)

AI_DISCLOSURE = PolicyRule(
    code="META_AI_DISCLOSURE_REQUIRED",
    jurisdiction="meta",
    instrument="meta_ai_disclosure",
    gate_stage=6,
    rule_type=RuleType.STATE_CHECK,
    title="AI-generated content must be declared",
    severity=Severity.BLOCK,
    explanation="Disclosure is required for AI-generated or substantially modified assets.",
    source_url="https://transparency.meta.com/en-gb/policies/ad-standards/",
    as_of=date(2026, 3, 1),
)

LICENCE = PolicyRule(
    code="IN_AYUSH_LICENCE_ON_FILE",
    jurisdiction="in",
    instrument="ayush_guidelines",
    gate_stage=9,
    rule_type=RuleType.STATE_CHECK,
    title="AYUSH licence and product classification",
    severity=Severity.BLOCK,
    explanation="AYUSH licensing must be in place before the ad runs.",
    source_url="https://www.ayush.gov.in/",
    as_of=date(2026, 8, 1),
    business_types=("ayurveda",),
)

ALL_RULES = [SCHEDULE_J, PERSONAL_ATTRIBUTES, OUTCOME_TIMELINE, IMAGERY, AI_DISCLOSURE, LICENCE]


def compliant_ayurveda_bundle(**overrides) -> CreativeBundle:
    """The licence facts arrive as a resolved LicencePosture, not as loose
    strings on the bundle.

    ``ayush_licence_no`` and ``product_classification`` are still accepted here
    as a convenience, and are folded into a ``catalogue`` posture - the shape
    the orchestrator builds after reading t_advit.catalog_products. There is
    no longer any way to hand the gate a licence number without saying where it
    came from, which is what made a request-body licence indistinguishable from
    a filed one.
    """
    base = dict(
        primary_text="An Ayurvedic formulation prepared in the traditional manner.",
        headline="Traditional Ayurvedic care",
        description="Made to classical preparation standards.",
        cta_type="WHATSAPP",
        destination_url="https://example.in/product",
        business_type="ayurveda",
        has_media=False,
    )
    posture = LicencePosture(
        source="catalogue",
        sku=overrides.pop("sku", "PC-001"),
        ayush_licence_no=overrides.pop("ayush_licence_no", "UP-AYUR-12345"),
        classification=overrides.pop("product_classification", "ayurvedic_drug"),
        unresolved_reason=overrides.pop("unresolved_reason", None),
    )
    base["licence_posture"] = overrides.pop("licence_posture", posture)
    base.update(overrides)
    return CreativeBundle(**base)


@pytest.fixture
def gate() -> ComplianceGate:
    return ComplianceGate(ALL_RULES)


# ---------------------------------------------------------------------------
# Appendix D.3 - Schedule J claim in the copy
# ---------------------------------------------------------------------------


def test_schedule_j_claim_is_blocked(gate):
    result = gate.check(compliant_ayurveda_bundle(
        primary_text="Ye ayurvedic churn piles ko jad se khatam karta hai."
    ))
    assert result.verdict is Verdict.BLOCK
    assert result.blocked


def test_block_names_the_instrument(gate):
    result = gate.check(compliant_ayurveda_bundle(primary_text="Permanent relief from piles."))
    finding = next(f for f in result.findings if f.rule_code == "IN_DMRA_SCHEDULE_J")
    assert finding.instrument == "dmr_act_schedule_j"
    assert finding.layer == "india"


def test_block_quotes_the_offending_span(gate):
    text = "Ayurvedic care for piles and related discomfort."
    result = gate.check(compliant_ayurveda_bundle(primary_text=text))
    finding = next(f for f in result.findings if f.rule_code == "IN_DMRA_SCHEDULE_J")

    assert finding.offending_span == "piles"
    assert finding.field == "primary_text"
    # The span must index back into the exact source text so the UI can
    # highlight it rather than re-searching and guessing.
    assert text[finding.span_start:finding.span_end] == "piles"


def test_block_cites_source_and_effective_date(gate):
    result = gate.check(compliant_ayurveda_bundle(primary_text="Cures piles fast."))
    finding = next(f for f in result.findings if f.rule_code == "IN_DMRA_SCHEDULE_J")
    assert finding.source_url.startswith("https://")
    assert finding.as_of == date(2026, 8, 1)


def test_block_offers_a_rewrite_where_one_exists(gate):
    result = gate.check(compliant_ayurveda_bundle(primary_text="Relief from piles."))
    finding = next(f for f in result.findings if f.rule_code == "IN_DMRA_SCHEDULE_J")
    assert finding.suggested_rewrite


def test_legal_verification_flag_is_surfaced(gate):
    """The Schedule J list is encoded from public summaries. The gate must say
    so rather than presenting itself as a legal authority (PRD 4.5)."""
    result = gate.check(compliant_ayurveda_bundle(primary_text="For piles."))
    finding = next(f for f in result.findings if f.rule_code == "IN_DMRA_SCHEDULE_J")
    assert finding.needs_legal_verification is True


def test_meta_and_india_layers_are_reported_independently(gate):
    """Passing one layer implies nothing about the other, so each gets its own
    verdict (PRD Appendix D.3)."""
    result = gate.check(compliant_ayurveda_bundle(
        primary_text="Kya aap piles ki problem se pareshan hain? 7 din mein result."
    ))
    assert result.layer_verdict("meta") is Verdict.BLOCK
    assert result.layer_verdict("india") is Verdict.BLOCK

    codes = {f.rule_code for f in result.meta_findings}
    assert "META_PA_SECOND_PERSON_HEALTH" in codes
    assert "META_OUTCOME_TIMELINE" in codes
    assert {f.rule_code for f in result.india_findings} == {"IN_DMRA_SCHEDULE_J"}


def test_india_layer_can_block_while_meta_layer_passes(gate):
    """Third-person, no timeline, no personal attribute - clean under Meta
    policy, still illegal under the DMR Act."""
    result = gate.check(compliant_ayurveda_bundle(
        primary_text="A traditional preparation used in the management of piles."
    ))
    assert result.layer_verdict("meta") is Verdict.PASS
    assert result.layer_verdict("india") is Verdict.BLOCK
    assert result.verdict is Verdict.BLOCK


# ---------------------------------------------------------------------------
# Honest reporting of what was not checked
# ---------------------------------------------------------------------------


def test_unimplemented_stages_are_reported_as_skipped(gate):
    result = gate.check(compliant_ayurveda_bundle())
    assert 7 in result.stages_skipped   # landing page fetch
    assert 8 in result.stages_skipped   # DPDP consent


def test_clean_copy_is_not_evaluated_rather_than_passed(gate):
    """No rule fired, but two stages never ran. Reporting PASS here would imply
    a clean bill of health the gate cannot give (PRD 4.5)."""
    result = gate.check(compliant_ayurveda_bundle())
    assert result.findings == []
    assert result.verdict is Verdict.NOT_EVALUATED


def test_llm_judge_rules_are_skipped_not_passed_without_a_judge(gate):
    result = gate.check(compliant_ayurveda_bundle(has_media=True, ai_generated_declared=False))
    assert 5 in result.stages_skipped
    assert not any(f.rule_code == "META_IMAGERY_BEFORE_AFTER" for f in result.findings)


def test_judge_is_consulted_when_configured():
    class AlwaysViolates:
        def evaluate(self, rule, bundle):
            return True, "split-frame before/after visual"

    judged = ComplianceGate(ALL_RULES, judge=AlwaysViolates())
    result = judged.check(compliant_ayurveda_bundle(has_media=True, ai_generated_declared=False))

    finding = next(f for f in result.findings if f.rule_code == "META_IMAGERY_BEFORE_AFTER")
    assert finding.offending_span == "split-frame before/after visual"
    assert 5 in result.stages_evaluated


# ---------------------------------------------------------------------------
# State checks
# ---------------------------------------------------------------------------


def test_undeclared_ai_state_on_media_is_a_violation(gate):
    """Undeclared is not the same as 'not AI'. An unanswered question is the
    violation (undisclosed AI content is ~14% of Meta rejections)."""
    result = gate.check(compliant_ayurveda_bundle(has_media=True, ai_generated_declared=None))
    assert any(f.rule_code == "META_AI_DISCLOSURE_REQUIRED" for f in result.findings)


def test_declared_ai_state_satisfies_the_check(gate):
    result = gate.check(compliant_ayurveda_bundle(has_media=True, ai_generated_declared=True))
    assert not any(f.rule_code == "META_AI_DISCLOSURE_REQUIRED" for f in result.findings)


def test_missing_ayush_licence_blocks(gate):
    result = gate.check(compliant_ayurveda_bundle(ayush_licence_no=None))
    finding = next(f for f in result.findings if f.rule_code == "IN_AYUSH_LICENCE_ON_FILE")
    assert "AYUSH licence number" in finding.offending_span


def test_unverified_classification_blocks(gate):
    """Misclassification is the most common root cause of an unfixable
    rejection (PRD 13.3), so 'unverified' is treated as missing."""
    result = gate.check(compliant_ayurveda_bundle(product_classification="unverified"))
    finding = next(f for f in result.findings if f.rule_code == "IN_AYUSH_LICENCE_ON_FILE")
    assert "product classification" in finding.offending_span


# ---------------------------------------------------------------------------
# Precision - a false-block rate that is too high teaches owners to override
# the gate, which is as much a defect as a miss (PRD 13.4)
# ---------------------------------------------------------------------------


def test_term_matching_respects_word_boundaries(gate):
    """'compiles' contains 'piles'; it is not a Schedule J claim."""
    result = gate.check(compliant_ayurveda_bundle(
        primary_text="Our team compiles feedback from every customer."
    ))
    assert not any(f.rule_code == "IN_DMRA_SCHEDULE_J" for f in result.findings)


def test_rules_do_not_apply_outside_their_business_type(gate):
    """Schedule J is scoped to the Ayurveda pack; a general D2C bundle must not
    inherit it."""
    result = gate.check(CreativeBundle(
        primary_text="Our piles of inventory are cleared.",
        business_type="general_d2c",
        has_media=False,
    ))
    assert not any(f.rule_code == "IN_DMRA_SCHEDULE_J" for f in result.findings)


def test_landing_page_text_is_searched_when_supplied(gate):
    """Review is multimodal and includes the destination first fold. When the
    text IS supplied it must be searched, even though fetching it (stage 7) is
    not implemented."""
    result = gate.check(compliant_ayurveda_bundle(
        lp_first_fold_text="Permanent cure for piles guaranteed."
    ))
    finding = next(f for f in result.findings if f.rule_code == "IN_DMRA_SCHEDULE_J")
    assert finding.field == "lp_first_fold_text"


def test_overall_risk_reflects_the_worst_finding(gate):
    clean = gate.check(compliant_ayurveda_bundle())
    blocked = gate.check(compliant_ayurveda_bundle(primary_text="Cures piles."))
    assert clean.overall_risk == 0.0
    assert blocked.overall_risk == 1.0
