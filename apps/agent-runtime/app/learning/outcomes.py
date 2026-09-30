"""Measure what a decision actually did, at the horizon it named.

The loop was designed and half-built: ``t_advit.decisions`` records
``expected_effect_json`` and ``horizon_days`` BEFORE anything executes, and
``PostgresOutcomeScheduler`` queues a placeholder ``t_advit.outcomes`` row at
execution so the obligation to measure survives a restart. Nothing ever measured
one. Every outcome row in the database said ``unmeasurable`` with the note
"queued at execution; to be measured at the horizon", and no ``t_advit.learnings``
row had ever been written by anything.

The measurement is a before/after comparison over two adjacent windows of equal
length, ending and starting at the moment the action executed. That choice is
worth stating because a simpler one is wrong: comparing the after-window against
a fixed target alone cannot tell an improvement the decision caused from an
account that was already improving.

**The rule this module exists to hold.** ``unmeasurable`` is not ``met``. There
are five separate ways a measurement can fail to happen - the horizon has not
elapsed, the prediction was never machine-readable, the metric is unknown, the
before-window has no data, the after-window has no data - and every one of them
produces ``unmeasurable`` with the reason named. None of them produces a verdict
that reads like success. A learning loop that quietly scores its own unmeasured
predictions as met would get more confident the less it knew.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.db.pools import service_conn
from app.learning.metrics import Direction, Metric, resolve

log = logging.getLogger(__name__)

# Verdicts, matching t_advit.outcome_verdict.
BEAT = "beat"
MET = "met"
MISSED = "missed"
UNMEASURABLE = "unmeasurable"


@dataclass(frozen=True, slots=True)
class Window:
    start: date
    end: date          # half-open: [start, end)

    @property
    def days(self) -> int:
        return (self.end - self.start).days


@dataclass(frozen=True, slots=True)
class Reading:
    value: float | None
    days: int          # days that actually carried a number

    @property
    def known(self) -> bool:
        return self.value is not None and self.days > 0


@dataclass(frozen=True, slots=True)
class Measurement:
    verdict: str
    reason: str
    metric: Metric | None = None
    before: Reading | None = None
    after: Reading | None = None
    target: float | None = None
    predicted: Direction | None = None

    def as_vs_expected(self) -> dict[str, Any]:
        """The `vs_expected` column: enough to re-derive the verdict without
        re-reading the metric tables, because the tables will have moved on."""
        payload: dict[str, Any] = {"verdict": self.verdict, "reason": self.reason}
        if self.metric is not None:
            payload["metric"] = self.metric.key
            payload["metric_label"] = self.metric.label
            payload["source"] = self.metric.source
            payload["better_when"] = self.metric.better.value
        if self.predicted is not None:
            payload["predicted_direction"] = self.predicted.value
        if self.target is not None:
            payload["target"] = self.target
        for name, reading in (("before", self.before), ("after", self.after)):
            if reading is not None:
                payload[name] = {"value": reading.value, "days_with_data": reading.days}
        if self.before and self.after and self.before.known and self.after.known:
            payload["delta"] = self.after.value - self.before.value
        return payload


def _read(cur, metric: Metric, workspace_id: str, window: Window) -> Reading:
    cur.execute(
        metric.sql,
        {"workspace": workspace_id, "start": window.start, "end": window.end},
    )
    row = cur.fetchone()
    if row is None:
        return Reading(value=None, days=0)
    value = row["value"]
    return Reading(
        value=None if value is None else float(value),
        days=int(row["days"] or 0),
    )


def _prediction(expected_effect: dict[str, Any] | None) -> tuple[Metric | None, Direction | None, float | None]:
    """Pull the machine-readable half out of expected_effect_json.

    Older rows carry only prose - `{"expected_effect": "CAC should come down"}` -
    because the structured half did not exist when they were written. Those
    resolve to (None, None, None) and the caller reports `unmeasurable`, which is
    the honest answer: the prediction can be read and cannot be checked.
    """
    if not isinstance(expected_effect, dict):
        return None, None, None

    prediction = expected_effect.get("prediction")
    if not isinstance(prediction, dict):
        return None, None, None

    metric = resolve(prediction.get("metric"))

    raw_direction = str(prediction.get("direction") or "").strip().lower()
    direction = Direction(raw_direction) if raw_direction in {"up", "down"} else None

    target = prediction.get("target")
    try:
        target_value = None if target is None else float(target)
    except (TypeError, ValueError):
        target_value = None

    return metric, direction, target_value


def measure(
    cur,
    *,
    workspace_id: str,
    executed_on: date,
    horizon_days: int,
    expected_effect: dict[str, Any] | None,
    today: date,
) -> Measurement:
    """Compare the horizon window against the equal window before it."""

    metric, predicted, target = _prediction(expected_effect)

    after = Window(start=executed_on, end=executed_on + timedelta(days=horizon_days))
    if today < after.end:
        # Not a failure - it is simply not time yet. Reported as unmeasurable so
        # the row is honest if somebody reads it today, and the job will find it
        # again tomorrow.
        return Measurement(
            verdict=UNMEASURABLE,
            reason=(
                f"the {horizon_days}-day horizon ends on {after.end.isoformat()}; "
                "nothing to measure yet"
            ),
            metric=metric,
            target=target,
            predicted=predicted,
        )

    if metric is None or predicted is None:
        return Measurement(
            verdict=UNMEASURABLE,
            reason=(
                "the decision recorded no machine-readable prediction, so there is "
                "nothing to compare the result against"
            ),
            target=target,
            predicted=predicted,
        )

    before = Window(start=executed_on - timedelta(days=horizon_days), end=executed_on)
    before_reading = _read(cur, metric, workspace_id, before)
    after_reading = _read(cur, metric, workspace_id, after)

    if not before_reading.known:
        return Measurement(
            verdict=UNMEASURABLE,
            reason=(
                f"no {metric.label} on file for the {horizon_days} days before "
                f"{executed_on.isoformat()}, so there is no baseline to move from"
            ),
            metric=metric, before=before_reading, after=after_reading,
            target=target, predicted=predicted,
        )

    if not after_reading.known:
        return Measurement(
            verdict=UNMEASURABLE,
            reason=(
                f"no {metric.label} on file for the {horizon_days} days after "
                f"{executed_on.isoformat()}; the account may not have been synced"
            ),
            metric=metric, before=before_reading, after=after_reading,
            target=target, predicted=predicted,
        )

    delta = after_reading.value - before_reading.value
    moved_as_predicted = delta < 0 if predicted is Direction.DOWN else delta > 0

    if not moved_as_predicted:
        verdict, reason = MISSED, (
            f"{metric.label} moved from {before_reading.value:,.4g} to "
            f"{after_reading.value:,.4g}, against the predicted {predicted.value}"
        )
    elif target is None:
        # Moved the right way, and there is no threshold to have exceeded. `met`
        # rather than `beat`, deliberately: without a target, "beat" would mean
        # "moved further than some noise floor I invented", and a fabricated
        # threshold in the learning loop is a fabricated confidence downstream.
        verdict, reason = MET, (
            f"{metric.label} moved from {before_reading.value:,.4g} to "
            f"{after_reading.value:,.4g}, as predicted; no target was stated"
        )
    else:
        reached = (
            after_reading.value <= target
            if predicted is Direction.DOWN
            else after_reading.value >= target
        )
        verdict = BEAT if reached else MET
        verb = "reached" if reached else "moved toward but did not reach"
        reason = (
            f"{metric.label} went from {before_reading.value:,.4g} to "
            f"{after_reading.value:,.4g} and {verb} the target of {target:,.4g}"
        )

    return Measurement(
        verdict=verdict, reason=reason, metric=metric,
        before=before_reading, after=after_reading,
        target=target, predicted=predicted,
    )


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

DUE_OUTCOMES = """
select o.id::text            as outcome_id,
       o.decision_id::text   as decision_id,
       o.horizon_days        as horizon_days,
       d.expected_effect_json as expected_effect,
       d.decision_type       as decision_type,
       d.chosen_option       as chosen_option,
       -- The moment the action ran, not the moment the decision was recorded.
       -- A proposal can sit at the approval gate for a day, and measuring from
       -- the proposal would put part of the before-window after the change.
       (select min(a.executed_at) from t_advit.actions a
         where a.decision_id = d.id and a.executed_at is not null
           and a.rolled_back_at is null)          as executed_at
  from t_advit.outcomes o
  join t_advit.decisions d on d.id = o.decision_id
 where o.workspace_id = %(workspace)s::uuid
   -- Only rows nobody has measured. `verdict = 'unmeasurable'` alone is not the
   -- test: a row measured yesterday and found unmeasurable because the account
   -- was unsynced SHOULD be retried, but one that was genuinely measured must
   -- not be overwritten. `vs_expected is null` is what distinguishes "never
   -- looked" from "looked"; a retry then re-reads only rows whose reason says
   -- the data was missing.
   and (o.vs_expected is null
        or (o.verdict = 'unmeasurable'
            and coalesce(o.vs_expected->>'reason', '') not like '%%no machine-readable%%'))
 order by o.measured_at
 limit %(limit)s
