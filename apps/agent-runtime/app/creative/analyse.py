"""Rate a creative: what ffmpeg can measure, what the model can judge, what the
compliance gate can refuse, and what this account's own history says.

Four sources, kept apart in the output so the owner can see which is which:

  * ``measured`` - aspect ratio and length, from the file. No model.
  * ``judged``   - the rubric's judged criteria, each with a score AND a reason,
                   from the organisation's own `creative_analysis` tier. This
                   is the part the customer pays for, and the part that can be
                   wrong - so every score carries the sentence that would let
                   the owner disagree with it.
  * ``compliance`` - the same nine-stage gate that governs a live ad, run on
                   the text the model read off the frames. A creative that
                   would be BLOCKED as an ad is told so here, before any spend.
  * ``history``  - how this account's own past creatives performed, from
                   metrics_daily at ad level where a creative is linked to an
                   ad. Arithmetic, not opinion; and "not enough history" when
                   there is not.

Two things are deliberately NOT here.

The ad library. Comparing against what is running in Meta's Ad Library needs
the Marketing API's ads_archive endpoint, which needs an approved app, which is
the long pole nobody has started. Until then a claim like "this hook is what is
performing in your category right now" would be a claim with nothing behind it,
and the rating says the comparison is unavailable rather than inventing one.

Audio. See frames.py.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.agents.compliance import ComplianceGate, CreativeBundle, LicencePosture
from app.creative import frames as fr
from app.creative.rubric import JUDGED, RUBRIC_VERSION, measure
from app.models.router import ModelRouter

log = logging.getLogger(__name__)

ROLE = "creative_analysis"


@dataclass(frozen=True, slots=True)
class Judgement:
    key: str
    score: int          # 0-10
    reason: str


@dataclass(slots=True)
class Rating:
    rubric_version: str
    overall: int | None                 # 0-100, weighted; None if not judged
    measured: list[dict[str, Any]]
    judged: list[Judgement]
    on_screen_text: str
    what_is_sold: str
    strongest: str
    weakest: str
    rewrite: str
    compliance: dict[str, Any] | None
    history: dict[str, Any]
    limitations: list[str] = field(default_factory=list)
    model: str | None = None
    cost_inr: float | None = None
    comparisons_unavailable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rubric_version": self.rubric_version,
            "overall": self.overall,
            "measured": self.measured,
            "judged": [{"key": j.key, "score": j.score, "reason": j.reason} for j in self.judged],
            "on_screen_text": self.on_screen_text,
            "what_is_sold": self.what_is_sold,
            "strongest": self.strongest,
            "weakest": self.weakest,
            "rewrite": self.rewrite,
            "compliance": self.compliance,
            "history": self.history,
            "limitations": self.limitations,
            "comparisons_unavailable": self.comparisons_unavailable,
            "model": self.model,
            "cost_inr": self.cost_inr,
        }


# ---------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------

SYSTEM = """You are reviewing a video ad for an Indian direct-to-consumer brand that sells largely on cash-on-delivery. You are shown a sequence of frames, labelled HOOK for the first three seconds and BODY after, with no audio.

Score each criterion from 0 to 10 and give the specific reason, in one or two sentences an owner could act on. Read every piece of on-screen text you can see and transcribe it exactly. Say what is being sold and what the viewer is asked to do.

