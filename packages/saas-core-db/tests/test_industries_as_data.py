"""Adding an industry must be a row insert, not a migration and a deploy.

`t_advit.business_type` was an enum, and it decided which body of statute an
advertiser is held to. Real estate is the next pack. Under an enum, shipping it
meant altering the type, altering three consumers - one of them an ARRAY of the
enum - and redeploying a runtime that repeated the same two labels in a Pydantic
Literal.

These tests hold the three properties that make the replacement worth the
migration:

  * a pack is data, end to end, and a pack the seeds did not create can be
    created and resolved inside a transaction;
  * the referential integrity the enum element type used to provide did not
    quietly disappear when the array became a junction table;
  * "applies to every pack" and "nobody filled this in" can no longer be spelled
    the same way, because that is how a statutory layer goes missing without
    anyone deciding it should.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import MEMBER, OUTSIDER, SUPERADMIN, rows_as, scalar_as, acting_as

WORKSPACE_DEMO_BRAND = "00000000-0000-4000-8000-000000000050"
WORKSPACE_RIVAL = "00000000-0000-4000-8000-000000000051"


def resolve(conn, industry_key: str) -> set[str]:
    """The rule codes the loader would serve for this industry.

    Deliberately the same SQL shape as PolicyRuleLoader._fetch, so a divergence
    between what the database scopes and what the runtime loads shows up here.
    """
    return {
        r[0]
        for r in rows_as(
            conn,
            SUPERADMIN,
            """
            select r.code from t_advit.policy_rules r
             where r.is_active
               and (r.scope = 'all_industries'
                    or exists (select 1 from t_advit.policy_rule_industries i
                                where i.rule_code = r.code
                                  and i.industry_key = %s))
            """,
            (industry_key,),
        )
    }


# A pack no seed ships, so this suite is testing the mechanism rather than
# re-reading 04_real_estate_pack.sql. Quick commerce is the plausible third
# vertical after real estate; what matters is that nothing has migrated for it.
UNSEEDED_PACK = "quick_commerce"


def add_an_unseeded_pack(conn) -> None:
    """The whole of a new pack, as data. No DDL appears in this function - that
    is the assertion."""
    assert not rows_as(
        conn, SUPERADMIN, "select 1 from t_advit.industries where key = %s", (UNSEEDED_PACK,)
    ), f"{UNSEEDED_PACK} is seeded now; pick a key nothing ships so this still proves something"

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.industries
              (key, display_name, status, summary, statutory_note)
            values (%s, 'Quick commerce', 'active',
                    'Ten-minute grocery and essentials delivery.',
                    'Legal Metrology (Packaged Commodities) Rules on declared quantity.')
            """,
            (UNSEEDED_PACK,),
        )
        cur.execute(
            """
            insert into t_advit.policy_rules
              (code, jurisdiction, instrument, gate_stage, rule_type, title,
               severity, explanation, remedy_template, source_url, as_of,
               scope, required_facts)
            values ('IN_TEST_LM_DECLARED_QUANTITY', 'in', 'legal_metrology_pcr', 9,
                    'state_check', 'Declared quantity on file', 'block',
                    'The declared quantity must match the pack being advertised.',
                    'Record the declared quantity on the product.',
                    'https://example.test/legal-metrology', '2026-08-01',
                    'listed_industries', array['declared_quantity'])
            """
        )
        cur.execute(
            "insert into t_advit.policy_rule_industries (rule_code, industry_key) "
            "values ('IN_TEST_LM_DECLARED_QUANTITY', %s)",
            (UNSEEDED_PACK,),
        )


# ---------------------------------------------------------------------------
# The claim the table was created to make
# ---------------------------------------------------------------------------


def test_a_new_industry_pack_is_created_without_any_ddl(conn):
    """The whole point. If this test ever needs a `create` or an `alter` to
    pass, industries have stopped being data and the enum has grown back."""
    add_an_unseeded_pack(conn)

    codes = resolve(conn, UNSEEDED_PACK)
    assert "IN_TEST_LM_DECLARED_QUANTITY" in codes, "the new pack's own rule must load"
    assert "META_OUTCOME_GUARANTEE" in codes, "and so must every all_industries rule"
    assert "IN_DMRA_SCHEDULE_J" not in codes, (
        "Schedule J is scoped to Ayurveda. A grocery advertiser inheriting it would "
        "be blocked for words that carry no statutory meaning in their category."
    )
    conn.rollback()


def test_the_two_seeded_industries_keep_the_identities_the_seeds_rely_on(conn):
    """The keys are the enum labels they replaced, character for character, so
    `business_type::text` backfilled cleanly and every seed, fixture and test
    that spells 'ayurveda' or 'general_d2c' keeps meaning the same thing."""
    keys = {r[0] for r in rows_as(conn, SUPERADMIN, "select key from t_advit.industries")}
    assert {"ayurveda", "general_d2c"} <= keys


