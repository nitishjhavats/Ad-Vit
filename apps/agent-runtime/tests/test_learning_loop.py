"""The closed loop: predict, measure, learn.

``t_advit.decisions`` has always recorded ``expected_effect_json`` and
``horizon_days`` before anything executed, and ``PostgresOutcomeScheduler`` has
always queued a placeholder ``t_advit.outcomes`` row so the obligation to measure
survived a restart. Nothing ever measured one, and no ``t_advit.learnings`` row
had ever been written by any code path — all three memory tiers had zero writers.
The product's central claim had a schema and no implementation.

It could not have worked as designed either: the prediction was PROSE
(``{"expected_effect": "CAC should come down"}``), so there was nothing to
compare a result against. Every outcome would have been ``unmeasurable`` forever.

The property these tests exist to hold is the one the rest of this repository
keeps failing: **``unmeasurable`` is not ``met``**. There are five separate ways
a measurement can fail to happen, and a loop that scored any of them as success
would grow more confident the less it knew.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import psycopg
import pytest

from app.learning import outcomes as oc
from app.learning.metrics import PREDICTABLE, METRICS, Direction, resolve
from app.learning.promote import MIN_EVIDENCE, Claim, promote
import os

from conftest import SERVICE_DSN

# The superuser connection, for FIXTURE SETUP AND CLEANUP ONLY.
#
# advit_service holds select/insert/update on every t_advit table and
# deliberately NO DELETE - 20260911000007 says it out loud: "the runtime
# corrects rows; it never removes tenant history". So the code under test
# runs on the service credential and the scrub cannot. Test setup may be
# privileged; the code under test may not.
SUPERUSER_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
)

WORKSPACE = "00000000-0000-4000-8000-000000000050"
TODAY = date(2026, 9, 12)
EXECUTED_ON = date(2026, 9, 5)      # a 7-day horizon ending 2026-09-12


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_every_predictable_metric_can_actually_be_measured():
    """The enum handed to the model and the SQL that reads it live in one file
    for this reason. If they drifted, a model could name a metric nothing knows
    how to read and every outcome for it would come back unmeasurable — which
    looks like a data gap rather than like a schema mistake."""
    for key in PREDICTABLE:
        assert resolve(key) is not None, key


@pytest.mark.parametrize("key", sorted(PREDICTABLE))
def test_each_metric_query_runs_and_names_its_source(key):
    """Executed for real, so a typo in one branch of the registry cannot hide
    behind the others."""
    metric = METRICS[key]
    with psycopg.connect(SERVICE_DSN, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                metric.sql,
                {"workspace": WORKSPACE, "start": EXECUTED_ON, "end": TODAY},
            )
            row = cur.fetchone()
    assert "value" in row and "days" in row
    assert metric.source in {"business_truth", "platform"}


def test_business_truth_and_platform_metrics_are_never_mixed():
    """A number that is half the owner's orders and half Meta's report is
    neither, and the whole thesis of this product is that the two differ and
    business truth wins."""
    for metric in METRICS.values():
        reads_blended = "blended_daily" in metric.sql
        reads_platform = "metrics_daily" in metric.sql
        assert reads_blended != reads_platform, f"{metric.key} reads both sources"


# ---------------------------------------------------------------------------
# measure(): the five ways a measurement does not happen
# ---------------------------------------------------------------------------


def prediction(metric="blended_cac_inr", direction="down", target=None):
    body = {"metric": metric, "direction": direction}
    if target is not None:
        body["target"] = target
    return {"expected_effect": "prose for the owner", "prediction": body}


@pytest.fixture
def cur():
    with psycopg.connect(SERVICE_DSN, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as c:
            yield c
        conn.rollback()


def test_a_horizon_that_has_not_elapsed_is_unmeasurable(cur):
    """Not a failure — it is simply not time yet. Reported so the row is honest
    to anyone reading it today, and the job finds it again tomorrow."""
    result = oc.measure(
        cur, workspace_id=WORKSPACE, executed_on=TODAY, horizon_days=7,
        expected_effect=prediction(), today=TODAY,
    )
    assert result.verdict == oc.UNMEASURABLE
    assert "horizon ends" in result.reason


def test_a_prose_only_prediction_is_unmeasurable(cur):
    """Every decision written before the structured prediction existed. The
    prediction can be READ and cannot be CHECKED, and saying so is the whole
    point of this verdict."""
    result = oc.measure(
        cur, workspace_id=WORKSPACE, executed_on=EXECUTED_ON, horizon_days=7,
        expected_effect={"expected_effect": "CAC should come down"}, today=TODAY,
    )
    assert result.verdict == oc.UNMEASURABLE
    assert "no machine-readable prediction" in result.reason


def test_an_unknown_metric_is_unmeasurable(cur):
    result = oc.measure(
        cur, workspace_id=WORKSPACE, executed_on=EXECUTED_ON, horizon_days=7,
        expected_effect=prediction(metric="vibes"), today=TODAY,
    )
    assert result.verdict == oc.UNMEASURABLE


def test_no_baseline_is_unmeasurable_rather_than_met(cur):
    """The account has no blended_daily rows at all, so there is nothing to have
    moved FROM. Scoring this as met would be the defect this whole file is about:
    a prediction graded against an absence."""
    result = oc.measure(
        cur, workspace_id=WORKSPACE, executed_on=EXECUTED_ON, horizon_days=7,
        expected_effect=prediction(), today=TODAY,
    )
    assert result.verdict == oc.UNMEASURABLE
    assert "no baseline" in result.reason or "no blended CAC" in result.reason


# ---------------------------------------------------------------------------
# measure(): and the four ways it does
# ---------------------------------------------------------------------------


def write_cac(cur, days: dict[date, float | None]):
    """blended_daily rows. A None writes the NULL that compute_blended_daily
    writes when a day cannot be computed — which the registry must skip rather
    than average as zero."""
    for day, value in days.items():
        cur.execute(
            """
            insert into t_advit.blended_daily (date, workspace_id, blended_cac_inr)
            values (%s::date, %s::uuid, %s)
            on conflict (workspace_id, date) do update
               set blended_cac_inr = excluded.blended_cac_inr
            """,
            (day, WORKSPACE, value),
        )


def window(before: float, after: float) -> dict[date, float]:
    days = {}
    for i in range(1, 8):
        days[EXECUTED_ON - timedelta(days=i)] = before
    for i in range(0, 7):
        days[EXECUTED_ON + timedelta(days=i)] = after
    return days


def test_a_move_in_the_predicted_direction_with_no_target_is_met(cur):
    """`met`, never `beat`. Without a target, "beat" would mean "moved further
    than some noise floor I invented", and a fabricated threshold here becomes a
    fabricated confidence in a prompt later."""
    write_cac(cur, window(before=500.0, after=400.0))
    result = oc.measure(
        cur, workspace_id=WORKSPACE, executed_on=EXECUTED_ON, horizon_days=7,
        expected_effect=prediction(), today=TODAY,
    )
    assert result.verdict == oc.MET
    assert "no target was stated" in result.reason


def test_reaching_a_stated_target_beats_it(cur):
    write_cac(cur, window(before=500.0, after=400.0))
    result = oc.measure(
        cur, workspace_id=WORKSPACE, executed_on=EXECUTED_ON, horizon_days=7,
        expected_effect=prediction(target=450.0), today=TODAY,
    )
    assert result.verdict == oc.BEAT


def test_moving_the_right_way_but_short_of_the_target_is_only_met(cur):
    write_cac(cur, window(before=500.0, after=470.0))
    result = oc.measure(
        cur, workspace_id=WORKSPACE, executed_on=EXECUTED_ON, horizon_days=7,
        expected_effect=prediction(target=450.0), today=TODAY,
    )
    assert result.verdict == oc.MET
    assert "did not reach" in result.reason


def test_moving_against_the_prediction_is_missed(cur):
    write_cac(cur, window(before=400.0, after=520.0))
    result = oc.measure(
        cur, workspace_id=WORKSPACE, executed_on=EXECUTED_ON, horizon_days=7,
        expected_effect=prediction(), today=TODAY,
    )
    assert result.verdict == oc.MISSED
    assert "against the predicted down" in result.reason


def test_a_null_day_is_skipped_rather_than_averaged_as_zero(cur):
    """compute_blended_daily writes NULL and names the reason in `gaps` when a
    day cannot be computed — that was the whole point of 20260911000002.
    Counting a NULL as zero here would undo it one layer up, and would do it in
    the permissive direction: a NULL in the after-window drags the average down,
    which looks exactly like the CAC improvement being predicted."""
    days = window(before=500.0, after=400.0)
    days[EXECUTED_ON + timedelta(days=3)] = None
    write_cac(cur, days)

    result = oc.measure(
        cur, workspace_id=WORKSPACE, executed_on=EXECUTED_ON, horizon_days=7,
        expected_effect=prediction(), today=TODAY,
    )
    assert result.after.value == pytest.approx(400.0)
    assert result.after.days == 6, "the NULL day should not count as a measured day"


def test_the_comparison_windows_do_not_overlap(cur):
    """Half-open [start, end), so the day the action executed belongs to the
    after-window and to nothing else. An inclusive bound would put the day of
    the change on both sides and damp every measured effect."""
    days = {EXECUTED_ON - timedelta(days=1): 500.0, EXECUTED_ON: 400.0}
    write_cac(cur, days)
    result = oc.measure(
        cur, workspace_id=WORKSPACE, executed_on=EXECUTED_ON, horizon_days=7,
        expected_effect=prediction(), today=TODAY,
    )
    assert result.before.days == 1 and result.before.value == pytest.approx(500.0)
    assert result.after.days == 1 and result.after.value == pytest.approx(400.0)


def test_vs_expected_carries_enough_to_re_derive_the_verdict(cur):
    """The metric tables move on. A verdict nobody can check a month later is a
    number, not a record."""
    write_cac(cur, window(before=500.0, after=400.0))
    result = oc.measure(
        cur, workspace_id=WORKSPACE, executed_on=EXECUTED_ON, horizon_days=7,
        expected_effect=prediction(target=450.0), today=TODAY,
    )
    payload = result.as_vs_expected()
    for key in ("verdict", "reason", "metric", "source", "predicted_direction",
                "target", "before", "after", "delta"):
        assert key in payload, key
    assert payload["delta"] == pytest.approx(-100.0)


# ---------------------------------------------------------------------------
# promote(): one observation is not a learning
# ---------------------------------------------------------------------------


def claim(supported: int, total: int, delta: float | None = -80.0) -> Claim:
    return Claim(
        decision_type="update_budget",
        metric_key="blended_cac_inr",
        direction="down",
        supported=supported,
        total=total,
        mean_delta=delta,
        evidence_refs=tuple(str(uuid.uuid4()) for _ in range(total)),
    )


def test_two_for_two_does_not_claim_certainty():
    """Laplace-smoothed: (supported + 1) / (total + 2), the posterior mean of a
    Beta(1,1) prior. Two for two reads 0.75, not 1.0 — which is the right amount
    of doubt to carry into a prompt, and the reason this is not supported/total."""
    assert claim(2, 2).confidence == pytest.approx(0.75)
    assert claim(1, 1).confidence == pytest.approx(2 / 3)
    assert claim(9, 10).confidence == pytest.approx(10 / 12)


def test_a_claim_that_mostly_fails_is_still_a_claim():
    """Recorded as what it is. A learning is not only a thing that worked, and
    keeping only the successes is how a system talks itself into a strategy."""
    failed = claim(0, 4)
    assert failed.status == "active"
    assert "did NOT move" in failed.statement()


def test_evidence_that_genuinely_disagrees_is_contested():
    assert claim(2, 4).status == "contested"
    assert claim(3, 4).status == "active"


def test_a_statement_reads_like_a_sentence_about_this_account():
    text = claim(3, 4).statement()
    assert "update budget" in text
    assert "blended CAC" in text
    assert "3 of 4" in text


# ---------------------------------------------------------------------------
# promote(): against the database
# ---------------------------------------------------------------------------


FIXTURE_MARK = "learning loop fixture"


def _scrub():
    """Delete everything these tests write, by MARKER rather than by tracked id.

    Cleaning before as well as after, which is not belt-and-braces. These rows
    have to be COMMITTED - `promote` opens its own service connection and would
    not see an open transaction - so a test that dies between the insert and the
    teardown leaves them behind, and the next run's `evidence_n` is then computed
    over somebody else's outcomes. That happened while writing this file: a
    crashed teardown left 19 outcomes, and a two-for-two claim came back with
    evidence_n = 17.

    Deleting decisions is enough for outcomes, approvals and actions, which are
    all `on delete cascade` from it. Learnings are not, so they go explicitly.
    """
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "delete from t_advit.learnings where workspace_id = %s::uuid", (WORKSPACE,)
        )
        cur.execute(
            "delete from t_advit.decisions where workspace_id = %s::uuid and reasoning = %s",
            (WORKSPACE, FIXTURE_MARK),
        )
        conn.commit()


@pytest.fixture
def measured_outcomes():
    """Decisions with measured outcomes, committed, and scrubbed either side.

    Committed rather than held in a transaction, because `promote` opens its own
    service connection and would not see uncommitted rows.
    """
    _scrub()

    def build(verdicts: list[str], *, decision_type: str = "update_budget") -> None:
        with psycopg.connect(SERVICE_DSN, row_factory=psycopg.rows.dict_row) as conn:
            with conn.cursor() as cur:
                for i, verdict in enumerate(verdicts):
                    cur.execute(
                        """
                        insert into t_advit.decisions
                          (workspace_id, decision_type, reasoning,
                           expected_effect_json, horizon_days, confidence)
                        values (%s::uuid, %s, %s, %s::jsonb, 7, 0.6)
                        returning id::text
                        """,
                        (
                            WORKSPACE,
                            decision_type,
                            FIXTURE_MARK,
                            json.dumps(prediction(target=450.0)),
                        ),
                    )
                    decision_id = cur.fetchone()["id"]
                    cur.execute(
                        """
                        insert into t_advit.outcomes
                          (decision_id, workspace_id, horizon_days, verdict,
                           metrics_json, vs_expected, notes)
                        values (%s::uuid, %s::uuid, 7,
                                %s::t_advit.outcome_verdict, '{}'::jsonb,
                                %s::jsonb, 'fixture')
                        """,
                        (
                            decision_id,
                            WORKSPACE,
                            verdict,
                            json.dumps(
                                {
                                    "metric": "blended_cac_inr",
                                    "predicted_direction": "down",
                                    "delta": -80.0 - i,
                                }
                            ),
                        ),
                    )
            conn.commit()

    yield build

    _scrub()


def learnings_for(workspace: str) -> list[dict]:
    with psycopg.connect(SERVICE_DSN, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select statement, confidence, evidence_n, status::text as status,
                       effect_size, conditions_json
                  from t_advit.learnings
                 where workspace_id = %s::uuid and tier = 'account'
                """,
                (workspace,),
            )
            return cur.fetchall()


