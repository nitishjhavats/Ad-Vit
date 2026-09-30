"""The orchestrator graph.

Runs offline with a stub router, so the suite is free and deterministic. The
behaviours pinned here are the ones that cost money or trust when they break:

* A compliance block must cost nothing. Running the judgement tier alongside a
  gate that is about to reject the work burns real rupees on output nobody will
  ever see - measured at ~Rs 9 per blocked run before this was fixed.

* The gate must not treat ordinary conversation as ad copy. A budget question
  blocked on a missing AYUSH licence is a false block, and false blocks teach
  owners to override the gate.

* A block must be rendered deterministically. The owner needs the instrument,
  the span and the source - not a model's paraphrase of a legal rule.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import date

import psycopg
import pytest

from app.agents.compliance import (
    ComplianceGate,
    PolicyRule,
    RuleType,
    Severity,
)
from app.orchestrator.graph import (
    Orchestrator,
    _creative_copy,
    _licence_posture,
    build_graph,
)
from app.policy.rules import PolicyRuleLoader
from app.orchestrator.state import Intent, Mode
from conftest import OWNER, OWNER_OF, acting_as

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
WORKSPACE = "00000000-0000-4000-8000-000000000050"
# The seeded workspace that has never been synced from Meta - no
# t_advit.metrics_daily rows at all. Used where the absence is the point.
UNSYNCED_WORKSPACE = "00000000-0000-4000-8000-000000000051"


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="local Postgres is not running")


SCHEDULE_J = PolicyRule(
    code="IN_DMRA_SCHEDULE_J",
    jurisdiction="in",
    instrument="dmr_act_schedule_j",
    gate_stage=2,
    rule_type=RuleType.TERM_LIST,
    title="Schedule J prohibited condition",
    severity=Severity.BLOCK,
    explanation="needs_legal_verification: confirm against the current Schedule.",
    source_url="https://www.indiacode.nic.in/handle/123456789/1391",
    as_of=date(2026, 8, 1),
    terms=("piles", "bawasir"),
    remedy_template="Describe the category without naming the condition.",
    business_types=("ayurveda",),
)


class StubRouter:
    """Counts calls and bills a fixed amount, so a test can assert that a path
    spent nothing without needing a real key."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, role, *, system, user, **kw):
        from app.models.router import Completion

        self.calls.append(role)
        return Completion(
            text=f"[{role} narration]", model="stub/model", role=role,
            model_class="judgement", tokens_in=100, tokens_out=50,
            cost_usd=0.01, cost_inr=0.88, latency_ms=5, finish_reason="stop",
        )

    def complete_json(self, role, *, system, user, schema, **kw):
        from app.models.router import Completion

        self.calls.append(role)
        obj = {
            "goal": "raise budget",
            "assumptions": ["sales are genuinely up"],
            "options": [
                {"label": "A", "what": "straight to 20k", "expected_effect": "unstable",
                 "risk": "learning reset", "cost_of_being_wrong": "high"},
                {"label": "B", "what": "staged ramp", "expected_effect": "stable",
                 "risk": "slower", "cost_of_being_wrong": "low"},
            ],
            "recommended": "B",
            "single_strongest_reason": "no spend data to judge CAC",
            "questions": ["What is the trailing RTO rate?"],
        }
        return obj, Completion(
            text="", model="stub/model", role=role, model_class="judgement",
            tokens_in=200, tokens_out=100, cost_usd=0.02, cost_inr=1.76,
            latency_ms=5, finish_reason="stop",
        )


def build(router=None, rules=(SCHEDULE_J,)):
    orch = Orchestrator(
        router=router,
        gate_factory=lambda bt: ComplianceGate(list(rules)),
    )
    return build_graph(orch)


def run(graph, message: str, workspace: str = WORKSPACE, actor: str | None = None, **extra):
    """Invoke the graph the way the route does: under a bound tenant transaction.

    The orchestrator no longer holds a DSN. Its reads go through
    `current_tenant_tx()`, which raises when nothing is bound - so a test that
    forgot this would fail loudly rather than quietly reading every tenant's
    account context into a prompt, which is exactly the fallback the production
    code refuses to have.

    `actor` defaults to whoever owns the workspace, because running a workspace
    under the wrong principal is now a 404 rather than a silent cross-tenant
    read.
    """
    with acting_as(actor or OWNER_OF.get(workspace, OWNER)):
        return graph.invoke(
            # A real uuid: t_advit.decisions.run_id is a foreign key to
            # t_advit.runs, and open_run writes that row now.
            {"run_id": str(uuid.uuid4()), "workspace_id": workspace,
             "trigger": "user_message", "message": message, **extra}
        )


