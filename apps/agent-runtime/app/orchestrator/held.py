"""The system's record of a question it asked and the owner has not answered.

The CTA gate holds a proposal that would build a campaign while no destination
is on file. The turn ends ASKED, and until now that was the end of the record:
the hold lived in one run's state and one chat response, so an owner who
closed the chat could not find the question again, and the reports route had
to answer ``held: null``.

A question the owner has not answered is a fact about the account, not about
one chat turn - so it is a row in ``t_advit.held_proposals``, and this module
is the only writer of that row. Three operations:

  * ``hold``     writes the row when the gate holds. A workspace has at most
                 one open question, so the previous open row is resolved as
                 ``superseded`` first; the newest question is the live one.
  * ``resolve``  closes the open row, saying how - ``cta_set`` when the owner
                 answered, ``superseded`` when a newer hold replaced it.
  * ``open_for`` reads the open row in the shape the reports route returns.

Every function takes a CURSOR and nothing here opens a connection. The
caller decides which connection, and the grant matrix decides whether the
statement runs: ``authenticated`` holds SELECT on this table and nothing else,
so ``hold`` and ``resolve`` succeed only on the backend connection, and a
tenant cursor handed to either gets ``permission denied``. That is the
property the table exists for - a tenant cannot edit, invent or close the
system's record of its own question - and keeping the SQL here rather than
in each caller means it is enforced by one grant rather than re-argued at
every call site.

What this module refuses: to hold a result the gate did not hold. A gate
result with ``held=False`` has no question, and writing a row for it would
put an answered question in the inbox.
"""

from __future__ import annotations

import json
from typing import Any

from app.orchestrator.cta_gate import GateResult

CTA_SET = "cta_set"
SUPERSEDED = "superseded"

RESOLUTIONS = frozenset({CTA_SET, SUPERSEDED})

# The open row, in the shape the reports route hands to the page. Ordered and
# limited even though the partial unique index allows one open row, so the
# query says what it means if the index is ever relaxed.
OPEN = """
select id::text, run_id::text as run_id, proposal_json, question, reason,
       recommendation_json, held_at
  from t_advit.held_proposals
 where workspace_id = %(workspace)s::uuid
   and resolved_at is null
 order by held_at desc
 limit 1
"""


def hold(cur: Any, *, workspace_id: str, run_id: str | None, gated: GateResult) -> str:
    """Record a held proposal; return the new row's id.

    ``run_id`` may be None for a hold raised outside a run, but not for a run
    that has no row: the foreign key refuses that, which is the correct answer
    to "record a question from a run that never opened".
    """
    if not gated.held:
        raise ValueError("refusing to record a proposal the gate did not hold")
    if not gated.question:
        raise ValueError("a held proposal with no question is not a question")

    # The newest question supersedes. The partial unique index would refuse a
    # second open row anyway; resolving first is what makes the refusal a
    # design rather than a race - and the lock is what makes "first" true.
    # Two chat turns for one workspace can reach here together; without the
    # lock both would find nothing open, both would insert, and one would
    # die on the index after the model had already been paid for. The lock
    # is transaction-scoped and keyed on the workspace, so it serialises
    # exactly the two writers that would collide and nobody else.
    cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"held_proposals:{workspace_id}",))
    resolve(cur, workspace_id=workspace_id, by=SUPERSEDED)

    cur.execute(
        """
        insert into t_advit.held_proposals
          (workspace_id, run_id, proposal_json, question, reason, recommendation_json)
        values (%s::uuid, %s::uuid, %s::jsonb, %s, %s, %s::jsonb)
        returning id::text
        """,
        (
            workspace_id,
            run_id,
            json.dumps(gated.proposal, default=str),
            gated.question,
            gated.reason,
            json.dumps(gated.recommendation, default=str) if gated.recommendation is not None else None,
        ),
    )
    return _id(cur.fetchone())


def resolve(cur: Any, *, workspace_id: str, by: str) -> dict[str, Any] | None:
    """Close the workspace's open held proposal, saying how.

    Returns the row that was closed - id, run_id, question, held_at and the
    proposal's goal - or None when nothing was open. The caller uses that to
    tell the owner their answer released a question, and None is an honest
    "nothing was waiting" rather than an error: the owner may set a destination
    before ever asking for a campaign.
    """
    if by not in RESOLUTIONS:
        raise ValueError(f"{by!r} is not a resolution; one of {sorted(RESOLUTIONS)}")
    cur.execute(
        """
        update t_advit.held_proposals
           set resolved_at = now(), resolved_by = %s
         where workspace_id = %s::uuid
           and resolved_at is null
        returning id::text, run_id::text as run_id, question, held_at,
                  proposal_json ->> 'goal' as goal
        """,
        (by, workspace_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if not isinstance(row, dict):
        row = dict(zip(("id", "run_id", "question", "held_at", "goal"), row))
    return {**row, "held_at": row["held_at"].isoformat(), "resolved_by": by}


def open_for(cur: Any, workspace_id: str) -> dict[str, Any] | None:
    """The open held proposal, or None. Run on the caller's own connection so
    the policy, not this query, decides whether the row is theirs to see."""
    cur.execute(OPEN, {"workspace": workspace_id})
    row = cur.fetchone()
    if row is None:
        return None
    if not isinstance(row, dict):
        row = dict(zip(
            ("id", "run_id", "proposal_json", "question", "reason", "recommendation_json", "held_at"),
            row,
        ))
    return {
        "held": True,
        "id": row["id"],
        "run_id": row["run_id"],
        "proposal": row["proposal_json"],
        "question": row["question"],
        "reason": row["reason"],
        "recommendation": row["recommendation_json"],
        "held_at": row["held_at"].isoformat(),
    }


def nothing_open() -> dict[str, Any]:
    """The same keys as an open row, so the page reads one shape. ``held`` is
    False here because the table WAS checked and had no open row - the case
    the old ``held: null`` existed to distinguish from."""
    return {
        "held": False,
        "id": None,
        "run_id": None,
        "proposal": None,
        "question": None,
        "reason": "no proposal is held for this workspace",
        "recommendation": None,
        "held_at": None,
    }


def _id(row: Any) -> str:
    return row["id"] if isinstance(row, dict) else row[0]