def test_one_measured_outcome_writes_no_learning(measured_outcomes):
    """The outcome row is the whole record: the system did a thing once and saw a
    result once. Writing a learning at n=1 produces a memory indistinguishable in
    SHAPE from a well-evidenced one, which will then be retrieved into a prompt
    as though it were."""
    measured_outcomes(["met"])
    result = promote(WORKSPACE)
    assert result["written"] == []
    assert learnings_for(WORKSPACE) == []


def test_two_agreeing_outcomes_write_one_learning(measured_outcomes):
    measured_outcomes(["met", "beat"])
    result = promote(WORKSPACE)

    assert len(result["written"]) == 1
    rows = learnings_for(WORKSPACE)
    assert len(rows) == 1
    row = rows[0]
    assert row["evidence_n"] == 2
    assert float(row["confidence"]) == pytest.approx(0.75)
    assert row["status"] == "active"
    assert row["conditions_json"]["metric"] == "blended_cac_inr"


def test_unmeasurable_outcomes_are_excluded_from_the_denominator(measured_outcomes):
    """`unmeasurable` means nobody looked, or there was nothing to look at.
    Folding it into the denominator would make the system less confident the
    worse its own data pipeline was — a bias in the wrong direction, and
    invisible once averaged."""
    measured_outcomes(["met", "met", "unmeasurable", "unmeasurable"])
    promote(WORKSPACE)

    rows = learnings_for(WORKSPACE)
    assert len(rows) == 1
    assert rows[0]["evidence_n"] == 2, "unmeasurable outcomes were counted as evidence"
    assert float(rows[0]["confidence"]) == pytest.approx(0.75)


