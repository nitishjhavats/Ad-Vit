"""No function may be executable by PUBLIC.

PostgreSQL grants EXECUTE on a new function to PUBLIC by default. Every
`grant execute ... to authenticated` line in these migrations therefore
described an intent the database did not enforce — the blanket grant sat
underneath, and `anon` inherited it.

That mattered more than an ordinary over-grant because of how the tenant guards
are written: core.assert_org_visible and core.log_audit treat a caller with no
JWT subject as the backend, on the reasoning that PostgREST sets a subject for a
signed-in user. An anonymous caller has no subject either, so it took the
trusted branch and read any organisation's entitlements.

This is a standing check rather than a one-off regression, because the default
is the trap: every future migration creates functions that are PUBLIC-executable
the moment they exist, and the only thing that stops it is remembering. Here,
forgetting fails the build.
"""

from __future__ import annotations

import pytest

from conftest import rows_as, SUPERADMIN

SCHEMAS = ("core", "t_advit")


def public_executable(conn):
    return rows_as(
        conn,
        SUPERADMIN,
        """
        select n.nspname || '.' || p.proname
                 || '(' || pg_get_function_identity_arguments(p.oid) || ')' as fn,
               p.prosecdef
          from pg_proc p
          join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = any(%s)
           and exists (
             select 1 from aclexplode(p.proacl) a
              where a.grantee = 0 and a.privilege_type = 'EXECUTE'
           )
         order by 1
        """,
        (list(SCHEMAS),),
    )


def test_no_function_is_executable_by_public(conn):
    offenders = public_executable(conn)
    assert offenders == [], (
        "these functions are callable by every role including anon:\n  "
        + "\n  ".join(f"{fn}{' [SECURITY DEFINER]' if sd else ''}" for fn, sd in offenders)
        + "\n\nAdd `revoke execute on function ... from public;` to the migration "
          "that creates them. A SECURITY DEFINER function reachable by anon runs "
          "with the owner's rights on behalf of someone who never signed in."
    )


@pytest.mark.parametrize(
    "fn,args",
    [
        ("t_advit.compute_blended_daily", "uuid, date"),
        ("core.rollup_usage_for_day", "date"),
    ],
)
def test_compute_jobs_are_not_granted_to_tenants(conn, fn, args):
    """These do not read, they WRITE — computed economics into any workspace's
    blended_daily, and billing usage platform-wide. A tenant holding
    compute_blended_daily can overwrite the very numbers the scaling verdict
    reads back."""
    granted = rows_as(
        conn, SUPERADMIN,
        """
        select a.grantee::regrole::text
          from pg_proc p
          join pg_namespace n on n.oid = p.pronamespace
          join lateral aclexplode(p.proacl) a on a.privilege_type = 'EXECUTE'
         where n.nspname || '.' || p.proname = %s
           and pg_get_function_identity_arguments(p.oid) = %s
        """,
        (fn, args),
    )
    names = {r[0] for r in granted}
    assert "authenticated" not in names, f"{fn} is a compute job, not a tenant query"
    assert "anon" not in names
