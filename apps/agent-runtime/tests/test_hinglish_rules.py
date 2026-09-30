# -*- coding: utf-8 -*-
"""The India-layer rules, in the language the market actually writes in.

Five findings, one cause: the packs were written in English with a few Hinglish
citation forms bolted on, by someone who could read the transliterations but was
not writing the copy. Every one of them failed in a direction that matters:

  * ``META_PA_SECOND_PERSON_HEALTH`` carried ``你`` — Chinese for "you" — where a
    Devanagari alternative should have been, so the rule had **no Devanagari
    coverage at all**, and among Latin forms it omitted bare ``aap``, which is
    the commonest second-person framing in this market.
  * ``META_OUTCOME_GUARANTEE`` matched the bare word "guarantee", so a BLOCK rule
    scoped to *every* industry fired on "money-back guarantee".
  * ``META_OUTCOME_TIMELINE`` knew ``din``/``mahine``/``hafte`` — the citation
    forms — and not ``dino``/``mahino``/``hafton``, the oblique plurals Hindi
    actually uses before a postposition, which is the exact construction the rule
    targets.
  * ``META_OUTCOME_CURE_VERB`` blocked ``jad se``, which means "from the root" and
    is what Ayurvedic copy says about its botanical source.
  * ``IN_DMRA_SCHEDULE_J`` — the only rule in the India layer that can BLOCK —
    had Hinglish for piles and for no other condition.

And one the ledger did not know about, found while fixing the last of them:
``_term_pattern`` wrapped every term in ``\\b(...)\\b``, and Devanagari matras are
categories Mc/Mn, which ``\\w`` does not match. Five of the Schedule J names the
market uses (मोटापा, नपुंसकता, मिर्गी, लकवा, पथरी) end in a matra, so under the
old matcher they could never have fired. Seeding them would have produced a legal
gate that looks armed and adjudicates nothing.

Every test here asserts BOTH directions. Narrowing a rule until its false
positive disappears is not a fix if it also lets a real violation through, and on
the Drugs & Magic Remedies Act that is the expensive direction to get wrong.
"""

from __future__ import annotations

import pytest

from app.agents.compliance import CreativeBundle, Severity, _term_pattern
from app.policy.rules import PolicyRuleLoader


# ---------------------------------------------------------------------------
# The matcher, with no database in sight
# ---------------------------------------------------------------------------

MATRA_FINAL = ["मोटापा", "नपुंसकता", "मिर्गी", "लकवा", "पथरी", "मर्दाना कमजोरी"]


@pytest.mark.parametrize("term", MATRA_FINAL)
def test_a_term_ending_in_a_matra_can_be_matched_at_all(term):
    """The bug that would have made the Schedule J additions decorative.

    ``\\b`` is defined in terms of ``\\w``, and a Devanagari vowel sign is
    category Mc — not a word character — so ``\\b(मोटापा)\\b`` has no boundary to
    find after the final ``ा`` and never matches. The term sits in the pack,
    passes every membership assertion in the database suite, and adjudicates
    nothing.
    """
    assert _term_pattern([term]).search(f"आयुर्वेदिक दवा {term} के लिए"), (
        f"{term!r} cannot be matched; the term-list boundary is using \\b and this "
        "word ends in a combining mark"
    )


@pytest.mark.parametrize(
    "text,term,expected",
    [
        # The two cases the original \b was written for. Both must survive.
        ("This compiles without errors", "piles", False),
        ("Silk braids and ribbons", "aids", False),
        ("Piles ka ilaj", "piles", True),
        ("HIV and AIDS awareness", "aids", True),
        # Devanagari must not match across a word it is merely a prefix of.
        ("आपस में बात करें", "आप", False),
        ("आप परेशान हैं", "आप", True),
    ],
)
def test_latin_boundaries_are_unchanged(text, term, expected):
    """The wider boundary must not become a substring match. If it did, the fix
    would trade a silent false negative for a loud false positive."""
    assert bool(_term_pattern([term]).search(text)) is expected


# ---------------------------------------------------------------------------
# The seeded rules, through the real gate
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ayurveda():
    from app.agents.compliance import ComplianceGate

    return ComplianceGate(list(PolicyRuleLoader().load("ayurveda")))


