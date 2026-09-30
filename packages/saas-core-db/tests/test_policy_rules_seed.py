"""The seeded compliance ruleset, verified against Postgres itself.

The Python gate and the SQL seed hold the same patterns in two dialects. This
suite runs the SQL versions through Postgres' own regex engine so a pattern
that silently fails to compile - or quietly matches nothing - is caught here
rather than by an advertiser whose ad was approved and should not have been.
"""

from __future__ import annotations

import pytest

from conftest import SUPERADMIN, rows_as, scalar_as


def rule(conn, code: str) -> dict:
    rows = rows_as(
        conn,
        SUPERADMIN,
        # Scope used to be an enum array on the row. It is a declared scope plus
        # a junction table now, so the industries come back through a subquery -
        # which is also the only place a foreign key could live, since Postgres
        # cannot key array elements.
        "select r.code, r.jurisdiction, r.instrument, r.gate_stage, r.rule_type::text, "
        "       r.severity::text, r.pattern, r.terms, r.source_url, r.as_of, "
        "       r.scope::text, "
        "       array(select i.industry_key from t_advit.policy_rule_industries i "
        "              where i.rule_code = r.code order by i.industry_key) "
        "  from t_advit.policy_rules r where r.code = %s",
        (code,),
    )
    assert rows, f"rule {code} is not seeded"
    keys = (
        "code", "jurisdiction", "instrument", "gate_stage", "rule_type",
        "severity", "pattern", "terms", "source_url", "as_of",
        "scope", "industry_keys",
    )
    return dict(zip(keys, rows[0]))


def matches(conn, code: str, text: str) -> bool:
    """Evaluate the rule's stored pattern with Postgres' regex engine."""
    return scalar_as(
        conn,
        SUPERADMIN,
        "select %s ~* (select pattern from t_advit.policy_rules where code = %s)",
        (text, code),
    )


# ---------------------------------------------------------------------------
# Provenance is part of the rule (PRD 13.5)
# ---------------------------------------------------------------------------


def test_every_rule_cites_a_source_and_an_effective_date(conn):
    bad = rows_as(
        conn,
        SUPERADMIN,
        "select code from t_advit.policy_rules "
        " where source_url is null or source_url = '' or as_of is null",
    )
    assert bad == [], f"rules missing provenance: {bad}"


def test_every_blocking_rule_offers_a_remedy(conn):
    """A block without a fix teaches the owner to override the gate."""
    bad = rows_as(
        conn,
        SUPERADMIN,
        "select code from t_advit.policy_rules "
        " where severity = 'block' and rule_type in ('term_list','regex') "
        "   and (remedy_template is null or remedy_template = '')",
    )
    assert bad == [], f"blocking rules with no remedy: {bad}"


def test_both_layers_are_represented(conn):
    layers = {
        r[0] for r in rows_as(
            conn, SUPERADMIN, "select distinct jurisdiction from t_advit.policy_rules"
        )
    }
    assert layers == {"meta", "in"}


# ---------------------------------------------------------------------------
# Patterns actually compile and actually match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Sirf 7 din mein result",
        "7 din mein result guaranteed",
        "Results in 15 days",
        "just 30 days relief",
        "10 hafte mein aaram",
    ],
)
def test_timeline_claims_match_including_hinglish(conn, text):
    """Hinglish timeline claims routinely omit the leading preposition
    ("7 din mein result"), which an English-shaped pattern misses entirely."""
    assert matches(conn, "META_OUTCOME_TIMELINE", text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Delivered in 3 days across India",
        "30 day return window",
        "Traditional preparation, made over 21 days",
    ],
)
def test_timeline_pattern_does_not_fire_on_logistics_copy(conn, text):
    """Shipping and returns language mentions days without promising a result.
    A false block here would be as damaging as a miss (PRD 13.4)."""
    assert matches(conn, "META_OUTCOME_TIMELINE", text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Kya aap piles ki problem se pareshan hain?",
        "Are you suffering from constant pain?",
        "Aapko bawasir ki takleef hai?",
    ],
)
def test_second_person_health_framing_matches(conn, text):
    assert matches(conn, "META_PA_SECOND_PERSON_HEALTH", text) is True


def test_third_person_product_copy_does_not_match(conn):
    """The compliant rewrite the gate recommends must itself pass."""
    compliant = "An Ayurvedic formulation prepared in the traditional manner."
    assert matches(conn, "META_PA_SECOND_PERSON_HEALTH", compliant) is False
    assert matches(conn, "META_OUTCOME_GUARANTEE", compliant) is False


@pytest.mark.parametrize(
    "text",
    ["100% cure guaranteed", "permanent solution", "jad se khatam", "guaranteed result"],
)
def test_guarantee_language_matches(conn, text):
    assert matches(conn, "META_OUTCOME_GUARANTEE", text) is True


# ---------------------------------------------------------------------------
# Schedule J
# ---------------------------------------------------------------------------