"""


def measure_due(workspace_id: str, *, today: date | None = None, limit: int = 50) -> dict[str, Any]:
    """Measure every outcome for one workspace whose horizon has elapsed.

    Runs on the SERVICE connection. `authenticated` holds SELECT on outcomes and
    nothing else (20260903000007), so that an agent cannot revise its own score
    after the fact - which means the thing that writes the score has to be the
    connection a tenant is not.
    """
    when = today or date.today()
    measured: list[dict[str, Any]] = []
    counts = {BEAT: 0, MET: 0, MISSED: 0, UNMEASURABLE: 0}

    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(DUE_OUTCOMES, {"workspace": workspace_id, "limit": limit})
        due = cur.fetchall()

        for row in due:
            executed_at = row["executed_at"]
            if executed_at is None:
                # A decision that never executed has nothing to measure. It is
                # not a miss - the proposal may have been rejected, or is still
                # waiting at the approval gate - and scoring it would put a
                # prediction's failure on an action nobody took.
                result = Measurement(
                    verdict=UNMEASURABLE,
                    reason="the decision has no executed, un-rolled-back action to measure",
                )
            else:
                result = measure(
                    cur,
                    workspace_id=workspace_id,
                    executed_on=executed_at.date(),
                    horizon_days=int(row["horizon_days"]),
                    expected_effect=row["expected_effect"],
                    today=when,
                )

            cur.execute(
                """
                update t_advit.outcomes
                   set verdict      = %(verdict)s::t_advit.outcome_verdict,
                       vs_expected  = %(vs_expected)s::jsonb,
                       measured_at  = now(),
                       notes        = %(notes)s
                 where id = %(id)s::uuid
                """,
                {
                    "id": row["outcome_id"],
                    "verdict": result.verdict,
                    "vs_expected": json.dumps(result.as_vs_expected()),
                    "notes": result.reason,
                },
            )
            counts[result.verdict] += 1
            measured.append(
                {
                    "outcome_id": row["outcome_id"],
                    "decision_id": row["decision_id"],
                    "verdict": result.verdict,
                    "reason": result.reason,
                }
            )

        conn.commit()

    log.info(
        "measured %d outcome(s) for workspace %s: %s",
        len(measured), workspace_id,
        ", ".join(f"{k}={v}" for k, v in counts.items() if v),
    )
    return {"considered": len(due), "measured": measured, "counts": counts}