def test_general_d2c_still_does_not_load_the_indian_health_layer(conn):
    """The canonical false positive: 'we cleared our piles of stock this week'
    must not meet Schedule J. tests/test_api.py asserts the same thing over
    HTTP; this is the database half of it."""
    assert "IN_DMRA_SCHEDULE_J" not in resolve(conn, "general_d2c")
    assert "IN_DMRA_SCHEDULE_J" in resolve(conn, "ayurveda")


def test_the_business_type_enum_is_gone_rather_than_left_as_a_dead_type(conn):
    """A dead type still casts. `%s::t_advit.business_type` would keep
    compiling, so the next migration reaching for the familiar name would
    re-couple to a type nobody maintains - and would reject 'real_estate'."""
    assert scalar_as(
        conn,
        SUPERADMIN,
        "select exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace "
        "                where n.nspname = 't_advit' and t.typname = 'business_type')",
    ) is False


# ---------------------------------------------------------------------------
# The integrity the enum element type used to provide
# ---------------------------------------------------------------------------


def test_a_rule_cannot_be_scoped_to_an_industry_that_does_not_exist(conn):
    """Postgres cannot foreign-key array elements, so `business_types` accepted
    any label the enum happened to carry and nothing else could be said about
    it. A junction table can hold a real foreign key, which is the reason it is
    a junction table."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.cursor() as cur:
            cur.execute(
                "insert into t_advit.policy_rule_industries (rule_code, industry_key) "
                "values ('IN_DMRA_SCHEDULE_J', 'ayurvedaa')"
            )
    conn.rollback()


def test_an_industry_cannot_be_deleted_while_a_workspace_still_points_at_it(conn):
    """Deleting a pack out from under a live advertiser would leave their
    workspace pointing at nothing, and 'no industry' resolves to no statutory
    layer. Deprecate it instead - that is what the status column is for."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.cursor() as cur:
            cur.execute("delete from t_advit.industries where key = 'ayurveda'")
    conn.rollback()


def test_renaming_an_industry_carries_its_workspaces_and_its_rule_scopes_with_it(conn):
    """`on update cascade` on both references. Under the array this was not
    expressible at all: renaming an enum label and fixing up every array that
    mentioned it were two unrelated operations, and forgetting the second
    silently unscoped the rule."""
    before = scalar_as(
        conn, SUPERADMIN,
        "select industry_key from t_advit.workspaces where id = %s",
        (WORKSPACE_DEMO_BRAND,),
    )
    scoped_before = resolve(conn, before)

    with conn.cursor() as cur:
        cur.execute("update t_advit.industries set key = 'renamed_pack' where key = %s",
                    (before,))
        assert scalar_as(
            conn, SUPERADMIN,
            "select industry_key from t_advit.workspaces where id = %s",
            (WORKSPACE_DEMO_BRAND,),
        ) == "renamed_pack"
        # And the rule scoping came with it, rule for rule. Under the array this
        # was not expressible at all: renaming the enum label and fixing every
        # array that mentioned it were two unrelated operations.
        assert resolve(conn, "renamed_pack") == scoped_before
    conn.rollback()


# ---------------------------------------------------------------------------
# An empty scope is no longer a decision you can make by accident
# ---------------------------------------------------------------------------


def test_a_rule_scoped_to_listed_industries_that_names_none_is_refused(conn):
    """Under `business_types text[] default '{}'`, this row would have been
    accepted and would have applied to EVERY pack - the empty set meant
    universal. The same typo now means the rule is dead. Neither reading may be
    reached by omission, so the pair has to be declared and checked."""
    with pytest.raises(psycopg.errors.CheckViolation, match="names none"):
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.policy_rules
                  (code, jurisdiction, instrument, gate_stage, rule_type, title, terms,
                   severity, explanation, source_url, as_of, scope)
                values ('IN_TEST_ORPHAN', 'in', 'test', 2, 'term_list', 'orphan',
                        array['x'], 'block', 'x', 'https://example.test', '2026-01-01',
                        'listed_industries')
                """
            )
            cur.execute("set constraints all immediate")
    conn.rollback()


def test_a_rule_scoped_to_all_industries_may_not_also_name_one(conn):
    """The two halves would disagree, and the loader reads scope first - so the
    junction row would be invisible and someone would spend an afternoon
    wondering why their scoping had no effect."""
    with pytest.raises(psycopg.errors.CheckViolation, match="also names"):
        with conn.cursor() as cur:
            cur.execute(
                "insert into t_advit.policy_rule_industries (rule_code, industry_key) "
                "values ('META_OUTCOME_GUARANTEE', 'ayurveda')"
            )
            cur.execute("set constraints all immediate")
    conn.rollback()


def test_a_state_check_that_names_neither_a_fact_nor_a_handler_is_refused(conn):
    """A state_check compares the bundle against something. One that names
    nothing finds nothing, and no findings reads as a pass - so a half-written
    licence rule would ship as a silent approval on a BLOCK-severity stage."""
    with pytest.raises(psycopg.errors.CheckViolation, match="has_matcher"):
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.policy_rules
                  (code, jurisdiction, instrument, gate_stage, rule_type, title,
                   severity, explanation, source_url, as_of, scope)
                values ('IN_TEST_BLIND', 'in', 'test', 9, 'state_check', 'blind',
                        'block', 'x', 'https://example.test', '2026-01-01', 'all_industries')
                """
            )
    conn.rollback()