def spend(out) -> float:
    return sum(c.get("cost_inr", 0) for c in out.get("completions", []))


# ---------------------------------------------------------------------------
# Creative detection - what the gate is even allowed to look at
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Budget 5000 se 20000 kar do, sales achhi aa rahi hai",
        "kal ka CPL kyun badha?",
        "42 order aaye, 28 confirm",
        "",
    ],
)
def test_ordinary_conversation_carries_no_creative(message):
    """The gate adjudicates ad copy. Treating a budget question as copy blocks
    a legitimate conversation on a missing licence."""
    assert _creative_copy({"message": message}) is None


def test_quoted_copy_in_a_message_is_a_creative():
    copy = _creative_copy({"message": "Ye chala do: 'Piles ka permanent ilaj 7 din mein'"})
    assert copy.primary_text == "Piles ka permanent ilaj 7 din mein"
    assert copy.headline == "" and copy.description == ""


def test_an_explicit_creative_bundle_keeps_its_fields_apart():
    """This used to return one joined string, and the join is what let a clean
    headline and a clean primary text block each other."""
    copy = _creative_copy({
        "message": "run this",
        "creative": {"primary_text": "Bawasir ke liye upchar", "headline": "Ayurvedic"},
    })
    assert copy.primary_text == "Bawasir ke liye upchar"
    assert copy.headline == "Ayurvedic"


def test_a_short_quoted_aside_is_not_treated_as_copy():
    """Conservative on purpose: guessing produces false blocks, and a creative
    that was never submitted is caught later at the paused-first pre-flight."""
    assert _creative_copy({"message": "he said 'ok' and left"}) is None


# ---------------------------------------------------------------------------
# The block path must be free
# ---------------------------------------------------------------------------


def test_a_compliance_block_spends_nothing_on_models():
    """Before the gate was moved ahead of the paid agents, a blocked run cost
    roughly Rs 9 in judgement-tier tokens that were then discarded."""
    router = StubRouter()
    out = run(build(router), "Ye chala do: 'Piles ka permanent ilaj guaranteed'")

    assert out["compliance"]["verdict"] == "block"
    assert out["mode"] == Mode.HALTED.value
    assert router.calls == [], f"a blocked run called {router.calls}"
    assert spend(out) == 0


def test_a_block_is_rendered_deterministically_with_full_provenance():
    """The owner sees the rule, not a model's paraphrase of it."""
    out = run(build(StubRouter()), "Ye chala do: 'Piles ka permanent ilaj guaranteed'")
    narration = out["narration"]

    assert "dmr_act_schedule_j" in narration
    assert '"piles"' in narration.lower()
    assert "indiacode.nic.in" in narration
    assert "2026-08-01" in narration
    assert "needs legal verification" in narration


def test_a_block_names_the_stages_it_did_not_evaluate():
    """A block is not a clean bill of health for everything else."""
    out = run(build(StubRouter()), "Ye chala do: 'Bawasir ka ilaj, guaranteed result'")
    assert "not evaluated" in out["narration"]


def test_mode_is_populated_on_every_path():
    """A response whose mode is null is indistinguishable from one that never
    reached a decision."""
    blocked = run(build(StubRouter()), "Ye chala do: 'Piles ka ilaj'")
    clear = run(build(StubRouter()), "Budget badha do")
    assert blocked["mode"] == Mode.HALTED.value
    assert clear["mode"] is not None


# ---------------------------------------------------------------------------
# The clear path
# ---------------------------------------------------------------------------


def test_a_budget_question_is_not_blocked_by_the_creative_gate():
    """The regression that matters: no ad copy, so no creative gate."""
    out = run(build(StubRouter()), "Budget 5000 se 20000 kar do, sales achhi aa rahi hai")

    assert out["compliance"]["verdict"] == "not_applicable"
    assert "no creative" in out["compliance"]["reason"]
    assert out["mode"] != Mode.HALTED.value


def test_not_applicable_is_not_reported_as_a_pass():
    """The gate did not clear the bundle; there was no bundle to clear."""
    out = run(build(StubRouter()), "kal ka CPL kyun badha?")
    assert out["compliance"]["verdict"] == "not_applicable"
    assert out["compliance"]["verdict"] != "pass"