def test_promoting_twice_sharpens_one_row_instead_of_duplicating(measured_outcomes):
    """The natural key added by 20260912000001. A learning that exists eleven
    times with eleven confidences is not eleven learnings; it is one, recorded
    badly."""
    measured_outcomes(["met", "met"])
    first = promote(WORKSPACE)
    assert first["written"][0]["inserted"] is True

    second = promote(WORKSPACE)
    assert second["written"][0]["inserted"] is False
    assert len(learnings_for(WORKSPACE)) == 1


def test_more_evidence_raises_confidence_on_the_same_row(measured_outcomes):
    measured_outcomes(["met", "met"])
    promote(WORKSPACE)
    before = learnings_for(WORKSPACE)[0]

    measured_outcomes(["met", "met", "met"])
    promote(WORKSPACE)
    after = learnings_for(WORKSPACE)[0]

    assert len(learnings_for(WORKSPACE)) == 1
    assert after["evidence_n"] > before["evidence_n"]
    assert float(after["confidence"]) > float(before["confidence"])


def test_evidence_never_goes_backwards(measured_outcomes):
    """Monotonic on purpose. Evidence is a count of things that happened, and a
    tick that looked at fewer rows — a LIMIT, a partial sync — must not be able
    to make the account look less experienced than it is."""
    measured_outcomes(["met", "met", "met"])
    promote(WORKSPACE)
    full = learnings_for(WORKSPACE)[0]["evidence_n"]

    promote(WORKSPACE, min_evidence=MIN_EVIDENCE)
    assert learnings_for(WORKSPACE)[0]["evidence_n"] >= full


