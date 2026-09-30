"""The privileged connection is safe only while its call sites stay countable.

``service_conn`` is the connection that is not subject to row-level security.
Nothing about it is dangerous in the four modules that are supposed to hold it —
the governance spine genuinely has to write rows a tenant may not write, and the
guardrail arithmetic genuinely has to see rows a tenant may not see.

What is dangerous is it becoming a habit. The design rests on a rule a human
applies per query:

    A read that decides what the CALLER MAY SEE runs on the tenant connection.
    A read that decides what the SYSTEM MAY DO runs on the service connection.

Getting that wrong on a *write* fails loudly — the grant matrix raises
``permission denied``. Getting it wrong on a *read* fails **silently**: the
aggregate simply comes back smaller and the guardrail passes. There is no
runtime signal for that at all, so the only available defence is keeping the
list of modules that can make the mistake short enough to read.

These tests walk the import graph rather than grepping, so a rename, an alias or
a ``from app.db import pools`` spelling is caught the same way a direct import
is.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"

# Every module permitted to reach the privileged connection, with the reason.
# Adding a name here should feel like a decision, which is the entire point.
PERMITTED = {
    # The guardrail arithmetic and the action/audit spine. The reason the split
    # exists.
    "app.policy.store",
    # t_advit.policy_rules is platform data: the statutory layer is identical
    # for every workspace in an industry, and a ruleset that varied with the
    # asker would be a compliance gate a tenant could narrow.
    "app.policy.rules",
    # core.usage_events is billing metering. A tenant must not be able to
    # decide what it is charged for.
    "app.models.router",
    # The liveness probe and pool lifecycle only.
    "app.main",
    # Three writes, all to tables `authenticated` holds SELECT on and nothing
    # else (20260903000007): t_advit.runs, t_advit.decisions, and - through the
    # pipeline - t_advit.actions. That grant exists so an agent cannot rewrite
    # its own history, which means the agent recording its own history has to
    # be the one connection a tenant is not.
    "app.orchestrator.graph",
    # core.org_secrets holds each customer's own OpenRouter key, encrypted. It
    # has no tenant-facing SELECT on the ciphertext at all - core.org_secret_status
    # is what a settings page reads - so the only connection that can decrypt one
    # is the one a tenant is not.
    "app.secrets.store",
    # Reads t_advit.model_preferences to build a router for an organisation.
    # Not secret, but it runs where no principal is bound: the jobs process has
    # no session at all, and a model call there still has to know which tier the
    # customer chose.
    "app.models.for_org",
    # The unattended process. It holds the advit_jobs credential outright: it
    # has no HTTP surface and therefore no principal, and its workspace ids come
    # from a SELECT over our own tables rather than from any caller - which is
    # what makes "the scheduler cannot be used to bypass the per-request check"
    # a structural claim rather than a review comment.
    "app.jobs.runner",
    # t_advit.metrics_daily is the table the spend caps are checked against. A
    # tenant that could write it could raise its own ceiling, so `authenticated`
    # holds SELECT and nothing else - and the ingestion has to be the connection
    # a tenant is not.
    "app.ingest.metrics",
    # Two calls, both consequences the backend records of the tenant's own
    # write. t_advit.compute_blended_daily, revoked from `authenticated` by
    # 20260910000003 because it does not merely read - it WRITES the economics
    # the scaling verdict reads. And held.resolve on t_advit.held_proposals
    # after PUT .../cta: the row is the system's record of its own question,
    # `authenticated` holds SELECT on it and nothing else, so a member cannot
    # close a question by editing the record rather than by answering it.
    # Everything else in this module is the tenant's own write on the tenant
    # connection. (app.orchestrator.held itself takes a cursor and opens
    # nothing, which is why it is not on this list.)
    "app.routes_business",
    # Writes t_advit.outcomes.verdict - the system's score for its own
    # prediction. `authenticated` holds SELECT on outcomes and nothing else
    # (20260903000007) precisely so an agent cannot revise its own score after
    # the fact, which means the thing that writes the score has to be the
    # connection a tenant is not. It also reads across the whole of
    # blended_daily and metrics_daily for the workspace, and a measurement that
    # got smaller because the reader was less privileged would be worse than no
    # measurement.
    "app.learning.outcomes",
    # Writes t_advit.learnings. Same grant, same reason: a tenant that could
    # edit what the system has supposedly learned about them could edit what it
    # proposes to them next.
    "app.learning.promote",
    # Platform Watch. Writes t_advit.watched_sources and t_advit.watch_findings,
    # neither of which any tenant may see - which of the platform's own BLOCK
    # rules is running on an unverified reading is not a tenant's business.
    # It reads policy_rules and platform_knowledge on the same connection
    # because it runs from the jobs process, where no principal is bound.
    "app.watch.platform",
    # Writes the RATING and the compliance verdict onto a creative. Those are
    # the system's judgement, and the migration revoked tenant UPDATE on those
    # columns for exactly the reason a tenant must not set its own verdict.
    # Everything the tenant owns - the row, the name, the product - is written
    # on the tenant connection in the same module.
    "app.routes_creative",
    # Writes core.invoices from the jobs process, where no principal is bound.
    # An invoice is the platform's statement of what is owed; `authenticated`
    # holds SELECT (owners and admins, by policy) and nothing else, so a tenant
    # cannot issue, edit or void one.
    "app.billing.invoices",
    # Calls core.settle_subscriptions from the jobs process. The settle walk
    # is the platform's act on every subscription, not a caller's: it is
    # granted to advit_backend alone, and there is no principal to bind where
    # it runs.
    "app.billing.settle",
}

# The superuser DSN. Kept in Settings for `supabase db reset` and for test
# fixture setup; read by no runtime code path.
FORBIDDEN_SETTING = "database_url"


def _modules() -> list[tuple[str, Path, ast.Module]]:
    out = []
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(APP.parent).with_suffix("")
        name = ".".join(rel.parts)
        if name.endswith(".__init__"):
            name = name[: -len(".__init__")]
        out.append((name, path, ast.parse(path.read_text(encoding="utf-8"), str(path))))
    return out


def _names_imported_from_pools(tree: ast.Module) -> set[str]:
    """Every way a module can get its hands on the pools module."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.db.pools":
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("app.db.pools", "app.db"):
                    # A whole-module import reaches everything in it.
                    found.add("*")
    return found


