"""Turn measured outcomes into an account-tier learning.

This is tier 1 of the three-tier memory: what THIS account has learned about
itself. Tier 2 (industry) and tier 3 (global) are reached by promotion through
``t_advit.industry_patterns``, which carries its own independence gate - at
least three distinct workspaces and two distinct owners, enforced as a CHECK
constraint rather than as a convention.

Two properties are worth stating because they are choices, not accidents.

**No model call.** The statement is rendered from the numbers. That makes this
job deterministic, free, testable without a key, and impossible to hallucinate a
finding into. A model is the right tool for explaining a learning to an owner;
it is the wrong tool for deciding whether one exists.

**One observation is not a learning.** A claim needs at least two measured
outcomes agreeing about the same decision type and metric before any row is
written. Below that the outcome row stands on its own, which is the honest
record: the system did a thing once and saw a result once. The alternative -
writing a learning at n=1 - produces a memory that is indistinguishable in shape
from a well-evidenced one and will be retrieved into a prompt as though it were.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.db.pools import service_conn
from app.learning.metrics import resolve
from app.learning.outcomes import BEAT, MET

log = logging.getLogger(__name__)

# Below this, the outcome row is the whole record. See the module docstring.
MIN_EVIDENCE = 2

# A claim is contested rather than active when the evidence genuinely disagrees.
# Two thresholds rather than one, so "mostly holds" and "mostly fails" are both
# claims and only the middle is a shrug.
ACTIVE_AT = 0.60
FAILS_AT = 0.40


@dataclass(frozen=True, slots=True)
class Claim:
    decision_type: str
    metric_key: str
    direction: str
    supported: int          # outcomes where the metric moved as predicted
    total: int              # measured outcomes, excluding unmeasurable
    mean_delta: float | None
    evidence_refs: tuple[str, ...]

    @property
    def consistency(self) -> float:
        return self.supported / self.total if self.total else 0.0

    @property
    def confidence(self) -> float:
        """Laplace-smoothed, so one observation cannot claim certainty.

        (supported + 1) / (total + 2) is the posterior mean of a Beta(1,1) prior.
        Two for two reads 0.75, not 1.0 - which is the right amount of doubt to
        carry into a prompt, and it is why this is not simply supported/total.
        """
        return (self.supported + 1) / (self.total + 2)

    @property
    def status(self) -> str:
        if self.consistency >= ACTIVE_AT or self.consistency <= FAILS_AT:
            return "active"
        return "contested"

    def statement(self) -> str:
        metric = resolve(self.metric_key)
        label = metric.label if metric else self.metric_key
        action = self.decision_type.replace("_", " ")

        if self.consistency <= FAILS_AT:
            # Stated as what it is: a claim that FAILED. A learning is not only
            # a thing that worked, and recording only the successes is how a
            # system talks itself into a strategy.
            return (
                f"On this account, {action} did NOT move {label} "
                f"{self.direction} - it held or moved the other way in "
                f"{self.total - self.supported} of {self.total} measured attempts."
            )

        delta = ""
        if self.mean_delta is not None:
            delta = f" Mean change {self.mean_delta:+,.4g}."
        return (
            f"On this account, {action} moved {label} {self.direction} in "
            f"{self.supported} of {self.total} measured attempts.{delta}"
        )


# Only MEASURED outcomes count. `unmeasurable` is excluded rather than treated as
# a miss: it means nobody looked, or there was nothing to look at, and folding
# that into a denominator would make the system less confident the worse its data
# pipeline was - which is a bias in the wrong direction and invisible once
# averaged.
CLAIMS = """
select d.decision_type                       as decision_type,
       o.vs_expected ->> 'metric'            as metric_key,
       o.vs_expected ->> 'predicted_direction' as direction,
       count(*)::int                         as total,
       count(*) filter (
         where o.verdict in ('beat', 'met')
       )::int                                as supported,
       avg((o.vs_expected ->> 'delta')::numeric)::float as mean_delta,
       array_agg(o.id::text order by o.measured_at)     as evidence_refs
  from t_advit.outcomes o
  join t_advit.decisions d on d.id = o.decision_id
 where o.workspace_id = %(workspace)s::uuid
   and o.verdict <> 'unmeasurable'
   and o.vs_expected ->> 'metric' is not null
   and o.vs_expected ->> 'predicted_direction' is not null
 group by 1, 2, 3
having count(*) >= %(min_evidence)s
"""

UPSERT = """
insert into t_advit.learnings
  (workspace_id, tier, statement, conditions_json, effect_size,
   confidence, evidence_n, evidence_refs, status)
values
  (%(workspace)s::uuid, 'account', %(statement)s, %(conditions)s::jsonb,
   %(effect_size)s, %(confidence)s, %(evidence_n)s, %(evidence_refs)s::text[],
   %(status)s::t_advit.learning_status)
on conflict (workspace_id,
             (conditions_json ->> 'decision_type'),
             (conditions_json ->> 'metric'),
             (conditions_json ->> 'direction'))
  where tier = 'account' and status <> 'historical'
do update set
  statement     = excluded.statement,
  effect_size   = excluded.effect_size,
  confidence    = excluded.confidence,
  -- Monotonic. Evidence is a count of things that happened, and a job that
  -- looked at fewer rows this tick (a LIMIT, a partial sync) must not be able
  -- to make the account look less experienced than it is.
  evidence_n    = greatest(t_advit.learnings.evidence_n, excluded.evidence_n),
  evidence_refs = excluded.evidence_refs,
  status        = excluded.status,
  updated_at    = now()
returning id::text, (xmax = 0) as inserted
"""


def promote(workspace_id: str, *, min_evidence: int = MIN_EVIDENCE) -> dict[str, Any]:
    """Write or sharpen this workspace's account-tier learnings.

    On the SERVICE connection: `authenticated` holds SELECT on learnings and
    nothing else, so an agent cannot edit what it has supposedly learned.
    """
    written: list[dict[str, Any]] = []

    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(CLAIMS, {"workspace": workspace_id, "min_evidence": min_evidence})
        rows = cur.fetchall()

        for row in rows:
            claim = Claim(
                decision_type=row["decision_type"],
                metric_key=row["metric_key"],
                direction=row["direction"],
                supported=row["supported"],
                total=row["total"],
                mean_delta=row["mean_delta"],
                evidence_refs=tuple(row["evidence_refs"] or ()),
            )
            conditions = {
                "decision_type": claim.decision_type,
                "metric": claim.metric_key,
                "direction": claim.direction,
                "consistency": round(claim.consistency, 4),
                "measured_outcomes": claim.total,
            }
            cur.execute(
                UPSERT,
                {
                    "workspace": workspace_id,
                    "statement": claim.statement(),
                    "conditions": json.dumps(conditions),
                    "effect_size": claim.mean_delta,
                    "confidence": round(claim.confidence, 3),
                    "evidence_n": claim.total,
                    "evidence_refs": list(claim.evidence_refs),
                    "status": claim.status,
                },
            )
            result = cur.fetchone()
            written.append(
                {
                    "learning_id": result["id"],
                    "inserted": bool(result["inserted"]),
                    "statement": claim.statement(),
                    "confidence": round(claim.confidence, 3),
                    "evidence_n": claim.total,
                    "status": claim.status,
                }
            )

        conn.commit()

    log.info(
        "promoted %d account-tier learning(s) for workspace %s", len(written), workspace_id
    )
    return {"claims": len(rows), "written": written}