def codes(gate, **bundle) -> set[str]:
    """Judge under the Ayurveda pack unless told otherwise.

    ``CreativeBundle.business_type`` defaults to ``general_d2c``, and
    ``IN_DMRA_SCHEDULE_J`` is scoped to ``ayurveda`` — so building the gate from
    the Ayurveda ruleset and then handing it a general_d2c bundle filters the
    India layer straight back out. The Meta-layer rules are ``all_industries``
    and apply either way, which is why only the Schedule J cases noticed.
    """
    bundle.setdefault("business_type", "ayurveda")
    return {f.rule_code for f in gate.check(CreativeBundle(**bundle)).findings}


# -- second-person health framing -------------------------------------------

RULE = "META_PA_SECOND_PERSON_HEALTH"


@pytest.mark.parametrize(
    "copy",
    [
        "Are you suffering from piles?",
        "Aapko bawasir hai?",
        "Aap pareshan hain is dard se?",
        "क्या आपको बवासीर है?",
        "आप परेशान हैं?",
        "तुम्हें दर्द है?",
    ],
)
def test_second_person_health_framing_blocks(ayurveda, copy):
    assert RULE in codes(ayurveda, primary_text=copy), copy


@pytest.mark.parametrize(
    "copy",
    [
        "Ek Ayurvedic formulation, paachan ke liye.",
        "Aapke ghar tak free delivery.",
        "आपके लिए शुद्ध आयुर्वेदिक तेल.",
    ],
)
def test_second_person_without_a_health_claim_passes(ayurveda, copy):
    """``aap`` on its own is not the violation — pairing it with an assumed
    condition is. The same-sentence window is what keeps the bare pronoun from
    blocking ordinary copy."""
    assert RULE not in codes(ayurveda, primary_text=copy), copy


def test_the_chinese_pronoun_is_gone(ayurveda):
    """It could never match Hindi, Hinglish or Devanagari copy, and it was
    standing where a Devanagari alternative should have been — so its presence
    made the rule look bilingual while covering one script."""
    rule = next(r for r in PolicyRuleLoader().load("ayurveda") if r.code == RULE)
    assert "你" not in (rule.pattern or "")


# -- guarantees --------------------------------------------------------------

GUARANTEE = "META_OUTCOME_GUARANTEE"


@pytest.mark.parametrize(
    "copy",
    [
        "100% cure for piles",
        "Guaranteed relief in every case",
        "Result guaranteed",
        "permanent cure",
        "pakka ilaj",
        "Jad se khatam karein",
        "जड़ से खत्म",
        "गारंटी के साथ इलाज",
        "इलाज की गारंटी",   # head-final: the natural Hindi order
    ],
)
def test_an_outcome_guarantee_still_blocks(ayurveda, copy):
    assert GUARANTEE in codes(ayurveda, primary_text=copy), copy


@pytest.mark.parametrize(
    "copy",
    [
        "30-day money-back guarantee",
        "Authenticity guarantee on every bottle",
        "100% money back guarantee",
        "Replacement guarantee if the seal is broken",
    ],
)
def test_a_commercial_guarantee_is_not_an_outcome_claim(ayurveda, copy):
    """This rule is scoped to every industry, so the bare word "guarantee"
    blocked money-back terms for every tenant on the platform, not only
    Ayurveda."""
    assert GUARANTEE not in codes(ayurveda, primary_text=copy), copy


# -- timelines ---------------------------------------------------------------

TIMELINE = "META_OUTCOME_TIMELINE"


@pytest.mark.parametrize(
    "copy",
    [
        "7 din mein result",
        "7 dino mein aaram",      # oblique plural
        "2 mahino mein farq",
        "3 hafton mein asar",
        "Results in 15 days",
        "7 दिनों में आराम",
        "2 महीनों में फर्क",
    ],
)
def test_a_timeline_to_result_blocks_in_either_script(ayurveda, copy):
    assert TIMELINE in codes(ayurveda, primary_text=copy), copy