def test_a_learning_is_written_on_the_connection_a_tenant_is_not():
    """`authenticated` holds SELECT on learnings and nothing else, so an agent —
    or a browser — cannot edit what the system has supposedly learned about
    them, and therefore cannot edit what it proposes to them next."""
    from conftest import OWNER, TENANT_DSN, claims_for

    with psycopg.connect(TENANT_DSN) as conn:
        conn.autocommit = False
        conn.execute("select 1")
        with conn.cursor() as cur:
            cur.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps(claims_for(OWNER)),),
            )
            cur.execute("set local role authenticated")
            # A savepoint, not a nested transaction block: the INSERT aborts the
            # transaction, and unwinding a `conn.transaction()` over an aborted
            # one is what turned this passing assertion into a failing test.
            cur.execute("savepoint probe")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "insert into t_advit.learnings (workspace_id, tier, statement) "
                    "values (%s::uuid, 'account', 'forged')",
                    (WORKSPACE,),
                )
            cur.execute("rollback to savepoint probe")
        conn.rollback()


# ---------------------------------------------------------------------------
# Retrieval: memory nothing reads is write-only
# ---------------------------------------------------------------------------


def seed_learning(workspace: str, statement: str, *, tier: str = "account",
                  confidence: float = 0.75, status: str = "active") -> str:
    with psycopg.connect(SUPERUSER_DSN, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.learnings
                  (workspace_id, tier, statement, conditions_json, confidence,
                   evidence_n, status)
                values (%s, %s::t_advit.knowledge_tier, %s,
                        %s::jsonb, %s, 3, %s::t_advit.learning_status)
                returning id::text
                """,
                (
                    workspace if tier == "account" else None,
                    tier,
                    statement,
                    json.dumps(
                        {
                            "decision_type": "update_budget",
                            "metric": "blended_cac_inr",
                            "direction": "down",
                        }
                    ),
                    confidence,
                    status,
                ),
            )
            learning_id = cur.fetchone()["id"]
        conn.commit()
    return learning_id


@pytest.fixture
def seeded_learnings():
    made: list[str] = []

    def build(**kw) -> str:
        made.append(seed_learning(**kw))
        return made[-1]

    yield build

    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        if made:
            cur.execute("delete from t_advit.learnings where id = any(%s::uuid[])", (made,))
        conn.commit()


def context_for(user: str, workspace: str) -> dict:
    """Run assemble_context the way the chat route does, under a real principal."""
    from app.agents.compliance import ComplianceGate
    from app.orchestrator.graph import Orchestrator
    from conftest import acting_as

    orch = Orchestrator(router=None, gate_factory=lambda bt: ComplianceGate([]))
    with acting_as(user):
        return orch.assemble_context({"workspace_id": workspace})


def test_an_account_tier_learning_reaches_its_own_prompt(seeded_learnings):
    """Otherwise the loop is write-only: measured, promoted, and never consulted
    by the thing making the next proposal."""
    from conftest import BROADMATE_WORKSPACE, OWNER

    seeded_learnings(workspace=BROADMATE_WORKSPACE,
                     statement="Raising budget moved CAC down here")
    ctx = context_for(OWNER, BROADMATE_WORKSPACE)

    statements = [r["statement"] for r in ctx["learnings"]]
    assert "Raising budget moved CAC down here" in statements
    assert "Raising budget moved CAC down here" in ctx["stable_prefix"]


def test_one_accounts_learning_never_reaches_anothers_prompt(seeded_learnings):
    """The whole point of tier 1. This is hyperpersonalisation, and a tier-1
    memory that crossed accounts would be the worst kind of leak - not a row a
    tenant could query, but another tenant's hard-won conclusion quietly shaping
    the advice this one is given."""
    from conftest import BROADMATE_WORKSPACE, OUTSIDER, RIVAL_WORKSPACE

    seeded_learnings(workspace=BROADMATE_WORKSPACE,
                     statement="Broadmate private conclusion")
    ctx = context_for(OUTSIDER, RIVAL_WORKSPACE)

    statements = [r["statement"] for r in ctx["learnings"]]
    assert "Broadmate private conclusion" not in statements
    assert "Broadmate private conclusion" not in ctx["stable_prefix"]


def test_a_shared_tier_learning_reaches_everybody(seeded_learnings):
    """Tiers 2 and 3 are the product: what every account in an industry taught
    the system. They carry no workspace_id at all (learnings_tier_scoping
    enforces it), so there is nothing to scope and nothing to leak."""
    from conftest import OUTSIDER, RIVAL_WORKSPACE

    seeded_learnings(workspace=RIVAL_WORKSPACE, tier="global",
                     statement="Global: paused-first reduces rejections")
    ctx = context_for(OUTSIDER, RIVAL_WORKSPACE)

    assert "Global: paused-first reduces rejections" in ctx["stable_prefix"]


def test_confidence_and_evidence_travel_into_the_prompt(seeded_learnings):
    """promote.py Laplace-smooths confidence precisely so a thin claim reads
    thin. Rendering a 0.55 claim and a 0.95 claim as the same flat sentence
    throws away the only thing that doubt is recorded for."""
    from conftest import BROADMATE_WORKSPACE, OWNER

    seeded_learnings(workspace=BROADMATE_WORKSPACE,
                     statement="A thin claim", confidence=0.55)
    ctx = context_for(OWNER, BROADMATE_WORKSPACE)

    assert "confidence 0.55" in ctx["stable_prefix"]
    assert "3 measured outcome(s)" in ctx["stable_prefix"]


def test_a_contested_learning_is_retrieved_with_its_status(seeded_learnings):
    """Retrieved on purpose. A claim whose evidence disagrees is a real thing
    this account knows about itself, and hiding it would leave the model
    confident about exactly the questions where the evidence is thin."""
    from conftest import BROADMATE_WORKSPACE, OWNER

    seeded_learnings(workspace=BROADMATE_WORKSPACE,
                     statement="Evidence disagrees here", status="contested")
    ctx = context_for(OWNER, BROADMATE_WORKSPACE)

    assert "Evidence disagrees here" in ctx["stable_prefix"]
    assert "contested" in ctx["stable_prefix"]


def test_a_historical_learning_is_not_retrieved(seeded_learnings):
    """Superseded. The row stays - it is the audit trail for a decision somebody
    took on it - but it must not go on shaping new advice."""
    from conftest import BROADMATE_WORKSPACE, OWNER

    seeded_learnings(workspace=BROADMATE_WORKSPACE,
                     statement="No longer holds", status="historical")
    ctx = context_for(OWNER, BROADMATE_WORKSPACE)

    assert "No longer holds" not in ctx["stable_prefix"]


def test_a_retrieved_learning_is_named_in_the_provenance(seeded_learnings):
    """"Why did it say that?" has to be answerable from the run rather than
    from a log (PRD 17.8), so a learning that informed a proposal must appear in
    retrieved_record_ids or the answer is incomplete."""
    from conftest import BROADMATE_WORKSPACE, OWNER

    learning_id = seeded_learnings(workspace=BROADMATE_WORKSPACE,
                                   statement="Provenance probe")
    ctx = context_for(OWNER, BROADMATE_WORKSPACE)

    assert learning_id in ctx["retrieved_record_ids"]


def test_an_account_with_nothing_learned_says_so(seeded_learnings):
    """An empty section would read as though the system had no opinion, which is
    different from having nothing to go on yet. The second is the truth for a
    new account and the prompt should say it."""
    from conftest import OUTSIDER, RIVAL_WORKSPACE

    ctx = context_for(OUTSIDER, RIVAL_WORKSPACE)
    assert "nothing measured yet" in ctx["stable_prefix"]
