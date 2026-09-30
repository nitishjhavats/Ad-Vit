"""FixtureDriver - replays a captured snapshot of the real ad accounts.

The default driver, deliberately. It has no network client at all, so no
configuration mistake and no agent error can make it spend money. It is also
the substrate for the golden dataset (PRD 17.8): regression scenarios need a
Meta that behaves identically on every run.

Writes are simulated, but they are simulated *faithfully* - created objects
land in a store, so the pipeline's verification read genuinely reads back the
object that was created and genuinely diffs it against the proposal. A driver
that returned a canned success would leave step 11 of the tool pipeline
untested, which is the one step that catches a silent partial write.
"""

from __future__ import annotations

import json
import threading
import zlib
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.meta.driver import (
    Entity,
    EntityLevel,
    EntityStatus,
    Insight,
    MetaError,
    MetaErrorKind,
    QuotaState,
    WriteForbidden,
    WriteResult,
)

_SNAPSHOT_RELATIVE = Path("packages/marketing-db/fixtures/meta/snapshot.json")


def _find_default_snapshot() -> Path:
    """Walk up to the repo root rather than counting parent directories.

    A hard-coded ``parents[n]`` breaks silently the moment the tree is
    reorganised, and the failure looks like a missing fixture rather than a
    wrong path.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / _SNAPSHOT_RELATIVE
        if candidate.exists():
            return candidate
    # Nothing found: return the most likely location so the error message
    # names a real path.
    return here.parents[-1] / _SNAPSHOT_RELATIVE


DEFAULT_SNAPSHOT = _find_default_snapshot()


class FixtureDriver:
    name = "fixture"

    def __init__(
        self,
        snapshot_path: Path | str | None = None,
        *,
        write_allowlist: frozenset[str] | set[str] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        if snapshot is not None:
            self._snapshot = snapshot
        else:
            path = Path(snapshot_path or DEFAULT_SNAPSHOT)
            if not path.exists():
                raise FileNotFoundError(f"Meta fixture snapshot not found at {path}")
            self._snapshot = json.loads(path.read_text(encoding="utf-8"))

        self._allowlist = frozenset(write_allowlist or ())
        self._lock = threading.Lock()

        # Simulated mutations, layered over the immutable snapshot.
        self._created: dict[str, Entity] = {}
        self._status_overrides: dict[str, EntityStatus] = {}
        self._budget_overrides: dict[str, float] = {}

        # idempotency_key -> entity_id, so a retry after an ambiguous timeout
        # resolves to the original object instead of creating a second one.
        self._by_idempotency_key: dict[str, str] = {}

        self._seq = 0
        self._quota_reads = 0
        self._quota_writes = 0

    # -- reads -------------------------------------------------------------

    def get_entities(
        self, ad_account_id: str, level: EntityLevel, *, parent_id: str | None = None
    ) -> list[Entity]:
        self._quota_reads += 1
        key = {
            EntityLevel.CAMPAIGN: "campaigns",
            EntityLevel.AD_SET: "adsets",
            EntityLevel.AD: "ads",
        }.get(level)

        if key is None:
            raise MetaError(
                MetaErrorKind.VALIDATION, f"cannot list entities at level {level.value}"
            )

        raw = self._snapshot.get(key, {}).get(ad_account_id)
        if raw is None:
            raise MetaError(
                MetaErrorKind.NOT_FOUND,
                f"ad account {ad_account_id} is not present in the fixture snapshot",
            )

        out = [
            Entity(
                id=str(item["id"]),
                level=level,
                name=item["name"],
                status=self._status_overrides.get(
                    str(item["id"]), EntityStatus(item.get("status", "ACTIVE"))
                ),
                parent_id=item.get("parent_id"),
                ad_account_id=ad_account_id,
                # _with_budget, not a bare comprehension: a snapshot entity
                # whose budget was later changed must read back changed here
                # too, exactly as it does from get_entity.
                fields=self._with_budget(str(item["id"]), item),
            )
            for item in raw
        ]
        out.extend(
            # _materialise, for the same reason get_entity applies it: a created
            # entity that has since been activated or re-budgeted must not read
            # back in its original state. Without this the double contradicted
            # itself - get_entity called an ad set ACTIVE while get_entities
            # still called it PAUSED - and any caller that reasoned over a
            # campaign's children got the world as it was at creation.
            self._materialise(e)
            for e in self._created.values()
            if e.ad_account_id == ad_account_id and e.level is level
        )

        if parent_id is not None:
            out = [e for e in out if e.parent_id == parent_id]
        return out

    def get_entity(self, ad_account_id: str, entity_id: str) -> Entity | None:
        self._quota_reads += 1
        entity_id = str(entity_id)

        if entity_id in self._created:
            return self._materialise(self._created[entity_id])

        for key, level in (
            ("campaigns", EntityLevel.CAMPAIGN),
            ("adsets", EntityLevel.AD_SET),
            ("ads", EntityLevel.AD),
        ):
            for item in self._snapshot.get(key, {}).get(ad_account_id, []):
                if str(item["id"]) == entity_id:
                    return Entity(
                        id=entity_id,
                        level=level,
                        name=item["name"],
                        status=self._status_overrides.get(
                            entity_id, EntityStatus(item.get("status", "ACTIVE"))
                        ),
                        parent_id=item.get("parent_id"),
                        ad_account_id=ad_account_id,
                        fields=self._with_budget(entity_id, item),
                    )
        return None

    def get_datasets(self, ad_account_id: str) -> list[dict[str, Any]]:
        self._quota_reads += 1
        return list(self._snapshot.get("datasets", {}).get(ad_account_id, []))

    def get_account(self, ad_account_id: str) -> dict[str, Any] | None:
        for acct in self._snapshot.get("ad_accounts", []):
            if str(acct["ad_account_id"]) == str(ad_account_id):
                return dict(acct)
        return None

    # -- writes ------------------------------------------------------------

    def create_campaign(
        self, ad_account_id: str, *, name: str, objective: str, idempotency_key: str
    ) -> WriteResult:
        self._assert_writable(ad_account_id)
        with self._lock:
            existing = self._replay(idempotency_key)
            if existing is not None:
                return existing

            entity = Entity(
                id=self._next_id("campaign"),
                level=EntityLevel.CAMPAIGN,
                name=name,
                # Never create a live object (PRD 10.8). Activation is a
                # separate call in a separate risk class.
                status=EntityStatus.PAUSED,
                ad_account_id=ad_account_id,
                fields={"objective": objective},
            )
            return self._record(entity, idempotency_key)

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
        self._assert_writable(ad_account_id)
        if daily_budget_inr <= 0:
            raise MetaError(
                MetaErrorKind.VALIDATION, f"daily budget must be positive, got {daily_budget_inr}"
            )
        if self.get_entity(ad_account_id, campaign_id) is None:
            raise MetaError(
                MetaErrorKind.NOT_FOUND, f"parent campaign {campaign_id} does not exist"
            )

        with self._lock:
            existing = self._replay(idempotency_key)
            if existing is not None:
                return existing

            entity = Entity(
                id=self._next_id("adset"),
                level=EntityLevel.AD_SET,
                name=name,
                status=EntityStatus.PAUSED,
                parent_id=str(campaign_id),
                ad_account_id=ad_account_id,
                fields={
                    "daily_budget_inr": daily_budget_inr,
                    "optimisation_event": optimisation_event,
                },
            )
            return self._record(entity, idempotency_key)

    def update_status(
        self, ad_account_id: str, entity_id: str, status: EntityStatus, *, idempotency_key: str
    ) -> WriteResult:
        self._assert_writable(ad_account_id)
        entity = self.get_entity(ad_account_id, entity_id)
        if entity is None:
            raise MetaError(MetaErrorKind.NOT_FOUND, f"entity {entity_id} does not exist")

        with self._lock:
            existing = self._replay(idempotency_key)
            if existing is not None:
                return existing

            self._status_overrides[str(entity_id)] = status
            self._quota_writes += 1
            self._by_idempotency_key[idempotency_key] = str(entity_id)
            return WriteResult(
                entity=replace(entity, status=status),
                external_request_id=f"fixture-req-{len(self._by_idempotency_key)}",
                quota_consumed=3,
            )

    def update_budget(
        self, ad_account_id: str, entity_id: str, daily_budget_inr: float, *, idempotency_key: str
    ) -> WriteResult:
        self._assert_writable(ad_account_id)
        if daily_budget_inr <= 0:
            raise MetaError(
                MetaErrorKind.VALIDATION, f"daily budget must be positive, got {daily_budget_inr}"
            )
        entity = self.get_entity(ad_account_id, entity_id)
        if entity is None:
            raise MetaError(MetaErrorKind.NOT_FOUND, f"entity {entity_id} does not exist")

        with self._lock:
            existing = self._replay(idempotency_key)
            if existing is not None:
                return existing

            self._budget_overrides[str(entity_id)] = daily_budget_inr
            self._quota_writes += 1
            self._by_idempotency_key[idempotency_key] = str(entity_id)
            updated = replace(
                entity, fields={**entity.fields, "daily_budget_inr": daily_budget_inr}
            )
            return WriteResult(
                entity=updated,
                external_request_id=f"fixture-req-{len(self._by_idempotency_key)}",
                quota_consumed=3,
            )

    # -- quota -------------------------------------------------------------

    def quota(self, ad_account_id: str) -> QuotaState:
        # Development tier is ~60 points: reads cost 1, writes 3. Modelled so
        # quota-pressure behaviour is exercisable without a live account.
        consumed = self._quota_reads + (self._quota_writes * 3)
        return QuotaState(
            ads_management_pct=min(100.0, consumed / 60.0 * 100.0),
            ads_insights_pct=min(100.0, self._quota_reads / 60.0 * 100.0),
            reset_at=None,
        )

    # -- internals ---------------------------------------------------------

    def _assert_writable(self, ad_account_id: str) -> None:
        """The operator's switch. The product's switch
        (meta_connections.write_enabled) is checked separately by the tool
        pipeline - an account must clear BOTH."""
        ad_account_id = str(ad_account_id)
        if self.get_account(ad_account_id) is None:
            raise MetaError(
                MetaErrorKind.NOT_FOUND,
                f"ad account {ad_account_id} is not present in the fixture snapshot",
            )
        if ad_account_id not in self._allowlist:
            raise WriteForbidden(ad_account_id, "not in META_WRITE_ALLOWLIST")

    def _replay(self, idempotency_key: str) -> WriteResult | None:
        """A repeated key returns the original object. Creating a duplicate
        campaign because a response was lost in transit is not an acceptable
        outcome (PRD 10.9)."""
        prior_id = self._by_idempotency_key.get(idempotency_key)
        if prior_id is None:
            return None
        entity = self._created.get(prior_id)
        if entity is None:
            return None
        return WriteResult(
            entity=self._materialise(entity),
            external_request_id=f"fixture-replay-{prior_id}",
            quota_consumed=0,
        )

    # ------------------------------------------------------------------
    # Insights
    # ------------------------------------------------------------------

    def get_insights(
        self,
        ad_account_id: str,
        *,
        since: date,
        until: date,
        level: EntityLevel = EntityLevel.ACCOUNT,
    ) -> list[Insight]:
        """Deterministic synthetic performance, labelled as synthetic, and
        internally consistent.

        The snapshot this driver replays says in its own `_meta.note` that
        metrics were deliberately NOT captured, "because metric fixtures go
        stale silently and invite false confidence". That judgement is right and
        it cannot survive an ingestion path: something has to come back here or
        the loop is unexercisable offline.

        So these numbers exist and are marked. Every Insight carries
        `source="fixture"`, `t_advit.metrics_daily.source` stores it, and
        `t_advit.has_measured_spend` refuses to count it - which is what stops a
        developer's offline run from looking, to the spend cap, exactly like a
        synced production account.

        **The account figure is the SUM of the campaign figures**, not an
        independently generated number, and that is not cosmetic. The ingestion
        reconciles the two levels against each other; it is the only check in
        the system capable of noticing that a sync silently dropped a campaign.
        A fixture whose levels disagree by construction makes that check fire on
        every single day, and a check that always fires is one people learn to
        ignore - which is worse than not having it.

        Deterministic rather than random: derived from the date and the entity
        id, so two runs agree and a test can assert an actual number.

        The gap is deliberate. Every seventh day reports no `results` at all -
        not zero, absent - because that is what Meta does while a conversion
        window is still open, and a pipeline that has never seen the difference
        between "nobody converted" and "not reported yet" will collapse them the
        first time it meets one.
        """
        if until < since:
            raise MetaError(
                MetaErrorKind.VALIDATION,
                f"time range ends ({until}) before it begins ({since})",
            )
        if (until - since).days > 92:
            # Meta caps a single insights call at 92 days. A driver that quietly
            # accepted more would silently truncate in production and not here.
            raise MetaError(
                MetaErrorKind.VALIDATION,
                f"insights range of {(until - since).days} days exceeds the 92-day maximum",
            )
        if level in (EntityLevel.AD_SET, EntityLevel.AD):
            raise MetaError(
                MetaErrorKind.VALIDATION,
                f"the fixture driver reports insights at account and campaign level only, "
                f"not {level.value}",
            )

        self._quota_reads += 1

        campaigns = [e.id for e in self.get_entities(ad_account_id, EntityLevel.CAMPAIGN)]

        out: list[Insight] = []
        day = since
        while day <= until:
            per_campaign = [
                self._synthesise(day, campaign_id, EntityLevel.CAMPAIGN, ad_account_id)
                for campaign_id in campaigns
            ]

            if level is EntityLevel.CAMPAIGN:
                out.extend(per_campaign)
            else:
                out.append(self._roll_up(day, ad_account_id, per_campaign))

            day += timedelta(days=1)
        return out

    def _synthesise(
        self, day: date, entity_id: str, level: EntityLevel, ad_account_id: str
    ) -> Insight:
        seed = zlib.crc32(f"{day.isoformat()}|{entity_id}".encode())
        spend = round(120.0 + (seed % 900), 2)
        impressions = 3_000 + (seed % 18_000)
        clicks = max(1, impressions // 60)
        return Insight(
            date=day.isoformat(),
            level=level,
            entity_id=entity_id,
            ad_account_id=ad_account_id,
            source="fixture",
            spend_inr=spend,
            impressions=impressions,
            reach=int(impressions * 0.72),
            clicks=clicks,
            link_clicks=int(clicks * 0.74),
            # Absent, not zero, one day in seven.
            results=None if day.toordinal() % 7 == 0 else max(1, clicks // 28),
            purchases=None,
            purchase_value_inr=None,
        )

    def _roll_up(
        self, day: date, ad_account_id: str, rows: list[Insight]
    ) -> Insight:
        """The account-level row, summed from its campaigns.

        `results` is summed as None when EVERY campaign reported None and as a
        number otherwise - which is what an account total means. Summing None as
        zero here would reintroduce, one level up, the exact conflation the
        per-campaign None exists to demonstrate.

        An account with no campaigns reports a measured zero rather than NULL:
        it exists, it ran nothing, and it spent nothing. That is a fact, not a
        gap.
        """
        reported = [r.results for r in rows if r.results is not None]
        return Insight(
            date=day.isoformat(),
            level=EntityLevel.ACCOUNT,
            entity_id=ad_account_id,
            ad_account_id=ad_account_id,
            source="fixture",
            spend_inr=round(sum(r.spend_inr or 0.0 for r in rows), 2),
            impressions=sum(r.impressions or 0 for r in rows),
            reach=sum(r.reach or 0 for r in rows),
            clicks=sum(r.clicks or 0 for r in rows),
            link_clicks=sum(r.link_clicks or 0 for r in rows),
            results=sum(reported) if reported or not rows else None,
            purchases=None,
            purchase_value_inr=None,
        )

    def _record(self, entity: Entity, idempotency_key: str) -> WriteResult:
        self._created[entity.id] = entity
        self._by_idempotency_key[idempotency_key] = entity.id
        self._quota_writes += 1
        return WriteResult(
            entity=entity,
            external_request_id=f"fixture-req-{len(self._by_idempotency_key)}",
            quota_consumed=3,
        )

    def _materialise(self, entity: Entity) -> Entity:
        """Apply any later status or budget mutations to a stored entity."""
        status = self._status_overrides.get(entity.id, entity.status)
        fields = dict(entity.fields)
        if entity.id in self._budget_overrides:
            fields["daily_budget_inr"] = self._budget_overrides[entity.id]
        return replace(entity, status=status, fields=fields)

    def _with_budget(self, entity_id: str, item: dict[str, Any]) -> dict[str, Any]:
        fields = {
            k: v for k, v in item.items() if k not in {"id", "name", "status", "parent_id"}
        }
        if entity_id in self._budget_overrides:
            fields["daily_budget_inr"] = self._budget_overrides[entity_id]
        return fields

    def _next_id(self, kind: str) -> str:
        self._seq += 1
        return f"fx-{kind}-{self._seq:06d}"