PRIVILEGED = {"service_conn", "service_tx", "service_dsn", "*"}


def test_the_privileged_connection_is_reached_only_from_the_enumerated_modules():
    offenders = {
        name: sorted(_names_imported_from_pools(tree) & PRIVILEGED)
        for name, _, tree in _modules()
        if name not in PERMITTED
        and name != "app.db.pools"
        and _names_imported_from_pools(tree) & PRIVILEGED
    }
    assert offenders == {}, (
        "these modules reach the connection that is not subject to RLS, and are "
        "not on the list in this file:\n  "
        + "\n  ".join(f"{m}: {n}" for m, n in offenders.items())
        + "\n\nIf that is deliberate, add the module to PERMITTED with the reason "
        "it needs to see rows the caller cannot."
    )


def test_the_permitted_list_has_no_stale_entries():
    """The counterpart, so the list stays an inventory rather than a wish.

    A module that stops using the privileged connection and stays on this list
    makes the list longer than the truth, and a list nobody trusts is not a
    control.
    """
    actual = {
        name
        for name, _, tree in _modules()
        if _names_imported_from_pools(tree) & PRIVILEGED
    }
    stale = sorted(PERMITTED - actual)
    assert stale == [], f"these modules no longer reach the service connection: {stale}"


def test_no_runtime_module_connects_on_the_superuser_dsn():
    """``Settings.database_url`` is the vulnerability, kept only for tooling.

    Any runtime module that opens a connection on it is, by construction,
    running with RLS absent — which is the state this whole migration exists to
    leave. Two exemptions, both narrow:

      * ``app.config`` declares the setting.
      * ``app.db.pools`` reads it once, to REFUSE it: if
        ``SERVICE_DATABASE_URL`` is ever set to the same DSN — one environment
        variable copy-pasted into the wrong name — the process will not boot.
        Reading it in order to reject it is the opposite of the defect.
    """
    exempt = {"app.config", "app.db.pools"}
    offenders: list[str] = []
    for name, _, tree in _modules():
        if name in exempt:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == FORBIDDEN_SETTING:
                offenders.append(f"{name}:{node.lineno}")
    assert offenders == [], "these modules read the superuser DSN: " + ", ".join(offenders)


def test_the_pools_module_reads_the_superuser_dsn_only_to_refuse_it():
    """The exemption above, pinned.

    Without this, ``app.db.pools`` is simply on an allow-list, and the next
    person to need a connection there has a precedent rather than a rule.
    """
    source = (APP / "db" / "pools.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == FORBIDDEN_SETTING):
            continue
        enclosing = next(
            (
                fn
                for fn in functions
                if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno)
            ),
            None,
        )
        assert enclosing is not None, (
            f"pools.py reads the superuser DSN at module level (line {node.lineno})"
        )
        refuses = any(
            isinstance(n, ast.Raise)
            and isinstance(n.exc, ast.Call)
            and isinstance(n.exc.func, ast.Name)
            and n.exc.func.id == "PoolsNotOpen"
            for n in ast.walk(enclosing)
        )
        assert refuses, (
            f"pools.py reads the superuser DSN in {enclosing.name}() at line "
            f"{node.lineno}, and that function does not refuse it. The exemption "
            "this module holds is for rejecting the DSN, not for using it."
        )