Be direct. A score of 7 with "good hook" is useless; a score of 4 with "opens on the logo for two seconds before anything happens" is feedback. Do not praise. If a criterion cannot be judged from frames alone, say so in the reason and score it 5."""


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "on_screen_text": {
                "type": "string",
                "description": "Every word visible in any frame, transcribed. Empty string if none.",
            },
            "what_is_sold": {"type": "string"},
            "call_to_action": {"type": "string"},
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": [c.key for c in JUDGED]},
                        "score": {"type": "integer", "minimum": 0, "maximum": 10},
                        "reason": {"type": "string"},
                    },
                    "required": ["key", "score", "reason"],
                    "additionalProperties": False,
                },
            },
            "strongest": {"type": "string"},
            "weakest": {"type": "string"},
            "rewrite": {
                "type": "string",
                "description": "The single change that would most improve this creative, stated as an instruction to the editor.",
            },
        },
        "required": ["on_screen_text", "what_is_sold", "call_to_action", "criteria",
                     "strongest", "weakest", "rewrite"],
        "additionalProperties": False,
    }


def _user_turn(extracted: fr.Extracted, product_hint: str | None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    intro = (
        f"{len(extracted.frames)} frames from a "
        f"{extracted.metadata.duration_s or '?'}s video, "
        f"{extracted.metadata.width}x{extracted.metadata.height}."
    )
    if product_hint:
        intro += f" The brand says this advertises: {product_hint}."
    intro += "\n\nCriteria:\n" + "\n".join(f"- {c.key}: {c.ask}" for c in JUDGED)
    parts.append({"type": "text", "text": intro})

    for frame in extracted.frames:
        parts.append({"type": "text", "text": f"[{frame.phase.upper()} t={frame.at_s:.1f}s]"})
        parts.append({
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(frame.jpeg).decode("ascii"),
            },
        })
    return parts


def judge(router: ModelRouter, extracted: fr.Extracted, *, product_hint: str | None) -> tuple[dict[str, Any], Any]:
    """One structured call at the creative_analysis role."""
    return router.complete_json(
        ROLE,
        system=SYSTEM,
        user=_user_turn(extracted, product_hint),
        schema=_schema(),
    )


# ---------------------------------------------------------------------------
# History: how this account's own creatives have done
# ---------------------------------------------------------------------------

HISTORY = """
select c.id::text                                  as creative_id,
       c.original_name                             as name,
       c.rating_json ->> 'overall'                 as overall,
       sum(m.spend_inr)                            as spend_inr,
       sum(m.results)                              as results,
       case when sum(m.results) > 0
            then sum(m.spend_inr) / sum(m.results) end as cost_per_result
  from t_advit.creatives c
  join t_advit.metrics_daily m
    on m.workspace_id = c.workspace_id
   and m.level = 'ad'
   and m.entity_id = c.meta_id
 where c.workspace_id = %(workspace)s::uuid
   and c.meta_id is not null
   and c.status = 'analysed'
   and m.date >= current_date - 90
 group by c.id, c.original_name, c.rating_json
having sum(m.spend_inr) > 0
 order by cost_per_result nulls last
 limit 10
