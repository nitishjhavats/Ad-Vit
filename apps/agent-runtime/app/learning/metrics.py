"""What this system is willing to be held to.

A prediction is only worth recording if it can be checked, so the set of metrics
a proposal may predict and the SQL that measures one live in the same place. If
they lived apart, the model could name a metric nothing knows how to read, and
every outcome for it would come back ``unmeasurable`` - which looks like a data
gap rather than like a schema drift.

Two sources, and the distinction matters more than it looks:

  * ``t_advit.blended_daily`` is BUSINESS truth - the owner's own orders,
    confirmations and returns, with Meta spend folded in. CAC and contribution
    margin live here. Its rows carry a ``gaps`` array precisely because some
    days are not measurable, and this module honours that rather than reading
    around it.

  * ``t_advit.metrics_daily`` is PLATFORM truth - what Meta reported. Spend and
    results live here.

Never mixed in one metric. A number that is half business truth and half
platform truth is neither, and the whole thesis of this product is that the two
are different and business truth wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Direction(str, Enum):
    """Which way is better.

    Recorded on the metric rather than supplied by the caller, because "down is
    good for CAC" is a property of CAC and not an opinion a model should get to
    have. A proposal still states which way it expects the number to MOVE; this
    is what makes "moved as predicted" and "improved" separable.
    """

    DOWN = "down"
    UP = "up"


@dataclass(frozen=True, slots=True)
class Metric:
    key: str
    label: str
    better: Direction
    # The aggregate over one window. Parameterised by %(workspace)s, %(start)s
    # and %(end)s, with the window half-open [start, end) so two adjacent
    # windows cannot both claim the same day.
    sql: str
    # True when the metric is a RATE or a ratio, so averaging days is the right
    # aggregate; False when it is a per-day amount that should be summed.
    # Getting this backwards produces a number that looks plausible and is not
    # comparable to anything.
    is_rate: bool

    @property
    def source(self) -> str:
        return "business_truth" if "blended_daily" in self.sql else "platform"


def _blended(column: str) -> str:
    # avg, not sum: every column in blended_daily is a rate or a per-day
    # economic figure, and summing CAC across seven days answers no question.
    #
    # `where <column> is not null` is load-bearing. compute_blended_daily writes
    # NULL and names the reason in `gaps` when a day cannot be computed - that
    # was the whole point of 20260911000002 - so counting a NULL day as zero
    # here would undo it one layer up.
    return f"""
        select avg({column})::float as value, count({column})::int as days
          from t_advit.blended_daily
         where workspace_id = %(workspace)s::uuid
           and date >= %(start)s::date
           and date <  %(end)s::date
           and {column} is not null
    """


def _platform(expression: str) -> str:
    # Account level only. Summing account rows together with the ad-set rows
    # that roll up into them double-counts, which is the same defect
    # 20260911000010 fixed one layer down.
    return f"""
        select {expression} as value, count(*)::int as days
          from t_advit.metrics_daily
         where workspace_id = %(workspace)s::uuid
           and level = 'account'
           and date >= %(start)s::date
           and date <  %(end)s::date
    """


METRICS: dict[str, Metric] = {
    m.key: m
    for m in (
        Metric(
            key="blended_cac_inr",
            label="blended CAC",
            better=Direction.DOWN,
            sql=_blended("blended_cac_inr"),
            is_rate=True,
        ),
        Metric(
            key="contribution_margin_inr",
            label="contribution margin per delivered order",
            better=Direction.UP,
            sql=_blended("contribution_margin_inr"),
            is_rate=True,
        ),
        Metric(
            key="confirm_rate",
            label="confirmation rate",
            better=Direction.UP,
            sql=_blended("confirm_rate"),
            is_rate=True,
        ),
        Metric(
            key="rto_rate",
            label="RTO rate",
            better=Direction.DOWN,
            sql=_blended("rto_rate"),
            is_rate=True,
        ),
        Metric(
            key="delivered_aov_inr",
            label="delivered AOV",
            better=Direction.UP,
            sql=_blended("delivered_aov_inr"),
            is_rate=True,
        ),
        Metric(
            key="mer",
            label="marketing efficiency ratio",
            better=Direction.UP,
            sql=_blended("mer"),
            is_rate=True,
        ),
        Metric(
            key="spend_inr",
            label="daily spend",
            # Neither. More spend is neither good nor bad on its own - it is
            # good if CAC held and bad if it did not - so `better` is the
            # direction a proposal would have to STATE, and nothing here treats
            # a rise as an improvement by itself.
            better=Direction.UP,
            sql=_platform("(sum(spend_inr) / nullif(count(*), 0))::float"),
            is_rate=True,
        ),
        Metric(
            key="results",
            label="results per day",
            better=Direction.UP,
            sql=_platform("(sum(results)::numeric / nullif(count(*), 0))::float"),
            is_rate=True,
        ),
        Metric(
            key="cost_per_result",
            label="cost per result",
            better=Direction.DOWN,
            sql=_platform(
                "(sum(spend_inr) / nullif(sum(results), 0))::float"
            ),
            is_rate=True,
        ),
    )
}

# What a proposal is allowed to predict. Sorted so the JSON Schema handed to the
# model is stable across runs, which keeps the prompt cacheable.
PREDICTABLE = tuple(sorted(METRICS))


def resolve(key: str | None) -> Metric | None:
    """A metric this system can actually measure, or None.

    None is returned rather than raised on purpose: an unknown metric means the
    outcome is UNMEASURABLE, which is a verdict the caller records, not an error
    that loses the row. A decision whose prediction cannot be checked is still a
    decision that happened.
    """
    if not key:
        return None
    return METRICS.get(key.strip())
