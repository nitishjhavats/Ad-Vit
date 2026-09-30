"""The Meta gateway's driver interface.

No agent has direct network access to Meta (PRD 10.7). Every call goes through
a driver behind the policy layer, which owns versioning, batching, retries,
quota accounting and audit.

Three drivers share this interface so the fifteen-step tool pipeline never
learns which one it is talking to:

  FixtureDriver   replays a captured snapshot. Cannot reach the network, and
                  therefore cannot spend money. The default.
  GraphApiDriver  the real Graph API.
  AdsMcpDriver    reserved - a runtime-held MCP client against Meta's hosted
                  Ads MCP endpoint. Not implemented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class EntityLevel(str, Enum):
    ACCOUNT = "ad_account"
    CAMPAIGN = "campaign"
    AD_SET = "adset"
    AD = "ad"

    @property
    def db(self) -> str:
        """The vocabulary t_advit.metrics_daily.level actually accepts.

        Meta says `ad_account` and `adset`; the CHECK constraint says `account`
        and `ad_set`. Two of the four labels differ, which is the worst possible
        proportion: `campaign` and `ad` round-trip unchanged, so a writer that
        passed `.value` straight through would work in half its tests and raise
        on the other half - and if the constraint were ever loosened, would
        write two spellings of one level and make every `group by level` wrong.

        EntityStatus already carries exactly this property for exactly this
        reason. EntityLevel did not, because nothing wrote a level to the
        database until ingestion existed.
        """
        return {
            EntityLevel.ACCOUNT: "account",
            EntityLevel.AD_SET: "ad_set",
        }.get(self, self.value)


class EntityStatus(str, Enum):
    """Meta's vocabulary, because that is what arrives on the wire.

    The database has its own: `t_advit.entity_status` is lower case. The two
    meet whenever a status is serialised into JSON that SQL later reads, and
    they met silently once already - the committed-spend CTE compared
    `after_state_json->>'status'` against 'active', matched nothing, and
    reported zero, which left the daily cap unable to accumulate. No error, no
    warning, just a wrong number in a guardrail.

    So the translation is explicit and has exactly one home: `.value` is Meta's
    form and belongs on the wire, `.db` is the database's and belongs in
    anything Postgres will read.
    """

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"
    DELETED = "DELETED"

    @property
    def db(self) -> str:
        """The `t_advit.entity_status` label for this status."""
        return self.value.lower()

    @classmethod
    def _missing_(cls, value: object) -> "EntityStatus | None":
        """Accept either vocabulary when parsing.

        Fixtures and Graph API responses carry Meta's upper case; a value read
        back out of the database carries the other. Parsing should not be the
        place this distinction bites.
        """
        if isinstance(value, str):
            upper = value.upper()
            for member in cls:
                if member.value == upper:
                    return member
        return None


class MetaErrorKind(str, Enum):
    """Errors are classified, because the correct response differs per class
    (PRD 7.2). A transient error is retried; a permission error is escalated to
    the user with the exact fix; a policy error goes to Compliance Guard.
    """

    TRANSIENT = "transient"
    QUOTA = "quota"
    PERMISSION = "permission"
    POLICY = "policy"
    NOT_FOUND = "not_found"
    VALIDATION = "validation"
    UNKNOWN = "unknown"


class MetaError(Exception):
    def __init__(
        self,
        kind: MetaErrorKind,
        message: str,
        *,
        external_request_id: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.external_request_id = external_request_id
        self.retryable = (
            retryable
            if retryable is not None
            else kind in (MetaErrorKind.TRANSIENT, MetaErrorKind.QUOTA)
        )


class WriteForbidden(MetaError):
    """Raised when a write targets an account the driver is not permitted to
    mutate. Two independent switches must both allow it: the operator's
    allowlist and the product's meta_connections.write_enabled column."""

    def __init__(self, ad_account_id: str, reason: str) -> None:
        super().__init__(
            MetaErrorKind.PERMISSION,
            f"writes to ad account {ad_account_id} are not permitted: {reason}",
            retryable=False,
        )
        self.ad_account_id = ad_account_id