def test_a_proposal_returns_options_a_recommendation_and_one_reason():
    """PRD 5.3: 2-3 options with the trade-off in the owner's units, a stated
    recommendation, and the single strongest reason."""
    out = run(build(StubRouter()), "Budget badha do 20000 tak")

    proposal = out["proposal"]
    assert len(proposal["options"]) >= 2
    assert proposal["recommended"]
    assert proposal["single_strongest_reason"]
    assert out["mode"] == Mode.PROPOSED.value


def test_the_orchestrator_proposes_rather_than_executing():
    """Structural change is approval-gated at every autonomy level."""
    out = run(build(StubRouter()), "Budget badha do 20000 tak")
    assert out["mode"] != Mode.EXECUTED.value
    assert out.get("execution") is None


def test_intent_classification_is_deterministic_and_free():
    """Cheap deterministic work first: classification never calls a model."""
    router = StubRouter()
    out = run(build(router), "42 order aaye, 28 confirm, 9 cancel")
    assert out["intent"] == Intent.REPORT.value
    assert "classify" not in router.calls


# ---------------------------------------------------------------------------
# Facts and gaps
# ---------------------------------------------------------------------------


def test_facts_are_computed_before_any_agent_runs():
    out = run(build(StubRouter()), "kal ka CPL kyun badha?")
    assert "facts" in out
    assert out["facts"]["workspace"]["access_mode"] in ("full", "read_only", "denied")


def test_missing_spend_is_reported_as_a_gap_not_as_zero():
    """A blended CAC of zero because no spend was ingested is a data gap. Read
    as a number it says acquisition is free, which is the opposite of true.

    Deliberately run against the workspace that has never been synced. The
    primary seed now carries ingested metrics - because a workspace with none
    is not a state any real account stays in, and pretending otherwise is what
    let the monthly cap compare against a confident zero.

    Asserted on the property rather than the wording: the previous version
    matched the substring "gap", and passed for a while on a completely
    different gap than the one it names.
    """
    out = run(build(StubRouter()), "kal ka CPL kyun badha?", workspace=UNSYNCED_WORKSPACE)

    assert out.get("facts_gaps"), "an unmeasurable input must be named"
    economics = out.get("facts", {}).get("economics") or {}
    assert economics.get("blended_cac_inr") in (None, 0) or "spend" in " ".join(
        out["facts_gaps"]
    ).lower()
    assert economics.get("blended_cac_inr") != 0.0 or not economics, (
        "a zero CAC reported as a fact is the bug this test exists for"
    )


def test_retrieved_record_ids_are_carried_for_traceability():
    """Every claim about past performance must cite the memory record it came
    from (PRD Appendix A)."""
    out = run(build(StubRouter()), "kal ka CPL kyun badha?")
    assert out["retrieved_record_ids"]


def test_the_stable_prefix_is_assembled_for_caching():
    """The industry pack and account digest repeat on every call, which makes
    them the largest cost lever available."""
    out = run(build(StubRouter()), "kal ka CPL kyun badha?")
    assert "ACCOUNT CONTEXT" in out["stable_prefix"]
    assert "PLATFORM KNOWLEDGE" in out["stable_prefix"]


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_no_model_key_degrades_to_facts_rather_than_failing():
    """Quality degrades, availability does not. Every deterministic path still
    works without a key."""
    out = run(build(router=None), "kal ka CPL kyun badha?")
    assert out["narration"]
    assert "Narration unavailable" in out["narration"]
    assert spend(out) == 0


def test_a_failing_model_does_not_take_the_run_down():
    class Failing(StubRouter):
        def complete(self, role, **kw):
            from app.models.router import AllModelsFailed
            raise AllModelsFailed(role, [("stub", "provider down")])

    out = run(build(Failing()), "kal ka CPL kyun badha?")
    assert out["narration"]
    assert out["mode"] is not None


def test_events_form_an_activity_trace():
    """The same typed stream feeds the UI tracker, the reasoning panel and the
    trace - it is not a decorative animation (PRD 7.5)."""
    out = run(build(StubRouter()), "Budget badha do")
    events = out["events"]
    assert events
    assert all("agent" in e and "event" in e for e in events)
    assert any(e["event"].startswith("agent.") for e in events)


