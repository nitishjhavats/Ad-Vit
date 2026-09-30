"""Wiring. One place where the driver and adapters are chosen.

The driver is selected by configuration, never by a request parameter: a caller
must not be able to talk its way onto the live Graph API by passing a flag.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import Settings, get_settings
from app.meta.driver import MetaDriver
from app.meta.fixture import FixtureDriver
from app.policy.pipeline import ToolPipeline
from app.policy.rules import PolicyRuleLoader
from app.policy.store import (
    PostgresAuditSink,
    PostgresLockManager,
    PostgresOutcomeScheduler,
    PostgresPolicyStore,
)


@lru_cache(maxsize=1)
def get_driver() -> MetaDriver:
    settings = get_settings()

    if settings.meta_driver == "fixture":
        return FixtureDriver(write_allowlist=settings.write_allowlist)

    if settings.meta_driver == "graph":
        raise NotImplementedError(
            "GraphApiDriver is not implemented yet. Set META_DRIVER=fixture. "
            "Building it requires your own Meta app credentials; the session-scoped "
            "Ads MCP connection is not reachable from this process."
        )

    if settings.meta_driver == "adsmcp":
        raise NotImplementedError(
            "AdsMcpDriver is reserved: it would hold its own MCP client against Meta's "
            "hosted Ads MCP endpoint with its own OAuth. Not implemented."
        )

    raise ValueError(f"unknown META_DRIVER {settings.meta_driver!r}")


@lru_cache(maxsize=1)
def get_rule_loader() -> PolicyRuleLoader:
    return PolicyRuleLoader()


@lru_cache(maxsize=1)
def get_pipeline() -> ToolPipeline:
    # No DSN passed anywhere. Each adapter names the connection it needs in its
    # own module, so "which credential does this run on?" is answered next to
    # the query rather than by whatever this function happened to be handed.
    settings: Settings = get_settings()
    return ToolPipeline(
        driver=get_driver(),
        policy=PostgresPolicyStore(),
        audit=PostgresAuditSink(),
        locks=PostgresLockManager(),
        scheduler=PostgresOutcomeScheduler(),
        lock_timeout_s=settings.mutation_lock_timeout_s,
    )
