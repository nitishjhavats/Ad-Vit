"""Account audit (PRD 6.1 step 4, FR-005 and FR-008).

The first moment of value in onboarding, and it costs nothing to deliver: a
structural and measurement audit producing a scored punch-list before the OS
proposes anything at all.

Deliberately deterministic. Every finding here is derived by reading the
account, not by asking a model - which makes the audit reproducible, cheap, and
impossible to hallucinate. PRD 17.7: arithmetic is never done by a model.

Measurement readiness is a gate, not a report. PRD 12.2 makes the closed loop
the product's core claim, and that loop needs a dataset to upload confirmed
sales into. An account with no dataset cannot run it at all, so that finding is
blocking rather than advisory.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.meta.driver import EntityLevel, MetaDriver


class Severity(str, Enum):
    BLOCKING = "blocking"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


# Machine-parseable naming convention (PRD 10.1). Names are generated, never
# hand-typed, so every segment maps to a warehouse column and tag-level
# analysis needs no separate tagging discipline.
CAMPAIGN_NAME_PATTERN = re.compile(
    r"^[A-Z0-9]{2,6}\|[A-Z]{3,4}\|[A-Z]+\|[A-Z]{3,6}\|\d{4}\|\d{2}$"
)
AD_SET_NAME_PATTERN = re.compile(r"^[A-Z]{3,4}\|[A-Z]+\|[A-Z0-9-]+\|[A-Z]+\|\d{2}$")

# Names Meta generates when the advertiser does not supply one.
META_DEFAULT_NAME = re.compile(r"^New\s+.*(Ad Set|Campaign|Ad)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Severity
    title: str
    detail: str
    remedy: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def weight(self) -> int:
        return {
            Severity.BLOCKING: 40,
            Severity.HIGH: 20,
            Severity.MEDIUM: 10,
            Severity.LOW: 4,
            Severity.INFO: 0,
        }[self.severity]


@dataclass(slots=True)
class AuditReport:
    ad_account_id: str
    score: int
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    measurement_ready: bool = False

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKING]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ad_account_id": self.ad_account_id,
            "score": self.score,
            "measurement_ready": self.measurement_ready,
            "counts": self.counts,
            "findings": [
                {
                    "code": f.code,
                    "severity": f.severity.value,
                    "title": f.title,
                    "detail": f.detail,
                    "remedy": f.remedy,
                    "evidence": f.evidence,
                }
                for f in self.findings
            ],
        }


class AccountAuditor:
    def __init__(self, driver: MetaDriver) -> None:
        self._driver = driver

    def audit(self, ad_account_id: str) -> AuditReport:
        campaigns = self._driver.get_entities(ad_account_id, EntityLevel.CAMPAIGN)
        ad_sets = self._driver.get_entities(ad_account_id, EntityLevel.AD_SET)
        ads = self._driver.get_entities(ad_account_id, EntityLevel.AD)
        datasets = self._driver.get_datasets(ad_account_id)

        findings: list[Finding] = []
        findings += self._measurement(ad_account_id, datasets)
        findings += self._naming(campaigns, ad_sets)
        findings += self._structure(campaigns, ad_sets, ads)
        findings += self._creative_supply(ads)

        score = max(0, 100 - sum(f.weight for f in findings))
        return AuditReport(
            ad_account_id=ad_account_id,
            score=score,
            findings=findings,
            counts={
                "campaigns": len(campaigns),
                "ad_sets": len(ad_sets),
                "ads": len(ads),
                "datasets": len(datasets),
            },
            measurement_ready=bool(datasets),
        )

    # -- measurement -------------------------------------------------------

    def _measurement(self, ad_account_id: str, datasets: list[dict[str, Any]]) -> list[Finding]:
        if not datasets:
            return [
                Finding(
                    code="NO_DATASET",
                    severity=Severity.BLOCKING,
                    title="No dataset (pixel) on the ad account",
                    detail=(
                        "The account has no dataset, so there is nowhere to send confirmed "
                        "sales back to Meta. Without it the Conversions API cannot run, "
                        "pixel and CAPI events cannot be deduplicated on a shared event_id, "
                        "and Event Match Quality cannot be measured or improved. The closed "
                        "loop this product optimises - feeding delivered, non-RTO sales back "
                        "so the algorithm learns the same truth the business lives on - is "
                        "unavailable until one exists."
                    ),
                    remedy=(
                        "Create a dataset in Events Manager and attach it to this ad "
                        "account, then verify event volume and match quality before "
                        "enabling any autonomous optimisation."
                    ),
                    evidence={"dataset_count": 0},
                )
            ]

        return [
            Finding(
                code="DATASET_QUALITY_UNVERIFIED",
                severity=Severity.MEDIUM,
                title="Dataset present but signal quality not yet verified",
                detail=(
                    "A dataset exists. Event volume, Event Match Quality and deduplication "
                    "health have not been read yet, and a low EMQ means events are received "
                    "but barely used for optimisation."
                ),
                remedy=(
                    "Read dataset quality and normalise phone numbers to E.164 before "
                    "hashing - typically the single highest-leverage fix for an Indian "
                    "account."
                ),
                evidence={"dataset_count": len(datasets)},
            )
        ]

    # -- naming ------------------------------------------------------------

    def _naming(self, campaigns, ad_sets) -> list[Finding]:
        findings: list[Finding] = []

        defaults = [e.name for e in list(campaigns) + list(ad_sets) if META_DEFAULT_NAME.match(e.name)]
        if defaults:
            findings.append(
                Finding(
                    code="META_DEFAULT_NAMES",
                    severity=Severity.HIGH,
                    title="Objects still carry Meta's auto-generated names",
                    detail=(
                        f"{len(defaults)} object(s) use the name Meta generates when none is "
                        "supplied. Nothing downstream can tell them apart in a report, and "
                        "an approval that says 'raise the budget on New Leads Ad Set' is "
                        "ambiguous when three of them exist."
                    ),
                    remedy=(
                        "Apply the naming convention so every segment maps to a warehouse "
                        "column: {ACC}|{STAGE}|{OBJ}|{PACK}|{yymm}|{seq} for campaigns and "
                        "{STAGE}|{AUD}|{GEO}|{OPT}|{seq} for ad sets."
                    ),
                    evidence={"names": sorted(set(defaults))[:10]},
                )
            )

        unparseable = [c.name for c in campaigns if not CAMPAIGN_NAME_PATTERN.match(c.name)]
        if unparseable and len(unparseable) == len(list(campaigns)):
            findings.append(
                Finding(
                    code="NO_NAMING_CONVENTION",
                    severity=Severity.MEDIUM,
                    title="No machine-parseable naming convention in use",
                    detail=(
                        "No campaign name parses against the convention, so stage, objective "
                        "and pack cannot be derived from the account itself. Tag-level "
                        "performance analysis then requires a separate tagging discipline "
                        "that nobody maintains."
                    ),
                    remedy="Generate names rather than typing them; rename on the next edit.",
                    evidence={"examples": unparseable[:5]},
                )
            )

        # Near-duplicate names, compared with separators and case removed.
        normalised = Counter(re.sub(r"[\s_|-]+", "", c.name).lower() for c in campaigns)
        collisions = {k: v for k, v in normalised.items() if v > 1}
        if collisions:
            findings.append(
                Finding(
                    code="DUPLICATE_CAMPAIGN_NAMES",
                    severity=Severity.MEDIUM,
                    title="Near-duplicate campaign names",
                    detail=(
                        "Two or more campaigns differ only by spacing or punctuation, which "
                        "makes them indistinguishable in a report and easy to confuse when "
                        "approving a change."
                    ),
                    remedy="Rename so each campaign is identifiable without opening it.",
                    evidence={"collision_count": len(collisions)},
                )
            )

        return findings

    # -- structure ---------------------------------------------------------

    def _structure(self, campaigns, ad_sets, ads) -> list[Finding]:
        findings: list[Finding] = []
        n_campaigns, n_ad_sets = len(list(campaigns)), len(list(ad_sets))

        # Fragmentation is the most common cause of permanent learning-limited
        # status in small accounts (PRD 15.1): the fewer, bigger ad sets you
        # run, the more likely each clears ~50 optimisation events a week.
        if n_campaigns >= 3 and n_ad_sets <= n_campaigns:
            findings.append(
                Finding(
                    code="FRAGMENTED_STRUCTURE",
                    severity=Severity.HIGH,
                    title="Budget fragmented across many campaigns",
                    detail=(
                        f"{n_campaigns} campaigns hold {n_ad_sets} ad set(s) between them - "
                        "roughly one each. Every ad set runs its own learning phase and needs "
                        "about 50 optimisation events a week to leave it, so splitting a "
                        "small budget this way can leave all of them permanently "
                        "learning-limited. One ad set with 25 diverse creatives beat five ad "
                        "sets with five each by ~17% on conversions."
                    ),
                    remedy=(
                        "Consolidate into one acquisition campaign carrying the majority of "
                        "spend, with a separate manual laboratory at 15-25% for testing."
                    ),
                    evidence={"campaigns": n_campaigns, "ad_sets": n_ad_sets},
                )
            )

        if n_campaigns and not n_ad_sets:
            findings.append(
                Finding(
                    code="CAMPAIGNS_WITHOUT_AD_SETS",
                    severity=Severity.MEDIUM,
                    title="Campaigns with no ad sets",
                    detail="Campaigns exist but carry no ad sets, so nothing can deliver.",
                    remedy="Complete or remove the incomplete structures.",
                    evidence={"campaigns": n_campaigns},
                )
            )

        return findings

    # -- creative supply ---------------------------------------------------

    def _creative_supply(self, ads) -> list[Finding]:
        n = len(list(ads))
        if n == 0:
            return [
                Finding(
                    code="NO_ADS_VISIBLE",
                    severity=Severity.INFO,
                    title="No ads read for this account",
                    detail=(
                        "No ads were returned, so creative supply and diversity could not be "
                        "assessed. This is a gap in the audit, not a verdict on the account."
                    ),
                    remedy="Read ad-level entities to assess creative supply and fatigue.",
                    evidence={"ads": 0},
                )
            ]

        if n < 15:
            return [
                Finding(
                    code="LOW_CREATIVE_SUPPLY",
                    severity=Severity.MEDIUM,
                    title="Creative supply below the 2026 target",
                    detail=(
                        f"{n} live ad(s). The fatigue window collapsed to 2-3 weeks, and "
                        "retrieval now reads creative content directly, so diversity is an "
                        "eligibility requirement rather than an optimisation nicety. Target "
                        "15-30 genuinely distinct creatives in the acquisition campaign."
                    ),
                    remedy="Raise creative throughput and vary hook register, not just headline.",
                    evidence={"ads": n},
                )
            ]

        return []