@dataclass(frozen=True, slots=True)
class Entity:
    id: str
    level: EntityLevel
    name: str
    status: EntityStatus = EntityStatus.PAUSED
    parent_id: str | None = None
    ad_account_id: str = ""
    fields: dict[str, Any] = field(default_factory=dict)

    def state(self) -> dict[str, Any]:
        """The comparable snapshot used for verification and rollback."""
        return {
            "id": self.id,
            "level": self.level.value,
            "name": self.name,
            # The database's vocabulary, not Meta's: this dict is persisted to
            # t_advit.actions and read back by SQL that compares against
            # t_advit.entity_status.
            "status": self.status.db,
            "parent_id": self.parent_id,
            **self.fields,
        }


@dataclass(frozen=True, slots=True)
class WriteResult:
    entity: Entity
    external_request_id: str | None = None
    quota_consumed: int = 0


@dataclass(frozen=True, slots=True)
class Insight:
    """One day of measured performance for one entity.

    Every numeric field is `| None`, and that is the whole design. Meta omits a
    field it has no data for rather than returning zero, and the difference
    matters more here than almost anywhere else in this system: `results = 0`
    means the ads ran and nobody converted, `results = None` means Meta did not
    report it. The first is a signal to pause; the second is a signal to fix the
    measurement. Collapsing them - which `int(row.get("results", 0))` does
    silently - has already been the shape of several defects in this repository.

    `source` travels with the data rather than being decided by the writer, so
    an ingestion path cannot label a fixture number as measurement by forgetting
    an argument.
    """

    date: str                  # ISO, in the ad account's own timezone
    level: EntityLevel
    entity_id: str
    ad_account_id: str
    source: str = "meta"

    spend_inr: float | None = None
    impressions: int | None = None
    reach: int | None = None
    clicks: int | None = None
    link_clicks: int | None = None
    results: int | None = None
    purchases: int | None = None
    purchase_value_inr: float | None = None
    # Which attribution window produced these numbers. Comparing a figure from
    # one regime against a figure from another is not a comparison at all, so it
    # is carried with every row rather than assumed globally.
    attribution_regime: str = "post_2026_03"


@dataclass(slots=True)
class QuotaState:
    """Business Use Case quota. Exceeding either bucket affects the whole ad
    account's API access, so utilisation is tracked and non-critical work
    yields before safety checks do (PRD 2.2, 6.3)."""

    ads_management_pct: float = 0.0
    ads_insights_pct: float = 0.0
    reset_at: str | None = None

    @property
    def under_pressure(self) -> bool:
        return max(self.ads_management_pct, self.ads_insights_pct) >= 80.0


@runtime_checkable
class MetaDriver(Protocol):
    """What the tool pipeline is allowed to ask of Meta.

    Note what is absent: there is no ``create_and_activate``. Creation and
    activation are separate calls in separate risk classes, because that is the
    single best safety pattern available here (PRD 10.8) - a misread budget
    becomes a paused artefact costing nothing, rather than a live campaign
    burning money at ten times the intended rate.
    """

    name: str

    def get_entities(
        self, ad_account_id: str, level: EntityLevel, *, parent_id: str | None = None
    ) -> list[Entity]: ...

    def get_entity(self, ad_account_id: str, entity_id: str) -> Entity | None: ...

    def get_datasets(self, ad_account_id: str) -> list[dict[str, Any]]: ...

    def get_insights(
        self,
        ad_account_id: str,
        *,
        since: date,
        until: date,
        level: EntityLevel = EntityLevel.ACCOUNT,
    ) -> list[Insight]:
        """Measured performance, day by day, for a closed date range.

        Inclusive of both ends, because that is how Meta's `time_range` behaves
        and a driver that quietly differed would make every reconciliation off
        by one day at one end.
        """
        ...

    def create_campaign(
        self, ad_account_id: str, *, name: str, objective: str, idempotency_key: str
    ) -> WriteResult:
        """Creates PAUSED. Always."""
        ...

    def create_ad_set(
        self,
        ad_account_id: str,
        *,
        campaign_id: str,
        name: str,
        daily_budget_inr: float,
        optimisation_event: str,
        idempotency_key: str,
    ) -> WriteResult:
        """Creates PAUSED. Always."""
        ...

    def update_status(
        self, ad_account_id: str, entity_id: str, status: EntityStatus, *, idempotency_key: str
    ) -> WriteResult:
        """Activation is a CRITICAL-class action requiring its own approval."""
        ...

    def update_budget(
        self, ad_account_id: str, entity_id: str, daily_budget_inr: float, *, idempotency_key: str
    ) -> WriteResult: ...

    def quota(self, ad_account_id: str) -> QuotaState: ...