# ---------------------------------------------------------------------------
# The compliance bundle, built from trustworthy sources
#
# Four faces of one defect: the node assembled its bundle from a joined string,
# from a tenant-writable table, from a hard-coded default and from the request
# body. Each is reproduced below in the form it was found in.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded_rules() -> PolicyRuleLoader:
    """The ruleset that actually ships.

    The stub ruleset at the top of this file cannot exercise pack scoping - it
    is handed to the gate whatever the business type - and pack scoping is
    exactly what these tests are about.
    """
    return PolicyRuleLoader()


def build_from_db(loader: PolicyRuleLoader, router=None):
    """A graph whose gate is built from the seeded pack for whichever business
    type the node selects. Pick the wrong pack and the wrong rules load, which
    is what makes the tests below able to see it."""
    orch = Orchestrator(
        router=router,
        gate_factory=lambda bt: ComplianceGate(list(loader.load(bt))),
    )
    return build_graph(orch)


@contextmanager
def account_context_row(workspace: str, key: str, value: str):
    """Write a row to t_advit.account_context, then remove it.

    Committed rather than rolled back, because the orchestrator opens its own
    connections and would not see an open transaction - which is precisely the
    property that made the injection work against the running product.
    """
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.account_context
              (workspace_id, dimension, key, value_json, confidence, source)
            values (%s, 'compliance', %s, to_jsonb(%s::text), 0.999, 'owner_asserted')
            returning id::text
            """,
            (workspace, key, value),
        )
        row_id = cur.fetchone()[0]
        conn.commit()
    try:
        yield row_id
    finally:
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("delete from t_advit.account_context where id = %s", (row_id,))
            conn.commit()


@contextmanager
def extra_product(workspace: str, sku: str, **columns):
    """A second SKU in the workspace, so the many-products case is real rather
    than hypothetical. The seed ships exactly one."""
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.catalog_products
              (workspace_id, sku, name, price_inr, margin_rate,
               classification, ayush_licence_no)
            values (%s, %s, %s, 1499.00, 0.55, %s, %s)
            returning id::text
            """,
            (workspace, sku, columns.get("name", sku),
             columns.get("classification"), columns.get("ayush_licence_no")),
        )
        row_id = cur.fetchone()[0]
        conn.commit()
    try:
        yield row_id
    finally:
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("delete from t_advit.catalog_products where id = %s", (row_id,))
            conn.commit()


# -- face 1: the fields were joined -----------------------------------------


def test_a_clean_headline_cannot_complete_a_timeline_claim_in_a_clean_primary_text(
    seeded_rules,
):
    """The reproduction, exactly as found.

    "Order within 7 days" promises a delivery date and names no outcome.
    "Relief for the whole family" names an outcome and no duration. Each clears
    META_OUTCOME_TIMELINE alone. Joined with a space, the rule's proximity
    window bridges the seam and reads a promise of relief in seven days - a
    BLOCK, which short-circuits the entire run to halted. CreativeBundle
    .copy_fields() exists precisely so the fields stay apart.
    """
    out = run(
        build_from_db(seeded_rules, StubRouter()),
        "chala do",
        creative={
            "primary_text": "Order within 7 days",
            "headline": "Relief for the whole family",
            "product_sku": "PC-001",
        },
    )
    codes = {f["rule_code"] for f in out["compliance"]["findings"]}
    assert "META_OUTCOME_TIMELINE" not in codes, (
        "a proximity window bridged two fields that are never adjacent in the ad"
    )
    # Asserted on the Meta layer rather than on `mode`. The seeded product still
    # carries no AYUSH licence, so this bundle is legitimately blocked on the
    # India layer - and a test that watched `mode` would have passed on the
    # wrong block.
    assert out["compliance"]["meta_layer"] != "block"


def test_a_finding_names_the_field_its_span_was_found_in(seeded_rules):
    """Every span used to be reported against primary_text, because every field
    WAS primary_text by the time the gate saw it. An owner cannot fix a span
    they cannot locate."""
    out = run(
        build_from_db(seeded_rules, StubRouter()),
        "chala do",
        creative={"primary_text": "Traditional care", "headline": "Bawasir ka upchar",
                  "product_sku": "PC-001"},
    )
    finding = next(
        f for f in out["compliance"]["findings"] if f["rule_code"] == "IN_DMRA_SCHEDULE_J"
    )
    assert finding["field"] == "headline"