@pytest.mark.parametrize("copy", ["Ships in 3 days", "30 day return window"])
def test_a_delivery_or_returns_window_is_not_a_result_claim(ayurveda, copy):
    assert TIMELINE not in codes(ayurveda, primary_text=copy), copy


# -- cure verbs --------------------------------------------------------------

CURE = "META_OUTCOME_CURE_VERB"


@pytest.mark.parametrize(
    "copy", ["Permanent cure for piles", "Ayurvedic ilaj", "आयुर्वेदिक इलाज"]
)
def test_cure_language_still_blocks(ayurveda, copy):
    assert CURE in codes(ayurveda, primary_text=copy), copy


@pytest.mark.parametrize(
    "copy",
    ["Neem ki jad se banaya gaya", "Jad se taiyaar Ayurvedic tel"],
)
def test_describing_the_botanical_root_is_not_a_cure_claim(ayurveda, copy):
    """जड़ means root. "jad se" — "from the root" — is what honest ingredient
    copy says about its source, and it was a standalone block term."""
    assert CURE not in codes(ayurveda, primary_text=copy), copy


def test_the_cure_idiom_is_still_caught_by_the_guarantee_rule(ayurveda):
    """The half of the previous test that makes it a fix rather than a
    weakening. Dropping ``jad se`` from the term list is only safe because
    ``jad se khatam`` — the actual cure claim — is matched elsewhere, and now by
    elimination verb rather than by one hard-coded spelling."""
    for copy in ("Jad se khatam", "jad se mita dega", "jad se door karein"):
        assert GUARANTEE in codes(ayurveda, primary_text=copy), copy


# -- Schedule J --------------------------------------------------------------

SCHEDULE_J = "IN_DMRA_SCHEDULE_J"


@pytest.mark.parametrize(
    "copy",
    [
        "Ayurvedic support for diabetes",
        "मधुमेह के लिए",
        "मोटापा कम करें",
        "मिर्गी की दवा",
        "लकवा का उपाय",
        "पथरी के लिए",
        "Mardana kamzori ka upay",
        "गुप्त रोग",
        "Bawasir ke liye",
        "Ganjapan ka ilaj",
        "सफेद दाग हटाएं",
    ],
)
def test_a_schedule_j_condition_blocks_under_its_indian_name(ayurveda, copy):
    """The only rule in the India layer that can BLOCK. Every name it did not
    know was a Schedule J condition advertisable in the language this market
    writes in."""
    assert SCHEDULE_J in codes(ayurveda, primary_text=copy), copy


@pytest.mark.parametrize(
    "copy",
    [
        "No added sugar",
        "This compiles without errors",
        "Silk braids and ribbons",
        "Sugar-free chyawanprash",
    ],
)
def test_the_terms_deliberately_left_out_do_not_fire(ayurveda, copy):
    """``sugar`` is the one that matters: it is a Schedule J condition's common
    name AND on half the food and supplement copy in India. Adding it would have
    made the rule unusable, so it is excluded on purpose and this pins that."""
    assert SCHEDULE_J not in codes(ayurveda, primary_text=copy), copy


def test_schedule_j_severity_is_still_block(ayurveda):
    """A term list this long is only worth having if it still halts the run."""
    rule = next(r for r in PolicyRuleLoader().load("ayurveda") if r.code == SCHEDULE_J)
    assert rule.severity is Severity.BLOCK


def test_the_india_layer_does_not_reach_a_general_d2c_workspace():
    """The repo's canonical false positive, re-pinned after widening the list.

    "piles" is a Schedule J term and "piles of stock" is ordinary English. What
    keeps that from blocking is not the term list — it is the pack: Schedule J is
    scoped to Ayurveda, and a general_d2c workspace is judged under its own
    rules. Widening the list makes this property more load-bearing, not less.
    """
    from app.agents.compliance import ComplianceGate

    gate = ComplianceGate(list(PolicyRuleLoader().load("general_d2c")))
    result = gate.check(
        CreativeBundle(
            primary_text="We cleared our piles of stock this week",
            business_type="general_d2c",
        )
    )
    assert SCHEDULE_J not in {f.rule_code for f in result.findings}