def test_the_ayush_licence_rule_names_its_facts_rather_than_being_recognised_by_code(conn):
    """ComplianceGate._check_state used to dispatch on rule.code, so an
    analogous RERA registration check was a Python change and a deploy. The
    requirement is data now, which is what makes the RERA rule an INSERT."""
    facts = scalar_as(
        conn,
        SUPERADMIN,
        "select required_facts from t_advit.policy_rules "
        " where code = 'IN_AYUSH_LICENCE_ON_FILE'",
    )
    assert set(facts) == {"ayush_licence_no", "product_classification"}


# ---------------------------------------------------------------------------
# Industries are shared reference data, not tenant data
# ---------------------------------------------------------------------------


def test_every_signed_in_user_can_read_the_industry_catalogue(conn):
    """Shared reference data, like policy_rules and platform_knowledge. A member
    of one org reading the list of packs learns nothing about another tenant."""
    keys = {r[0] for r in rows_as(conn, OUTSIDER, "select key from t_advit.industries")}
    assert {"ayurveda", "general_d2c"} <= keys


def test_a_draft_pack_is_invisible_to_tenants_until_it_is_launched(conn):
    """A pack being authored has an incomplete ruleset. A workspace that could
    select it would be advertising under a statutory layer nobody has finished
    writing."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into t_advit.industries (key, display_name, status, summary) "
            "values (%s, 'Quick commerce', 'draft', 'in progress')",
            (UNSEEDED_PACK,),
        )
        visible = {r[0] for r in rows_as(conn, MEMBER, "select key from t_advit.industries")}
        assert UNSEEDED_PACK not in visible
        assert UNSEEDED_PACK in {
            r[0] for r in rows_as(conn, SUPERADMIN, "select key from t_advit.industries")
        }
    conn.rollback()


def test_a_tenant_cannot_write_the_industry_catalogue_at_all(conn):
    """No write policy exists and no write grant was issued. Which pack exists,
    and what is in it, is not something the regulated party edits."""
    with pytest.raises((psycopg.errors.InsufficientPrivilege,
                        psycopg.errors.InsufficientPrivilege)):
        with acting_as(conn, MEMBER) as c:
            with c.cursor() as cur:
                cur.execute(
                    "insert into t_advit.industries (key, display_name, status, summary) "
                    "values ('made_up', 'Made up', 'active', 'x')"
                )
    conn.rollback()


# The two tests that used to sit here - a tenant cannot move their own workspace
# to another industry, and a backend move is audited - live in
# test_business_type_guard.py, which covers the guard in more detail than a
# passing reference here could. Duplicating them would mean two places to
# update, and one of them would be forgotten.

# ---------------------------------------------------------------------------
# The pack identity, which used to be a hand-maintained string
# ---------------------------------------------------------------------------


def test_the_pack_id_is_derived_and_cannot_drift_from_the_industry(conn):
    """workspaces.industry_pack_id held 'general@1' on a workspace whose
    business_type said 'general_d2c'. Two hand-maintained notions of the same
    thing had already disagreed before anything read either of them."""
    rows = dict(rows_as(conn, SUPERADMIN, "select key, pack_id from t_advit.industries"))
    assert rows["ayurveda"] == "ayurveda@1"
    assert rows["general_d2c"] == "general_d2c@1"

    with conn.cursor() as cur:
        cur.execute("update t_advit.industries set pack_version = 2 where key = 'ayurveda'")
        assert scalar_as(
            conn, SUPERADMIN, "select pack_id from t_advit.industries where key = 'ayurveda'"
        ) == "ayurveda@2"
    conn.rollback()


def test_workspaces_no_longer_carry_a_second_notion_of_which_pack(conn):
    """One answer to "which pack", reachable by joining industry_key. The
    column that held the other one is gone rather than deprecated, because a
    deprecated column is one somebody still reads."""
    assert scalar_as(
        conn,
        SUPERADMIN,
        "select exists (select 1 from information_schema.columns "
        "                where table_schema = 't_advit' and table_name = 'workspaces' "
        "                  and column_name in ('business_type', 'industry_pack_id'))",
    ) is False
