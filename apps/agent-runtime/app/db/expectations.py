"""What this build of the runtime expects the database to already have.

Production migrations are applied by a person, through the Supabase MCP's
``apply_migration``, and the containers are built by Coolify from ``master``
on a separate trigger. Nothing ties the two together. On 2026-09-16 the
production API was found running a build that called ``core.access_mode(uuid)``
against a database whose migration 20260911000015 had turned that overload
into a function that always raises - every workspace request failing, no
deploy having failed, nothing in a log naming the cause. The database was
ahead of the code that time; the next time it will be behind.

This module is the tie. One probe per migration the code depends on, newest
last, each a single ``select`` that is true when the migration's objects
exist. ``main.py`` runs them at boot and ``/health`` answers 503 while any is
false - so a container built from code the database cannot yet serve fails
its health check and Coolify keeps the previous one running, instead of the
new one going live and 500-ing until somebody notices.

The list is also a standing guard on the repository: the newest migration on
disk must be the newest entry here (``test_schema_expectations``), so adding a
migration means saying, in one line, what the code now needs from it.
"""

from __future__ import annotations

from typing import Any

# (migration file stem, probe). The probe runs on the SERVICE connection and
# must return exactly one row with one boolean column.
EXPECTATIONS: tuple[tuple[str, str], ...] = (
    (
        "20260911000015_entitlements_name_their_product",
        "select to_regprocedure('core.access_mode(uuid, uuid)') is not null",
    ),
    (
        "20260911000016_an_edge_is_a_row_about_two_nodes",
        "select to_regprocedure('t_advit.guard_kg_edge_tier()') is not null",
    ),
    (
        "20260912000001_a_learning_has_a_natural_key",
        "select exists (select 1 from pg_indexes "
        " where schemaname = 't_advit' and indexname = 'learnings_account_claim_unique')",
    ),
    (
        "20260914000001_platform_watch",
        "select to_regclass('t_advit.watch_findings') is not null",
    ),
    (
        "20260914000002_creative_studio",
        "select to_regtype('t_advit.creative_status') is not null",
    ),
    (
        "20260916000001_tiers_coupons_invoices",
        "select to_regprocedure('core.apply_coupon(uuid, text)') is not null",
    ),
    (
        "20260917000001_the_operator_console",
        "select to_regprocedure('core.set_organisation_status(uuid, core.org_status, text)') is not null",
    ),
    (
        "20260917000002_the_catalogue_is_platform_data",
        "select count(*) = 4 from core.plans "
        " where key in ('starter', 'growth', 'scale', 'agency') and is_active",
    ),
    (
        "20260917000003_the_packs_are_platform_data",
        "select exists (select 1 from t_advit.policy_rules where code = 'IN_RERA_ASSURED_RETURN')",
    ),
    (
        "20260918000001_a_held_proposal_is_a_row",
        "select exists (select 1 from pg_indexes "
        " where schemaname = 't_advit' and indexname = 'held_proposals_one_open_per_workspace')",
    ),
    (
        "20260918000002_money_moves_the_subscription",
        "select to_regprocedure('core.review_payment(uuid, core.payment_status, text)') is not null",
    ),
)


def missing(cur: Any) -> list[str]:
    """The migrations whose objects are absent, in order. Empty means the
    database is at least as new as this code.

    Each probe runs under its own savepoint: a probe that cannot even parse -
    ``to_regprocedure`` naming a type the migration has not created yet -
    raises, and without the savepoint that would abort the transaction and
    make every later probe report missing too.
    """
    out: list[str] = []
    for version, probe in EXPECTATIONS:
        cur.execute("savepoint expectation")
        try:
            cur.execute(probe)
            row = cur.fetchone()
            value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
            ok = bool(value)
            cur.execute("release savepoint expectation")
        except Exception:
            cur.execute("rollback to savepoint expectation")
            ok = False
        if not ok:
            out.append(version)
    return out