def test_schedule_j_is_scoped_to_the_ayurveda_pack(conn):
    r = rule(conn, "IN_DMRA_SCHEDULE_J")
    assert r["industry_keys"] == ["ayurveda"]
    # Stated as well as listed. Under the enum array, a rule that named no
    # industry applied to EVERY pack, so a scoping mistake and a scoping
    # omission were the same row - and this assertion could not tell them apart.
    assert r["scope"] == "listed_industries"
    assert r["gate_stage"] == 2
    assert r["severity"] == "block"


def test_schedule_j_covers_the_conditions_this_account_advertises(conn):
    """The connected accounts are a piles-care advertiser, so this list is not
    hypothetical for them."""
    terms = {t.lower() for t in rule(conn, "IN_DMRA_SCHEDULE_J")["terms"]}
    for expected in ("piles", "haemorrhoids", "bawasir", "fistula"):
        assert expected in terms


def test_schedule_j_flags_that_it_needs_legal_verification(conn):
    """The list is encoded from public summaries. The gate must say so rather
    than presenting itself as a legal authority (PRD 4.5)."""
    explanation = scalar_as(
        conn,
        SUPERADMIN,
        "select explanation from t_advit.policy_rules where code = 'IN_DMRA_SCHEDULE_J'",
    )
    assert "needs_legal_verification" in explanation


# ---------------------------------------------------------------------------
# The second pack, which exists to prove a pack is data
# ---------------------------------------------------------------------------


def test_the_real_estate_pack_is_rows_and_no_ddl(conn):
    """The pack is four kinds of row and no DDL - first as a seed, and since
    20260917000003 as a data-only migration so that it reaches production the
    way everything else does. If a pack ever needs DDL again, t_advit.industries
    has failed at the only job it was created to do."""
    r = rule(conn, "IN_RERA_ASSURED_RETURN")
    assert r["industry_keys"] == ["real_estate"]
    assert r["scope"] == "listed_industries"
    assert r["severity"] == "block"


def test_a_real_estate_advertiser_is_not_held_to_schedule_j(conn):
    """Schedule J names conditions under the Drugs & Magic Remedies Act. A
    property listing that mentions a corner plot near a cancer hospital has
    committed no offence, and blocking it would teach the owner to override the
    gate - which is as much a defect as a miss (PRD 13.4)."""
    codes = {
        r[0] for r in rows_as(
            conn, SUPERADMIN,
            "select r.code from t_advit.policy_rules r"
            " where r.is_active and exists ("
            "   select 1 from t_advit.policy_rule_industries i"
            "    where i.rule_code = r.code and i.industry_key = 'real_estate')",
        )
    }
    assert "IN_DMRA_SCHEDULE_J" not in codes
    assert "IN_RERA_REGISTRATION_ON_FILE" in codes


def test_the_rera_registration_check_names_the_facts_it_needs(conn):
    """The AYUSH licence check used to be a branch in Python keyed on rule.code,
    which made the analogous RERA check a deploy. Naming the facts as data is
    what turns it back into an INSERT."""
    facts = scalar_as(
        conn, SUPERADMIN,
        "select required_facts from t_advit.policy_rules"
        " where code = 'IN_RERA_REGISTRATION_ON_FILE'",
    )
    assert set(facts) == {"rera_registration_no", "rera_authority_url"}


def test_the_rera_rules_say_they_need_legal_verification(conn):
    """RERA is administered state by state and this pack encodes only the
    central provisions. The gate must say so rather than presenting itself as a
    legal authority (PRD 4.5)."""
    for code in ("IN_RERA_REGISTRATION_ON_FILE", "IN_RERA_ASSURED_RETURN"):
        explanation = scalar_as(
            conn, SUPERADMIN,
            "select explanation from t_advit.policy_rules where code = %s", (code,),
        )
        assert "needs_legal_verification" in explanation, code


# ---------------------------------------------------------------------------
# Unimplemented stages are declared, not hidden
# ---------------------------------------------------------------------------


def test_unimplemented_stages_still_have_rules_registered(conn):
    """Stages 7 (landing page) and 8 (DPDP consent) are not implemented in this
    build. Their rules are seeded anyway so the gap is visible in the ruleset
    rather than being an absence nobody notices."""
    stages = {
        r[0] for r in rows_as(
            conn, SUPERADMIN, "select distinct gate_stage from t_advit.policy_rules"
        )
    }
    assert 8 in stages


def test_platform_knowledge_carries_as_of_dates(conn):
    """Half of what the OS knows about Meta will be stale within a year, so a
    record with no effective date cannot be cited (PRD 15)."""
    bad = rows_as(
        conn,
        SUPERADMIN,
        "select topic from t_advit.platform_knowledge where as_of is null",
    )
    assert bad == []


def test_attribution_regime_change_is_recorded(conn):
    """The single most likely source of confidently-wrong analysis in 2026."""
    rows = rows_as(
        conn,
        SUPERADMIN,
        "select statement from t_advit.platform_knowledge "
        " where topic = 'attribution' and as_of = date '2026-01-12'",
    )
    assert rows and "view-through" in rows[0][0]