def test_a_bulleted_shipping_line_beside_a_benefit_line_is_not_a_timeline_claim(
    seeded_rules,
):
    """The residual half of the same window, inside ONE field.

    Ad copy is written in short lines, and a newline is not a sentence
    terminator, so "- Ships in 3 days" followed by "- Relief-focused formula"
    read as a promise of relief in three days until the windows were narrowed
    from [^.!?] to [^.!?\\n].
    """
    out = run(
        build_from_db(seeded_rules, StubRouter()),
        "chala do",
        creative={
            "primary_text": "- Ships in 3 days\n- Relief-focused formula",
            "product_sku": "PC-001",
        },
    )
    codes = {f["rule_code"] for f in out["compliance"]["findings"]}
    assert "META_OUTCOME_TIMELINE" not in codes


# -- face 2: the ruleset was selected from a tenant-writable table -----------


def test_writing_a_business_type_into_account_context_does_not_change_the_verdict(
    seeded_rules,
):
    """t_advit.account_context is tenant-writable - account_context_write lets
    ANY workspace member insert. The node used to scan it for a row keyed
    'business_type', so one INSERT valued 'general_d2c' switched the Indian
    statutory layer off for the whole workspace: Schedule J and the AYUSH
    licence check both vanished from a creative that plainly names bawasir.

    A value the caller can write must never decide which rules constrain the
    caller.
    """
    graph = build_from_db(seeded_rules, StubRouter())
    creative = {"primary_text": "Bawasir ke liye ayurvedic upchar", "product_sku": "PC-001"}

    before = run(graph, "chala do", creative=creative)
    with account_context_row(WORKSPACE, "business_type", "general_d2c"):
        after = run(graph, "chala do", creative=creative)

    assert before["compliance"]["verdict"] == "block"
    assert after["compliance"]["verdict"] == "block"
    assert after["compliance"]["business_type"] == "ayurveda"
    assert any(
        f["rule_code"] == "IN_DMRA_SCHEDULE_J" for f in after["compliance"]["findings"]
    ), "an injected account_context row disabled the statutory layer"


# -- face 3: the hard-coded default always won ------------------------------


def test_a_general_d2c_workspace_is_not_adjudicated_under_the_ayurveda_pack(
    seeded_rules,
):
    """The repo's own canonical false positive.

    assemble_context SELECTed w.business_type and did not return it, so the
    literal default "ayurveda" won for every workspace in the product - and
    account_context held no row with that key, so nothing ever displaced it.
    "We cleared our piles of stock this week" from a General D2C advertiser
    blocked on the Drugs & Magic Remedies Act. tests/test_api.py already asserts
    that this exact sentence must pass for general_d2c through the ad-hoc
    endpoint; through the orchestrator it did not.
    """
    out = run(
        build_from_db(seeded_rules, StubRouter()),
        "chala do",
        workspace=UNSYNCED_WORKSPACE,
        creative={"primary_text": "We cleared our piles of stock this week"},
    )

    assert out["compliance"]["business_type"] == "general_d2c"
    codes = {f["rule_code"] for f in out["compliance"]["findings"]}
    assert "IN_DMRA_SCHEDULE_J" not in codes
    assert "IN_AYUSH_LICENCE_ON_FILE" not in codes
    assert out["compliance"]["verdict"] != "block"
    assert out["mode"] != Mode.HALTED.value


def test_a_workspace_whose_business_type_cannot_be_read_is_refused_not_guessed(
    seeded_rules,
):
    """A workspace that does not exist still reaches the compliance node -
    assemble_context returns errors and the graph has no conditional edge around
    it. Under the old default that unknown workspace was adjudicated under the
    DMR Act.

    A guessed pack is wrong in both directions at once, so the gate refuses to
    certify instead. Refusing to certify is not halting the owner's turn.
    """
    out = run(
        build_from_db(seeded_rules, StubRouter()),
        "chala do",
        workspace="11111111-1111-4111-8111-111111111111",
        creative={"primary_text": "Bawasir ke liye upchar"},
    )
    assert out["compliance"]["verdict"] == "not_evaluated"
    assert out["compliance"]["findings"] == []
    # The run still halts, on the missing access mode - which is the correct
    # reason and a different one. What must not happen is a compliance block,
    # and the deterministic block rendering is how that would show.
    assert "dmr_act_schedule_j" not in out["narration"]


# -- face 4: the licence came from the request body -------------------------


