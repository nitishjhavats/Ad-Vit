"""Compliance Guard - the pre-flight gate (PRD 13).

Two layers, both blocking: Meta advertising policy and Indian law. This module
is deliberately free of database and network dependencies so it can be unit
tested against a fixed ruleset; the DB loader lives in ``app.policy.rules``.

Three commitments encoded here rather than left to convention:

1. A stage that is not implemented reports NOT_EVALUATED. It never reports a
   pass. PRD 4.5 is explicit that the OS does not guarantee ad approval, and a
   silently skipped check is exactly how such a guarantee gets implied.

2. Every finding carries the instrument, the offending span, the source URL and
   the effective date. "Blocked because policy" is not an acceptable answer
   (PRD 13.5).

3. Meta and Indian layers are adjudicated independently and reported
   separately. Passing one implies nothing about the other (PRD Appendix D.3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Protocol, Sequence


class Verdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"
    NOT_EVALUATED = "not_evaluated"


class Severity(str, Enum):
    BLOCK = "block"
    WARN = "warn"
    INFO = "info"


class RuleType(str, Enum):
    TERM_LIST = "term_list"
    REGEX = "regex"
    LLM_JUDGE = "llm_judge"
    STATE_CHECK = "state_check"


# The nine stages of PRD 13.4, in order.
STAGE_NAMES: dict[int, str] = {
    1: "claim_extraction",
    2: "schedule_j_match",
    3: "personal_attributes",
    4: "outcome_and_timeline",
    5: "imagery",
    6: "ai_disclosure",
    7: "landing_page",
    8: "consent_and_privacy",
    9: "licence_posture",
}

# Stages this build does not implement. Named explicitly so the omission is
# visible in every verdict rather than buried in a changelog.
UNIMPLEMENTED_STAGES: frozenset[int] = frozenset({7, 8})


@dataclass(frozen=True, slots=True)
class PolicyRule:
    code: str
    jurisdiction: str          # "meta" | "in"
    instrument: str
    gate_stage: int
    rule_type: RuleType
    title: str
    severity: Severity
    explanation: str
    source_url: str
    as_of: date
    pattern: str | None = None
    terms: tuple[str, ...] = ()
    remedy_template: str | None = None
    business_types: tuple[str, ...] = ()

    # What a state_check checks, carried as data so a second industry pack does
    # not need a branch in _check_state. `required_facts` names the state that
    # must be on file (AYUSH: licence number and classification; RERA:
    # registration number and authority). `state_handler` names a coded handler
    # for the one rule that is not a credential lookup - the AI disclosure
    # tri-state. A rule with neither is refused by the database
    # (policy_rules_has_matcher), because it would pass every bundle silently.
    required_facts: tuple[str, ...] = ()
    state_handler: str | None = None

    def applies_to(self, business_type: str) -> bool:
        return not self.business_types or business_type in self.business_types


@dataclass(frozen=True, slots=True)
class LicencePosture:
    """What is ON FILE about the product a creative advertises.

    Stage 9 reads this and nothing else. Two nullable strings used to carry it,
    and that is precisely why the defect was invisible: ``None`` meant both "the
    caller did not say" and "the product has no licence", and the rule read them
    identically. Three states are needed and this carries all three.

    * resolved and licensed        - sku set, licence and classification set
    * resolved and NOT licensed    - sku set, licence or classification missing
    * unresolved                   - ``unresolved_reason`` set, and the gate
                                     must report stage 9 as not evaluated

    ``source`` records where it came from, because a posture the caller asserted
    about their own compliance is evidence of nothing. The orchestrator only
    ever builds ``catalogue`` postures.
    """

    source: str                                # "catalogue" | "caller_asserted"
    sku: str | None = None
    ayush_licence_no: str | None = None
    classification: str | None = None
    unresolved_reason: str | None = None
    candidate_skus: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.unresolved_reason is None


@dataclass(frozen=True, slots=True)
class CreativeBundle:
    """Everything Meta evaluates together: copy, creative, destination, state.

    Review is multimodal - text, image, video, audio and the landing-page first
    fold are assessed as one object (PRD 13.2).
    """

    primary_text: str = ""
    headline: str = ""
    description: str = ""
    cta_type: str = ""
    destination_url: str = ""
    lp_first_fold_text: str | None = None
    business_type: str = "general_d2c"

    ai_generated_declared: bool | None = None
    # The licence facts arrive as one resolved record, never as loose strings a
    # caller can set. See LicencePosture.
    licence_posture: LicencePosture | None = None
    has_media: bool = False
    media_refs: tuple[str, ...] = ()

    def copy_fields(self) -> list[tuple[str, str]]:
        """Named text fields, so a finding can say WHERE the span was found."""
        return [
            (name, value)
            for name, value in (
                ("primary_text", self.primary_text),
                ("headline", self.headline),
                ("description", self.description),
            )
            if value
        ]


@dataclass(frozen=True, slots=True)
class Finding:
    rule_code: str
    layer: str                 # "meta" | "india"
    instrument: str
    stage: int
    severity: Severity
    title: str
    explanation: str
    source_url: str
    as_of: date
    field: str | None = None
    offending_span: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    suggested_rewrite: str | None = None
    needs_legal_verification: bool = False


@dataclass(slots=True)
class ComplianceResult:
    """Stage coverage is reported as three DISJOINT sets.

    A stage can be genuinely half-covered: stage 4 holds both regex rules,
    which always run, and a classical-text judgement that needs a model. Saying
    a stage was both evaluated and skipped reads like a contradiction and
    undermines the report, so partial coverage gets its own name.
    """

    verdict: Verdict
    findings: list[Finding] = field(default_factory=list)
    stages_evaluated: list[int] = field(default_factory=list)
    stages_partial: list[int] = field(default_factory=list)
    stages_skipped: list[int] = field(default_factory=list)
    overall_risk: float = 0.0

    @property
    def stages_not_fully_checked(self) -> list[int]:
        return sorted(set(self.stages_partial) | set(self.stages_skipped))

    @property
    def meta_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.layer == "meta"]

    @property
    def india_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.layer == "india"]

    @property
    def blocked(self) -> bool:
        return self.verdict is Verdict.BLOCK

    def layer_verdict(self, layer: str) -> Verdict:
        """Meta and Indian layers are reported independently: passing one says
        nothing about the other."""
        relevant = [f for f in self.findings if f.layer == layer]
        if any(f.severity is Severity.BLOCK for f in relevant):
            return Verdict.BLOCK
        if any(f.severity is Severity.WARN for f in relevant):
            return Verdict.WARN
        return Verdict.PASS


class Judge(Protocol):
    """Semantic adjudication for rules a regex cannot decide.

    Screened cheaply and adjudicated expensively (PRD 19.2 lever 2). When no
    judge is configured, llm_judge rules are reported as not evaluated - never
    as passed.
    """

    def evaluate(self, rule: PolicyRule, bundle: CreativeBundle) -> tuple[bool, str | None]:
        """Return (violates, quoted_evidence)."""
        ...


def _layer_of(rule: PolicyRule) -> str:
    return "meta" if rule.jurisdiction == "meta" else "india"


# What counts as "inside a word" for boundary purposes: anything \w matches,
# plus the whole Devanagari block.
#
# \b alone is wrong here, and wrong in the silent direction. Devanagari vowel
# signs (matras) and the anusvara are Unicode categories Mc and Mn, which \w
# does NOT match - so a term ending in one has no word boundary after it and
# \b(term)\b can never fire at all.
#
# That is not theoretical. Of the Schedule J conditions in the names Indian
# Ayurveda copy actually uses, these five all end in a matra:
#
#     मोटापा (obesity)    नपुंसकता (impotence)   मिर्गी (epilepsy)
#     लकवा (paralysis)    पथरी (kidney stone)
#
# Seeding them under the old matcher would have produced five BLOCK terms that
# are visible in the pack, pass every membership test in the database suite, and
# adjudicate nothing - a legal gate that looks armed and is not. Same family as
# every other defect in this ledger, in the one place it is least affordable.
#
# Latin behaviour is unchanged, including the two cases the original comment was
# written for: "piles" still does not match "compiles", "aids" still does not
# match "braids".
_WORDISH = r"[\w\u0900-\u097F]"


def _term_pattern(terms: Sequence[str]) -> re.Pattern[str]:
    # Longest-first so the fullest term wins the match.
    ordered = sorted((t for t in terms if t.strip()), key=len, reverse=True)
    alternation = "|".join(re.escape(t) for t in ordered)
    return re.compile(
        rf"(?<!{_WORDISH})({alternation})(?!{_WORDISH})", re.IGNORECASE
    )


class ComplianceGate:
    """Runs the pre-flight gate over a creative bundle."""

    def __init__(self, rules: Sequence[PolicyRule], judge: Judge | None = None) -> None:
        self._rules = list(rules)
        self._judge = judge

    def check(self, bundle: CreativeBundle) -> ComplianceResult:
        applicable = [r for r in self._rules if r.applies_to(bundle.business_type)]

        findings: list[Finding] = []
        evaluated: set[int] = set()
        skipped: set[int] = set(UNIMPLEMENTED_STAGES)

        # Stage 1 is claim extraction: it produces the surface the later stages
        # read. Deterministic field enumeration covers it for text; a model
        # would add implied-claim extraction from media.
        if bundle.copy_fields():
            evaluated.add(1)
        if bundle.has_media and self._judge is None:
            # Implied claims carried by imagery or audio were not extracted.
            skipped.add(1)

        for rule in applicable:
            if rule.gate_stage in UNIMPLEMENTED_STAGES:
                continue

            produced = self._apply(rule, bundle)
            if produced is not None:
                findings.extend(produced.findings)
                if produced.evaluated:
                    evaluated.add(rule.gate_stage)
                else:
                    skipped.add(rule.gate_stage)

        # Partition into three disjoint sets: a stage where some rules ran and
        # others could not is partial, not both.
        partial = evaluated & skipped
        fully_evaluated = evaluated - partial
        fully_skipped = skipped - partial

        verdict = self._roll_up(findings, partial | fully_skipped)
        return ComplianceResult(
            verdict=verdict,
            findings=findings,
            stages_evaluated=sorted(fully_evaluated),
            stages_partial=sorted(partial),
            stages_skipped=sorted(fully_skipped),
            overall_risk=self._risk(findings),
        )

    # -- rule dispatch ----------------------------------------------------

    @dataclass(slots=True)
    class _RuleOutput:
        findings: list[Finding]
        evaluated: bool

    def _apply(self, rule: PolicyRule, bundle: CreativeBundle) -> "_RuleOutput | None":
        if rule.rule_type is RuleType.TERM_LIST:
            return self._RuleOutput(self._match_terms(rule, bundle), True)
        if rule.rule_type is RuleType.REGEX:
            # Returns its own _RuleOutput, for the same reason _check_state
            # does: a regex rule CAN fail to run, and hard-coding
            # `evaluated=True` here turned "this rule could not compile" into
            # "this rule ran and found nothing".
            #
            # That fix was made for state checks and did not reach this branch,
            # two lines away.
            return self._match_regex(rule, bundle)
        if rule.rule_type is RuleType.STATE_CHECK:
            # Returns its own _RuleOutput. A state check can fail to run - the
            # state it compares against may simply not be on file - and the
            # caller used to hard-code `evaluated=True`, which turned "we could
            # not look" into "we looked and it was fine".
            return self._check_state(rule, bundle)
        if rule.rule_type is RuleType.LLM_JUDGE:
            if self._judge is None:
                return self._RuleOutput([], False)
            violates, evidence = self._judge.evaluate(rule, bundle)
            findings = [self._finding(rule, offending_span=evidence)] if violates else []
            return self._RuleOutput(findings, True)
        return None

    def _searchable(self, bundle: CreativeBundle) -> list[tuple[str, str]]:
        fields = bundle.copy_fields()
        # The landing page first fold is part of the bundle Meta reads, but we
        # only search it when it was actually supplied. Stage 7 - fetching it -
        # is unimplemented, so an absent value is a gap, not a pass.
        if bundle.lp_first_fold_text:
            fields.append(("lp_first_fold_text", bundle.lp_first_fold_text))
        return fields

    def _match_terms(self, rule: PolicyRule, bundle: CreativeBundle) -> list[Finding]:
        if not rule.terms:
            return []
        pattern = _term_pattern(rule.terms)
        out: list[Finding] = []
        for name, text in self._searchable(bundle):
            for m in pattern.finditer(text):
                out.append(
                    self._finding(
                        rule,
                        field=name,
                        offending_span=m.group(0),
                        span=(m.start(), m.end()),
                    )
                )
        return out

    def _match_regex(self, rule: PolicyRule, bundle: CreativeBundle) -> "_RuleOutput":
        if not rule.pattern:
            # A regex rule with no pattern matches nothing and never could. The
            # database refuses to store one (policy_rules_has_matcher), so this
            # is only reachable from a PolicyRule built in code - and it is
            # un-evaluated rather than clean for the same reason as below.
            return self._RuleOutput([], False)

        try:
            compiled = re.compile(rule.pattern, re.IGNORECASE)
        except re.error:
            # THIS is what the old comment said it did and did not.
            #
            # It returned `[]`, and the caller wrapped that as
            # `_RuleOutput(..., True)` - so a BLOCK rule that could not compile
            # reported the bundle clean AND marked its stage checked. The gate
            # then certified a creative nobody had actually adjudicated.
            #
            # PolicyRuleLoader now refuses to serve a ruleset containing one at
            # all, so in production this is unreachable. It stays because a
            # PolicyRule can be built in code, and because "unreachable" is a
            # property of today's callers rather than of this function.
            return self._RuleOutput([], False)

        out: list[Finding] = []
        for name, text in self._searchable(bundle):
            for m in compiled.finditer(text):
                out.append(
                    self._finding(
                        rule,
                        field=name,
                        offending_span=m.group(0),
                        span=(m.start(), m.end()),
                    )
                )
        return self._RuleOutput(out, True)

    def _check_state(self, rule: PolicyRule, bundle: CreativeBundle) -> "_RuleOutput":
        if rule.code == "META_AI_DISCLOSURE_REQUIRED":
            # Undeclared is not the same as "not AI". An unanswered question on
            # a media bundle is itself the violation.
            if bundle.has_media and bundle.ai_generated_declared is None:
                return self._RuleOutput([self._finding(rule, field="ai_generated_declared")], True)
            return self._RuleOutput([], True)

        if rule.code == "IN_AYUSH_LICENCE_ON_FILE":
            posture = bundle.licence_posture

            # No posture, or one that names no single governing product. There
            # is nothing to compare the copy against, and a guard with nothing
            # to compare against refuses rather than permits: stage 9 is
            # reported NOT EVALUATED, which _roll_up then refuses to certify.
            #
            # Deliberately not a BLOCK. A block halts the owner's turn, and the
            # thing that is wrong here is our knowledge, not their copy - false
            # blocks teach owners to override the gate (PRD 13.4).
            if posture is None or not posture.resolved:
                return self._RuleOutput([], False)

            missing = []
            if not posture.ayush_licence_no:
                missing.append("AYUSH licence number")
            if not posture.classification or posture.classification == "unverified":
                missing.append("product classification")
            if missing:
                where = f" for {posture.sku}" if posture.sku else ""
                return self._RuleOutput(
                    [
                        self._finding(
                            rule,
                            field="licence_posture",
                            offending_span=", ".join(missing) + " not on file" + where,
                        )
                    ],
                    True,
                )
            return self._RuleOutput([], True)

        # A state_check this build does not know how to run. Reported as not
        # evaluated, never as a pass - the same commitment UNIMPLEMENTED_STAGES
        # makes for whole stages, held here for individual rules.
        return self._RuleOutput([], False)

    # -- helpers ----------------------------------------------------------

    def _finding(
        self,
        rule: PolicyRule,
        *,
        field: str | None = None,
        offending_span: str | None = None,
        span: tuple[int, int] | None = None,
    ) -> Finding:
        return Finding(
            rule_code=rule.code,
            layer=_layer_of(rule),
            instrument=rule.instrument,
            stage=rule.gate_stage,
            severity=rule.severity,
            title=rule.title,
            explanation=rule.explanation,
            source_url=rule.source_url,
            as_of=rule.as_of,
            field=field,
            offending_span=offending_span,
            span_start=span[0] if span else None,
            span_end=span[1] if span else None,
            suggested_rewrite=rule.remedy_template,
            needs_legal_verification="needs_legal_verification" in rule.explanation,
        )

    @staticmethod
    def _roll_up(findings: Sequence[Finding], skipped: set[int]) -> Verdict:
        if any(f.severity is Severity.BLOCK for f in findings):
            return Verdict.BLOCK
        if any(f.severity is Severity.WARN for f in findings):
            return Verdict.WARN
        # Nothing fired, but stages were skipped. This is NOT a pass: the gate
        # cannot claim a clean bill of health for checks it never ran.
        if skipped:
            return Verdict.NOT_EVALUATED
        return Verdict.PASS

    @staticmethod
    def _risk(findings: Sequence[Finding]) -> float:
        if not findings:
            return 0.0
        weights = {Severity.BLOCK: 1.0, Severity.WARN: 0.4, Severity.INFO: 0.1}
        return min(1.0, max(weights[f.severity] for f in findings))