"""


def history(cur, workspace_id: str) -> dict[str, Any]:
    """This account's own linked creatives, ranked by what they cost per result.

    A creative is linked once it has run as an ad (meta_id set) and metrics
    exist for that ad. Until then there is nothing to compare against, and the
    honest answer is that rather than a comparison against nothing.
    """
    cur.execute(HISTORY, {"workspace": workspace_id})
    rows = cur.fetchall()
    if not rows:
        return {
            "available": False,
            "reason": (
                "no creative in this workspace has run as an ad with ingested "
                "metrics yet, so there is nothing of this account's own to compare "
                "against"
            ),
            "creatives": [],
        }
    return {
        "available": True,
        "window_days": 90,
        "creatives": [
            {
                "creative_id": r["creative_id"],
                "name": r["name"],
                "overall_rating": int(r["overall"]) if r["overall"] else None,
                "spend_inr": float(r["spend_inr"]),
                "results": int(r["results"] or 0),
                "cost_per_result_inr": float(r["cost_per_result"]) if r["cost_per_result"] else None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# The whole rating
# ---------------------------------------------------------------------------


def rate(
    *,
    path: Path,
    router: ModelRouter | None,
    gate: ComplianceGate,
    licence_posture: LicencePosture | None,
    cur,
    workspace_id: str,
    # From t_advit.workspaces, never defaulted. CreativeBundle defaults it to
    # general_d2c, and a rating judged under the wrong pack is the compliance
    # defect this repository has already had once: the ayurveda workspace's
    # video would clear a Schedule J check that governs it.
    business_type: str,
    product_hint: str | None = None,
    objective: str = "conversion",
    extractor: Callable[[Path], fr.Extracted] = fr.extract,
) -> Rating:
    extracted = extractor(path)
    limitations = list(extracted.limitations)

    measured = measure(
        width=extracted.metadata.width,
        height=extracted.metadata.height,
        duration_s=extracted.metadata.duration_s,
        objective=objective,
    )

    # -- judged ----------------------------------------------------------
    judged: list[Judgement] = []
    on_screen_text = ""
    what_is_sold = strongest = weakest = rewrite = ""
    model = None
    cost = None

    if router is None:
        limitations.append(
            "no model was available for this organisation (no OpenRouter key on "
            "file), so the judged criteria were not scored; only what the file "
            "itself says is reported"
        )
    elif not extracted.frames:
        limitations.append("no frames could be extracted, so nothing was judged")
    else:
        parsed, completion = judge(router, extracted, product_hint=product_hint)
        model = completion.model
        cost = completion.cost_inr
        on_screen_text = str(parsed.get("on_screen_text") or "")
        what_is_sold = str(parsed.get("what_is_sold") or "")
        strongest = str(parsed.get("strongest") or "")
        weakest = str(parsed.get("weakest") or "")
        rewrite = str(parsed.get("rewrite") or "")

        seen: set[str] = set()
        for item in parsed.get("criteria") or []:
            key = str(item.get("key") or "")
            if key in seen or key not in {c.key for c in JUDGED}:
                continue
            seen.add(key)
            score = max(0, min(10, int(item.get("score", 5))))
            judged.append(Judgement(key=key, score=score, reason=str(item.get("reason") or "")))

        missing = [c.key for c in JUDGED if c.key not in seen]
        if missing:
            # A criterion the model skipped is not a criterion it passed. It is
            # recorded as unjudged and the overall is computed over what WAS
            # judged, with the gap named.
            limitations.append(f"the model did not score: {', '.join(missing)}")

    # Weighted over the criteria that were actually judged. If none were, there
    # is no overall - not a zero, and not a fifty.
    overall: int | None = None
    if judged:
        weights = {c.key: c.weight for c in JUDGED}
        total_weight = sum(weights[j.key] for j in judged)
        overall = round(sum(j.score * weights[j.key] for j in judged) / total_weight * 10)

    # -- compliance --------------------------------------------------------
    compliance: dict[str, Any] | None = None
    if on_screen_text.strip():
        bundle = CreativeBundle(
            primary_text=on_screen_text,
            business_type=business_type,
            has_media=True,
            # Not declared here: the upload form asks. Absent means the
            # AI-disclosure stage reports itself unevaluated rather than clean.
            ai_generated_declared=None,
            licence_posture=licence_posture,
        )
        result = gate.check(bundle)
        compliance = {
            "verdict": result.verdict.value,
            "findings": [
                {
                    "rule": f.rule_code,
                    "severity": f.severity.value,
                    "field": f.field,
                    "span": f.offending_span,
                    "title": f.title,
                    "suggested_rewrite": f.suggested_rewrite,
                }
                for f in result.findings
            ],
            "stages_not_fully_checked": result.stages_not_fully_checked,
            "checked_against": "on-screen text only; spoken claims were not analysed",
        }
    else:
        limitations.append(
            "no on-screen text was read from the frames, so the compliance "
            "pre-check had nothing to evaluate"
        )

    return Rating(
        rubric_version=RUBRIC_VERSION,
        overall=overall,
        measured=measured,
        judged=judged,
        on_screen_text=on_screen_text,
        what_is_sold=what_is_sold,
        strongest=strongest,
        weakest=weakest,
        rewrite=rewrite,
        compliance=compliance,
        history=history(cur, workspace_id),
        limitations=limitations,
        model=model,
        cost_inr=cost,
        comparisons_unavailable=[
            "Meta Ad Library: needs an approved Marketing API app (ads_archive), "
            "which has not been applied for. No comparison against what is "
            "currently running in this category was made."
        ],
    )


__all__ = ["Rating", "Judgement", "rate", "judge", "history", "ROLE"]
_ = json