def test_a_licence_number_in_the_request_cannot_clear_the_stage_nine_block(
    seeded_rules,
):
    """IN_AYUSH_LICENCE_ON_FILE is severity BLOCK and reads exactly two strings.
    Both arrived on the HTTP request body, so posting
    ayush_licence_no="FAKE-NOT-A-LICENCE" cleared a statutory block while the
    workspace's real catalog_products row held NULL and 'unverified'. /api/chat
    has no authentication at all.

    The state now comes from the catalogue, and the run input is not consulted
    for a licence number at all - the keys below are inert.
    """
    out = run(
        build_from_db(seeded_rules, StubRouter()),
        "chala do",
        creative={"primary_text": "A traditional preparation", "product_sku": "PC-001"},
        ayush_licence_no="FAKE-NOT-A-LICENCE",
        product_classification="ayurvedic_drug",
    )
    finding = next(
        f for f in out["compliance"]["findings"]
        if f["rule_code"] == "IN_AYUSH_LICENCE_ON_FILE"
    )
    assert "AYUSH licence number" in finding["offending_span"]
    assert "PC-001" in finding["offending_span"]
    assert out["compliance"]["licence_posture"]["source"] == "catalogue"


def test_the_governing_product_is_named_in_the_response(seeded_rules):
    """Why did it say that must be answerable from the response, not from the
    logs. The seeded workspace holds exactly one SKU, so there is nothing else
    the creative could be advertising and the gate says which one it read."""
    out = run(
        build_from_db(seeded_rules, StubRouter()),
        "chala do",
        creative={"primary_text": "A traditional preparation"},
    )
    assert out["compliance"]["licence_posture"] == {
        "source": "catalogue", "sku": "PC-001", "resolved": True,
        "unresolved_reason": None, "candidate_skus": [],
    }


def test_a_second_product_stops_the_gate_certifying_a_creative_that_names_none(
    seeded_rules,
):
    """The hard case: a workspace holds many products and the bundle carries no
    product id. products[0] used to decide it, so row order chose whether a
    licensed SKU vouched for an unlicensed one. With two products and no name
    the gate cannot tell which is being advertised, so stage 9 is reported
    unevaluated - which is not a pass, and is not a block either."""
    graph = build_from_db(seeded_rules, StubRouter())
    with extra_product(WORKSPACE, "PC-002", classification="ayurvedic_drug",
                       ayush_licence_no="UP-AYUR-99999"):
        out = run(graph, "chala do",
                  creative={"primary_text": "A traditional preparation"})

    posture = out["compliance"]["licence_posture"]
    assert posture["resolved"] is False
    assert sorted(posture["candidate_skus"]) == ["PC-001", "PC-002"]
    assert 9 in out["compliance"]["stages_skipped"]
    assert out["compliance"]["verdict"] == "not_evaluated"


def test_naming_the_product_resolves_that_products_licence_and_no_other(seeded_rules):
    """And the point of naming it: in one workspace, on one turn, the licensed
    SKU clears and the unlicensed one does not."""
    graph = build_from_db(seeded_rules, StubRouter())
    with extra_product(WORKSPACE, "PC-002", classification="ayurvedic_drug",
                       ayush_licence_no="UP-AYUR-99999"):
        licensed = run(graph, "chala do", creative={
            "primary_text": "A traditional preparation", "product_sku": "PC-002"})
        unlicensed = run(graph, "chala do", creative={
            "primary_text": "A traditional preparation", "product_sku": "PC-001"})

    assert not any(f["rule_code"] == "IN_AYUSH_LICENCE_ON_FILE"
                   for f in licensed["compliance"]["findings"])
    assert any(f["rule_code"] == "IN_AYUSH_LICENCE_ON_FILE"
               for f in unlicensed["compliance"]["findings"])


# -- the resolver itself, without a database --------------------------------


def test_a_sku_that_is_not_in_the_catalogue_never_falls_through_to_another():
    """Falling through is how a licensed SKU comes to vouch for a product that
    is not on file at all."""
    posture = _licence_posture(
        [{"sku": "PC-001", "ayush_licence_no": "UP-1", "classification": "ayurvedic_drug"}],
        "SOMEONE-ELSES-SKU",
    )
    assert posture.resolved is False
    assert posture.ayush_licence_no is None


def test_an_empty_catalogue_is_a_gap_not_a_clean_licence_posture():
    """No rows is a data gap, not a zero. A guard with nothing to compare
    against refuses rather than finding nothing wrong."""
    posture = _licence_posture([], None)
    assert posture.resolved is False
    assert "no product is on file" in posture.unresolved_reason
