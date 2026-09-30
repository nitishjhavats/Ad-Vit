# P1 — API authentication and per-request tenancy

_Synthesised from three independently designed approaches, each scored by three
judge lenses. Ranking: RLS pass-through 35.7 > signed errand 33.0 > proof-carrying
scope 31.3. Every load-bearing claim below was verified against the running
Postgres rather than taken from the designs._

I read the runtime, the migrations, and the web app, and verified the load-bearing claims against the running Postgres (17.6 on 54322) rather than trusting the three designs. Here is the single plan.

---

# ad-vit auth + tenancy: the implementation plan

## 0. Facts I verified live before designing (these settle the disagreements between the three designs)

| Claim | Result |
|---|---|
| A `NOINHERIT` login role that is a member of `authenticated` but owns nothing: forgetting `set role` | `permission denied for schema marketing` — **fail-closed and loud**, verified |
| `set_config('request.jwt.claims',…,true)` + `set local role authenticated` inside `conn.transaction()` | RLS scopes correctly; after the txn `current_user` reverts and claims are `''` — verified |
| RLS policy `TO <group role>` applies to an **INHERIT** member login role | verified (member saw only the policy-matching row) |
| `current_setting('role', true)` on a plain connection | `'none'` → `core.assert_org_visible` and `core.log_audit` take the **backend** branch correctly |
| An org `member` with **no** `workspace_members` row | passes `workspaces_select` = **True**, `marketing.is_workspace_member` = **False** — **the gate bug is real** |
| `core.log_audit('workspace',…, p_workspace => X)` with no `p_org`, from a tenant connection | `InsufficientPrivilege: a workspace-scoped audit row must name its organisation` (hint `audit_org_required`) — **verified break** |
| `psycopg_pool` 3.3.1 `_reset_connection` (`pool.py:810-819`) | checks `transaction_status` **after** the reset callback and **discards** a connection left `INTRANS` — the naive scrub hook destroys the pool |

**Corrections to the source designs** (do not carry these errors forward):
- `supabase/config.toml` `[db.pooler] enabled = false`. Supavisor is a *future* hazard for the advisory lock, not the present configuration.
- Only **two** POST routes carry `workspace_id` in the body: `/api/chat` and `/api/daily-truth`. `/api/economics`, `/api/cta/recommend`, `/api/compliance/check` carry none.
- `packages/saas-core-db/tests/` has **93** `def test_` across 8 files, not 124.
- `AGENT_RUNTIME_SECRET` has **zero** code references — it exists only in `.env.example`.
- **No scheduler code exists anywhere** in `apps/agent-runtime`. "The scheduler path still works" is a test of code this plan writes for the first time.
- `apps/superadmin/` is empty.
- `compute_blended_daily`, `log_audit`, `is_workspace_member`, `effective_autonomy`, `access_mode` are all `SECURITY DEFINER` — the backend needs `EXECUTE`, not table grants, for those paths.

---

## Step 0 — Close the port. Today. Before any code.

In Coolify: remove the agent-runtime's public domain/port mapping, put `agent-runtime` and `web` on one project-scoped Docker network, verify with `docker network inspect` that the HRMS containers cannot resolve `agent-runtime:8000`.

`F:/Marketing AI OS/apps/web/src/lib/api.ts` already carries `import "server-only"` and calls the runtime from the Next server, so nothing breaks. This removes the live exploit in an afternoon and converts every step below from firefighting into unhurried hardening. **It is also the reason no feature flag is needed** (§9).

---

## Step 1 — `F:/Marketing AI OS/supabase/migrations/20260911000001_runtime_roles.sql`

The runtime stops being `postgres`. Three roles, **none of them a member of Supabase's `service_role`.**

This is the one place I overrule the winning design. `grant service_role to advit_service` hands a new password-holding credential **cluster-wide BYPASSRLS**, and this cluster also hosts HRMS. A leaked runtime password would then be full read/write over an unrelated product's database. Instead the backend role is a plain, non-BYPASSRLS group role whose reach is `core` + `marketing` and nothing else — enforced by explicit grants and its own permissive policies, which I verified apply through INHERIT membership.

```sql
-- =============================================================================
-- The agent runtime stops being the superuser.
--
-- Today it connects as `postgres`: rolbypassrls, owner of every table,
-- relforcerowsecurity = false. On that connection RLS is not weakened, it is
-- ABSENT - every policy in these migrations is dead code for the API path.
--
-- Three roles, and the split is the schema's own stated intent rather than an
-- invention. 20260903000007 grants `authenticated` SELECT-only on runs,
-- decisions, actions, outcomes and guardrail_events, UPDATE on approvals alone,
-- and nothing at all on marketing.secrets: "written by the agent runtime under
-- the service role, so an agent cannot rewrite its own history".
--
-- Deliberately NOT `grant service_role to ...`. Supabase's service_role carries
-- BYPASSRLS, which is cluster-wide. This database is shared with an unrelated
-- HRMS product, so a leaked runtime password must not be able to read it.
-- advit_backend is not BYPASSRLS; its reach is exactly the grants and policies
-- written below, and a marketing table added later without a policy fails the
-- service path LOUDLY rather than silently widening it.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. advit_backend: the governance spine's privilege set. NOLOGIN - it is worn,
--    never connected as, so the credential and the privilege rotate separately.
-- ---------------------------------------------------------------------------
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'advit_backend') then
    create role advit_backend nologin;
  end if;
end $$;

grant usage on schema core, marketing to advit_backend;

do $grants$
declare r record;
begin
  for r in
    select schemaname as s, tablename as t
      from pg_tables
     where schemaname in ('core','marketing')
       -- core.audit_log is excluded on purpose. Its ONLY writer is
       -- core.log_audit, which is SECURITY DEFINER and therefore needs no table
       -- grant. A direct grant here would let a bug write an unattributed row
       -- into the append-only trail, which is the exact property
       -- 20260907000001 exists to protect.
       and not (schemaname = 'core' and tablename = 'audit_log')
  loop
    -- No DELETE, anywhere. The runtime corrects rows; it never removes tenant
    -- history. A missing grant is a louder failure than a missing WHERE.
    execute format('grant select, insert, update on %I.%I to advit_backend', r.s, r.t);

    -- RLS is enabled on all 42 tables in these schemas and every existing
    -- policy is `to authenticated`, so without a policy of its own the backend
    -- role sees zero rows and every write is refused. Verified: an RLS policy
    -- naming a group role applies to a login role that INHERITs it.
    execute format(
      'create policy advit_backend_all on %I.%I for all to advit_backend '
      'using (true) with check (true)', r.s, r.t);
  end loop;
end
$grants$;

do $seqs$
declare r record;
begin
  for r in select sequence_schema s, sequence_name n
             from information_schema.sequences
            where sequence_schema in ('core','marketing')
  loop
    execute format('grant usage, select on sequence %I.%I to advit_backend', r.s, r.n);
  end loop;
end
$seqs$;

-- 20260910000003 revoked EXECUTE from PUBLIC on every function in these
-- schemas, so the backend needs its own grants. This is what lets it call
-- core.log_audit with actor_type 'agent'/'automation' and
-- marketing.compute_blended_daily, both revoked from `authenticated`.
do $fns$
declare r record;
begin
  for r in select n.nspname s, p.proname f,
                  pg_get_function_identity_arguments(p.oid) a
             from pg_proc p join pg_namespace n on n.oid = p.pronamespace
            where n.nspname in ('core','marketing')
  loop
    execute format('grant execute on function %I.%I(%s) to advit_backend', r.s, r.f, r.a);
  end loop;
end
$fns$;

-- ---------------------------------------------------------------------------
-- 2. advit_tenant: owns nothing, reads nothing. Its ONLY power is the ability
--    to become `authenticated`.
--
--    NOINHERIT is the whole trick and it is verified, not assumed: on a
--    connection that forgets `set local role authenticated`, the first
--    statement raises `permission denied for schema marketing`. Fail-closed by
--    construction rather than by discipline. With INHERIT the same mistake
--    would silently run with `authenticated`'s grants and no claims - which
--    also fails closed, but only by accident, and would carry HRMS's grants to
--    `authenticated` along with ours.
-- ---------------------------------------------------------------------------
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'advit_tenant') then
    create role advit_tenant nologin noinherit;
  end if;
end $$;
grant authenticated to advit_tenant;

-- ---------------------------------------------------------------------------
-- 3. advit_service / advit_jobs: INHERIT members of advit_backend. No `set
--    role` needed, and that is deliberate - `current_setting('role')` stays
--    'none' on these connections, which is precisely the shape
--    core.assert_org_visible (20260910000003) recognises as the backend, and
--    auth.uid() is null so core.log_audit takes its trusted branch and honours
--    p_actor_type => 'agent' / 'automation'.
--
--    Two credentials, not one, so the API and the unattended job process are
--    separately revocable and separately visible in pg_stat_activity.usename.
-- ---------------------------------------------------------------------------
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'advit_service') then
    create role advit_service nologin inherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'advit_jobs') then
    create role advit_jobs nologin inherit;
  end if;
end $$;
grant advit_backend to advit_service, advit_jobs;

-- Connection budget, computed rather than guessed. Per API replica:
--   tenant pool max 10, service pool max 5, plus PostgresLockManager's
--   un-pooled direct connection (semaphore-capped at 4 concurrent mutations).
-- Two replicas, doubling briefly during a rolling deploy:
--   advit_tenant   2 x 10 x 2 = 40
--   advit_service  2 x (5+4) x 2 = 36
--   advit_jobs     1 x (3+2) = 5
-- The winner design's `connection limit 5` on the service role was consumed by
-- its own pool, leaving nothing for the lock connections, and would have
-- surfaced as a failed mutation lock rather than a clean 503.
alter role advit_tenant  connection limit 40;
alter role advit_service connection limit 36;
alter role advit_jobs    connection limit 10;

comment on role advit_tenant is
  'Agent-runtime tenant connection. Owns nothing, is not BYPASSRLS, and can do '
  'nothing until it explicitly becomes `authenticated`. RLS is the tenancy '
  'boundary on this path - the same policies packages/saas-core-db/tests '
  'already exercise through conftest.acting_as.';
comment on role advit_backend is
  'Governance spine: the action/audit writes, marketing.secrets, and the '
  'guardrail arithmetic. NOT a member of service_role - reach is core + '
  'marketing only, so a leaked credential cannot read the HRMS product on this '
  'shared cluster.';
```

**LOGIN and passwords live outside the migration** — a forward-only migration must never carry a secret.
- `F:/Marketing AI OS/supabase/seeds/04_runtime_roles_local.sql`: `alter role advit_tenant login password 'advit_tenant_local'; …` (seeds run only on `supabase db reset`).
- Production: a Coolify pre-deploy step runs the same `alter role … login password` from a generated secret.

Check `show max_connections` on the VPS before shipping: 86 reserved connections is a real bite out of a budget shared with HRMS.

`F:/Marketing AI OS/apps/agent-runtime/app/config.py` gains:

```python
    # database_url stays ONLY for `supabase db reset` tooling and the DB test
    # suite. No runtime code path may read it after Step 3; the standing test
    # test_no_runtime_module_reads_the_superuser_dsn enforces that.
    tenant_database_url: str = ""
    service_database_url: str = ""

    supabase_url: str = "http://127.0.0.1:54321"
    supabase_jwt_issuer: str = ""            # defaults to f"{supabase_url}/auth/v1"
    supabase_jwt_secret: str = ""            # HS256, today's self-hosted shape
    supabase_jwt_secret_previous: str = ""   # rotation window
    supabase_jwks_url: str = ""              # set when auth.signing_keys_path is enabled
```

---

## Step 2 — `F:/Marketing AI OS/apps/agent-runtime/app/db/pools.py`

```python
"""Two pools, two login roles, two passwords.

    advit_tenant  -> becomes `authenticated` with the caller's JWT claims. The
                     RLS policies in supabase/migrations decide what is
                     visible. Byte-identical in shape to
                     packages/saas-core-db/tests/conftest.py::acting_as, so the
                     93 passing RLS tests become the API's authorization tests.

    advit_service -> the governance spine: marketing.actions, guardrail_events,
                     outcomes, the approvals INSERT, marketing.secrets,
                     compute_blended_daily, and core.log_audit with actor_type
                     'agent'. Every one of those is SELECT-only or ungranted for
                     `authenticated` in the existing schema.

The one hard rule this module exists to encode:

    A read that decides what the CALLER MAY SEE runs on the tenant connection.
    A read or write that decides what the SYSTEM MAY DO runs on the service
    connection, with a workspace id an AuthorizedWorkspace has already proved.

The second half is not a hedge. PostgresPolicyStore.workspace_policy computes
committed_daily_inr as coalesce(sum(...), 0) over marketing.ad_sets and
marketing.actions. Under RLS a row the caller cannot see is not an error - it is
ABSENT, and absent sums to zero. marketing.workspaces.spend_basis_known exists
because coalesce cannot tell "spent nothing" from "never ingested"; RLS adds a
third indistinguishable case, "not yours to see". A safety computation must not
vary with who is asking.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, ContextManager, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

_tenant_pool: ConnectionPool | None = None
_service_pool: ConnectionPool | None = None


def _scrub(conn: psycopg.Connection) -> None:
    """Pool reset hook, run on every putconn.

    psycopg_pool 3.3.1 inspects transaction_status AFTER this callback and
    DISCARDS any connection left INTRANS (pool.py:810-819). With autocommit off
    - which tenant_tx asserts - these three statements would open an implicit
    transaction nobody commits, every checkin would destroy the connection, and
    the pool would silently become a connection factory. Hence the flip.

    NOT `discard all`: psycopg3 prepares statements above prepare_threshold and
    caches their names per connection, so deallocating them behind its back
    makes the next checkout fail with `prepared statement "_pg3_0" does not
    exist`. Only the three things this module ever sets are cleared.
    """
    previous = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("reset role")
            cur.execute("select set_config('request.jwt.claims', '', false)")
            cur.execute("select set_config('request.impersonation_session_id', '', false)")
    finally:
        conn.autocommit = previous


def open_pools() -> None:
    """Called from the lifespan handler so a wrong credential fails at BOOT
    rather than on the first request that happens to need it."""
    global _tenant_pool, _service_pool
    s = get_settings()
    if not s.tenant_database_url or not s.service_database_url:
        raise RuntimeError(
            "TENANT_DATABASE_URL and SERVICE_DATABASE_URL are required. There is "
            "no fallback to DATABASE_URL: the superuser DSN is the vulnerability."
        )
    _tenant_pool = ConnectionPool(
        s.tenant_database_url, min_size=2, max_size=10,
        kwargs={"row_factory": dict_row}, reset=_scrub, open=True, timeout=5,
    )
    _service_pool = ConnectionPool(
        s.service_database_url, min_size=1, max_size=5,
        kwargs={"row_factory": dict_row}, reset=_scrub, open=True, timeout=5,
    )
    _tenant_pool.wait(timeout=10)
    _service_pool.wait(timeout=10)


@contextmanager
def tenant_tx(claims: dict[str, Any], impersonation_id: str | None = None) -> Iterator[psycopg.Cursor]:
    """One SHORT transaction acting as the caller.

    `set local`, never `set`: role and claims unwind with the transaction, so a
    connection returning to the pool cannot carry one caller's identity into the
    next caller's request. Verified: after the transaction, current_user is back
    to advit_tenant and request.jwt.claims is ''.

    The autocommit assertion is not decoration. Under autocommit each statement
    is its own transaction, SET LOCAL evaporates with a warning nobody reads,
    and the query then runs as the bare NOINHERIT login role - which raises
    `permission denied for schema marketing`, i.e. fails closed, but
    confusingly. PostgresLockManager already sets autocommit=True on its own
    connection, so this is a mistake that exists in this codebase today and
    would spread.

    Deliberately short. /api/chat runs model calls for seconds; holding an open
    transaction across one would exhaust a pool of ten under trivial concurrency
    and pin an idle-in-transaction snapshot. The PRINCIPAL is request-scoped;
    the TRANSACTION is query-scoped. Two extra statements per transaction
    (~0.1-0.3ms on loopback) against today's code, which opens a fresh TCP
    connection with a full auth handshake for every single query.
    """
    assert _tenant_pool is not None, "open_pools() has not run"
    with _tenant_pool.connection() as conn:
        assert not conn.autocommit, "tenant work must run in an explicit transaction"
        with conn.transaction():
            with conn.cursor() as cur:
                # Belt: prove the previous checkout left nothing behind. This
                # turns a `set` where `set local` was meant from "unlikely" into
                # "impossible and testable".
                cur.execute("select current_setting('request.jwt.claims', true) as c")
                if cur.fetchone()["c"]:
                    raise RuntimeError("pooled connection carried claims across a checkout")

                cur.execute(
                    "select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),)
                )
                if impersonation_id is not None:
                    cur.execute(
                        "select set_config('request.impersonation_session_id', %s, true)",
                        (impersonation_id,),
                    )
                cur.execute("set local role authenticated")
                yield cur


@contextmanager
def service_tx(impersonation_id: str | None = None) -> Iterator[psycopg.Cursor]:
    """The privileged connection.

    No `set role` here, deliberately. advit_service INHERITs advit_backend, so
    current_setting('role') stays 'none' - which is exactly the shape
    core.assert_org_visible recognises as the backend (a role of 'anon' or
    'authenticated' with no subject is refused), and auth.uid() is null so
    core.log_audit takes its trusted branch and will honour actor_type 'agent'.

    Import this ONLY from app/policy/store.py, app/policy/rules.py, app/db/
    secrets.py and app/jobs/. tests/test_service_connection_surface.py walks the
    import graph to enforce that list, because the design rests on this being a
    small enumerable set of call sites rather than a habit.
    """
    assert _service_pool is not None, "open_pools() has not run"
    with _service_pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                if impersonation_id is not None:
                    # So an action taken inside a support session is stamped on
                    # the privileged path too - it is the path that moves money,
                    # and it would otherwise be the ONE unstamped record.
                    cur.execute(
                        "select set_config('request.impersonation_session_id', %s, true)",
                        (impersonation_id,),
                    )
                yield cur


# ---------------------------------------------------------------------------
# Request-scoped tenant transaction for code we do not want to re-thread.
#
# app/orchestrator/graph.py's Orchestrator is constructed once and its LangGraph
# is compiled once, but the principal changes per request. A ContextVar is the
# standard way to carry request scope through such code, and Starlette copies
# the context into the threadpool that runs a sync `def` route, so it survives.
# It is unset by default and reading it raises - so a graph node that runs
# outside a bound request fails closed rather than falling back to a privileged
# connection.
# ---------------------------------------------------------------------------
_CURRENT_TENANT_TX: ContextVar[Callable[[], ContextManager[psycopg.Cursor]] | None] = ContextVar(
    "current_tenant_tx", default=None
)


@contextmanager
def bind_tenant(tx_factory: Callable[[], ContextManager[psycopg.Cursor]]) -> Iterator[None]:
    token = _CURRENT_TENANT_TX.set(tx_factory)
    try:
        yield
    finally:
        _CURRENT_TENANT_TX.reset(token)


def current_tenant_tx() -> ContextManager[psycopg.Cursor]:
    factory = _CURRENT_TENANT_TX.get()
    if factory is None:
        raise RuntimeError(
            "no tenant transaction is bound for this request. A read that decides "
            "what the caller may see must run under the caller's claims; it must "
            "not silently fall back to the service connection."
        )
    return factory()
```

`PostgresLockManager` keeps its **own direct un-pooled connection** to 5432 and gets a `threading.Semaphore(4)`. Reason, stated in the file: it takes a *session* advisory lock (`pg_try_advisory_lock`) and holds it across `audit.pre` (which commits), the driver call and `audit.post` — deliberately spanning transactions. `SET LOCAL` survives a transaction pooler; a session advisory lock does not. `[db.pooler] enabled = false` today, so this is a landmine *introducing* Supavisor would create, not a present bug — and `test_the_mutation_lock_connection_does_not_use_the_pooled_port` is what stops someone stepping on it later.

---

## Step 3 — Get off `postgres`, still no auth (one deployable PR)

Replace every `psycopg.connect(settings.database_url)` with `tenant_tx()` / `service_tx()`:

| File | Call site | Goes to |
|---|---|---|
| `app/main.py` `/health` | liveness probe | `service_tx()` |
| `app/main.py` `connections_health`, `list_approvals`, `dashboard`, `rollback_action`'s action lookup | reads that decide what the caller sees | `tenant_tx()` |
| `app/main.py` `respond_to_approval` UPDATE + audit | `approvals_respond` is exactly this check | `tenant_tx()` |
| `app/routes_business.py` `submit_daily_truth` trailing read + INSERT + audit | RLS-governed tenant write | `tenant_tx()` |
| `app/routes_business.py` `compute_blended_daily` call | revoked from `authenticated` in 20260910000003 | `service_tx()`, **after** the tenant insert commits |
| `app/policy/store.py` all four classes | governance spine + cap arithmetic | `service_tx()` |
| `app/policy/rules.py` `PolicyRuleLoader` | global rule data, 300s cache | `service_tx()` |
| `app/orchestrator/graph.py` `Orchestrator._conn` | tenant reads | `current_tenant_tx()` |

**Two breaks this PR must fix in the same commit, both verified:**

1. `core.log_audit` from a tenant connection with `p_workspace` and no `p_org` raises `audit_org_required`. Both `main.respond_to_approval` (line ~283) and `routes_business.submit_daily_truth` (line ~157) omit `p_org`. Add `p_org => %s` from the resolved `ws.org_id`. Without this, **every approval response and every daily-truth submission 500s.**

2. `main.respond_to_approval` writes `responded_by = payload.responded_by` — a caller-supplied human signature on the row that discharges an approval, the thing pipeline step 6 redeems. Delete the field from `ApprovalResponse` and write `responded_by = auth.uid()` in SQL, which the tenant connection has.

Also in this PR: `/health` keeps its shape (public liveness + product identity) but `meta_driver`, `write_allowlist` and `database.error` move to an authenticated `GET /api/health/detail`. That breaks `tests/test_api.py::test_health_reports_the_driver_and_the_write_allowlist` and the `Health` type in `apps/web/src/lib/api.ts` — **update both in this same commit**; the winner design proposed trimming `/health` without noticing either.

Deploy and watch `pg_stat_activity` for privilege errors before adding anything else. Behaviour is otherwise identical.

---

## Step 4 — Token verification: `F:/Marketing AI OS/apps/agent-runtime/app/auth/tokens.py`

Add to `F:/Marketing AI OS/apps/agent-runtime/requirements.txt`:
```
pyjwt[crypto]==2.10.1
cachetools==5.5.0
```
Not python-jose (unmaintained; CVE-2024-33663 algorithm confusion). Not Authlib (a much larger surface for one function). PyJWT ships `PyJWKClient` with keyed caching and **forces an explicit `algorithms=[...]` list.**

```python
"""Local verification of the Supabase JWT. GoTrue is never called per request.

A per-request call to /auth/v1/user ties this API's availability to the auth
service, adds a network hop to the hot path, and is rate-limited.

The algorithm list is derived from OUR configuration, never from the token
header. That is the mitigation for the classic algorithm-confusion forgery -
sign an HS256 token using the published RSA/EC public key as the HMAC secret. A
verifier that reads `alg` from the header to pick the key type accepts it. This
one cannot represent that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

from app.config import get_settings


class TokenRejected(Exception):
    """The reason goes to the log; the caller gets a flat message. Which claim
    failed is exactly the feedback a forger needs to tune the next attempt."""


class KeyServerUnavailable(Exception):
    """Our key source is down. 503, never 401. Telling a user their credentials
    are bad when OUR key server is down sends them to reset a password that was
    never wrong."""


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    subject: uuid.UUID
    claims: dict


_jwks_client: PyJWKClient | None = None


def _jwks(url: str) -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        # lifespan IS the rotation window: a new signing key is picked up within
        # five minutes with no restart, and the last good key set stays in
        # memory so a brief GoTrue outage does not take this API down.
        _jwks_client = PyJWKClient(url, cache_keys=True, lifespan=300, timeout=3)
    return _jwks_client


def verify(token: str) -> VerifiedToken:
    s = get_settings()
    issuer = s.supabase_jwt_issuer or f"{s.supabase_url.rstrip('/')}/auth/v1"

    if s.supabase_jwks_url:
        # Target state: supabase/config.toml `auth.signing_keys_path` enabled,
        # GoTrue publishing ES256/RS256 at /auth/v1/.well-known/jwks.json.
        algorithms = ["ES256", "RS256", "EdDSA"]
        try:
            keys = [_jwks(s.supabase_jwks_url).get_signing_key_from_jwt(token).key]
        except jwt.exceptions.PyJWKClientConnectionError as exc:
            raise KeyServerUnavailable(str(exc)) from exc
        except jwt.exceptions.PyJWKClientError as exc:
            raise TokenRejected(f"unknown kid: {exc}") from exc
    elif s.supabase_jwt_secret:
        # Today's shape: config.toml has signing_keys_path commented out, so
        # this deployment is HS256. supabase_jwt_secret_previous gives an HMAC
        # rotation a window in which both are tried.
        algorithms = ["HS256"]
        keys = [k for k in (s.supabase_jwt_secret, s.supabase_jwt_secret_previous) if k]
    else:
        # A service that cannot verify a principal has no way to refuse one,
        # which is today's bug. Refuse to start rather than to authenticate.
        raise RuntimeError("neither SUPABASE_JWKS_URL nor SUPABASE_JWT_SECRET is configured")

    last: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=algorithms,          # literal from config, never header-derived
                audience="authenticated",
                issuer=issuer,
                # Thirty seconds, not five minutes. Skew on a single
                # VPS is sub-second, and a generous leeway extends the life of
                # every revoked token by exactly that amount.
                leeway=30,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
            break
        except jwt.ExpiredSignatureError as exc:
            raise TokenRejected("expired") from exc
        except jwt.InvalidTokenError as exc:
            last = exc
    else:
        raise TokenRejected(f"signature or claim rejected: {last}") from last

    # The Supabase service-role key is a VALID JWT with role: service_role, no
    # sub, and a ten-year expiry. If one were pasted into a client and we
    # forwarded its claims, auth.uid() would be null and core.assert_org_visible
    # would take its BACKEND branch - re-opening from the HTTP side the exact
    # hole 20260910000003 closed in SQL.
    if claims.get("role") != "authenticated":
        raise TokenRejected(f"role {claims.get('role')!r} is not a tenant principal")

    # enable_anonymous_sign_ins = false in config.toml today, but a config flip
    # must not silently mint principals.
    if claims.get("is_anonymous"):
        raise TokenRejected("anonymous session")

    try:
        subject = uuid.UUID(claims["sub"])
    except (KeyError, ValueError, TypeError) as exc:
        # It is going into request.jwt.claims and then into auth.uid()::uuid.
        raise TokenRejected("sub is not a uuid") from exc

    return VerifiedToken(subject=subject, claims=claims)
```

**Failure map:** missing/malformed header → 401 · unknown `kid` after one bounded refresh → 401 · `alg: none` / HS256 against the published public key / wrong key → 401 · expired / wrong `iss` / wrong `aud` → 401 · service-role or anonymous → 401 · **JWKS unreachable with no cached key set → 503.**

**Revocation, honestly:** local verification cannot see a sign-out. A token stays good until `exp`. Two partial answers: cut `auth.jwt_expiry` in `supabase/config.toml` from 3600 to 900, and fold a `core.platform_users.is_active` read into the workspace resolver (below) so a *deactivated* account is dead on the next request. Sign-out itself is not covered. That is a JWT, not a session, and I would rather say so.

---

## Step 5 — Where the tenant check lives: `F:/Marketing AI OS/apps/agent-runtime/app/auth/scope.py`

**`workspace_id` stops being a query parameter, a body field, and a `str`.** Every workspace-scoped route moves under a path prefix and takes an `AuthorizedWorkspace`:

| Today | After |
|---|---|
| `GET /api/dashboard?workspace_id=` | `GET /api/workspaces/{workspace_id}/dashboard` |
| `GET /api/approvals?workspace_id=` | `GET /api/workspaces/{workspace_id}/approvals` |
| `GET /api/connections/health?workspace_id=` | `GET /api/workspaces/{workspace_id}/connections/health` |
| `POST /api/chat` (body field) | `POST /api/workspaces/{workspace_id}/chat` |
| `POST /api/daily-truth` (body field) | `POST /api/workspaces/{workspace_id}/daily-truth` |

This is the fix for the defect all three judges found: the winner's `authorized_workspace` read `request.state.body_workspace_id`, which **nothing populated**, from a **sync** dependency that cannot `await request.json()`. Making the id a path parameter removes the problem rather than plumbing around it. `ChatRequest.workspace_id` and `DailyTruthRequest.workspace_id` are **deleted** — a field that exists will eventually be read.

```python
"""A route may not take `workspace_id: str`. It takes an AuthorizedWorkspace.

Exactly five functions can produce one, and every one of them proves ownership
with a SELECT on the TENANT connection - so the answer comes from the RLS
policies in supabase/migrations, not from a Python `if`:

  * authorized_workspace()   - the path parameter, for /api/workspaces/{id}/...
  * authorized_action()      - POST /api/actions/{action_id}/rollback
  * authorized_approval()    - POST /api/approvals/{approval_id}/respond
  * authorized_ad_account()  - GET  /api/audit/account/{ad_account_id}
  * system_workspace()       - app/jobs/ only; ids come from a SELECT over our
                               own tables and never from a request.

The last four exist because RLS gives row visibility and nothing else: it cannot
resolve "which tenant owns action 7f3a?" for you. Every entity-keyed route needs
a written line of ownership resolution, and route_audit.py fails the boot if a
new one appears without it. The winner design enumerated the ad_account route
and MISSED the rollback route, which is the one that mutates Meta - a
cross-tenant rollback by UUID.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Path, Request

from app.auth.tokens import KeyServerUnavailable, TokenRejected, verify
from app.db.pools import tenant_tx


class Capability(str, Enum):
    READ_WORKSPACE = "read_workspace"
    SUBMIT_BUSINESS_TRUTH = "submit_business_truth"
    RESPOND_TO_APPROVAL = "respond_to_approval"
    ROLLBACK_ACTION = "rollback_action"
    RUN_UNATTENDED_JOB = "run_unattended_job"


_HUMAN = frozenset(Capability) - {Capability.RUN_UNATTENDED_JOB}
# A support session must not be able to satisfy an approval. An approval is the
# OWNER's signature, and support staff do not hold it. Without this rule,
# impersonation is a way for an operator to authorise spending in a tenant's
# name, and no amount of stamping makes that acceptable.
_IMPERSONATED = _HUMAN - {Capability.RESPOND_TO_APPROVAL}
# A scheduled job is strictly LESS powerful than any human, never more. A job
# that could answer its own proposal would make the approval gate decorative.
_SYSTEM = frozenset({Capability.READ_WORKSPACE, Capability.RUN_UNATTENDED_JOB})

# Module-private. A third construction site can still reach it as
# app.auth.scope._MINT - this is not a seal, it is the difference between an
# oversight and a deliberate, greppable act.
_MINT = object()


@dataclass(frozen=True, slots=True)
class Principal:
    subject: uuid.UUID | None        # whose RLS view this is
    actor: uuid.UUID | None          # who is answerable; differs only when impersonating
    claims: dict[str, Any]
    capabilities: frozenset[Capability]
    impersonation_session_id: str | None = None
    impersonation_org_id: str | None = None

    def tx(self):
        return tenant_tx(self.claims, self.impersonation_session_id)

    def require(self, cap: Capability) -> None:
        if cap not in self.capabilities:
            raise HTTPException(403, f"{cap.value} is not permitted for this principal")


@dataclass(frozen=True, slots=True)
class AuthorizedWorkspace:
    _mint: Any
    id: str
    org_id: str
    principal: Principal

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise RuntimeError(
                "AuthorizedWorkspace may only be built by app.auth.scope. A third "
                "construction site is a third place the membership proof is skipped."
            )


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "a bearer token is required")
    return token


def current_principal(
    request: Request,
    token: Annotated[str, Depends(_bearer)],
    impersonation: Annotated[str | None, Header(alias="X-Impersonation-Session")] = None,
) -> Principal:
    try:
        verified = verify(token)
    except KeyServerUnavailable as exc:
        request.state.auth_error = f"key server: {exc}"
        raise HTTPException(503, "authentication is temporarily unavailable") from exc
    except TokenRejected as exc:
        request.state.auth_error = str(exc)
        raise HTTPException(401, "invalid or expired token") from exc

    # Only role and sub travel onto the connection, in exactly the shape
    # packages/saas-core-db/tests/conftest.py::acting_as uses. Forwarding the
    # whole claim set would put attacker-influenced app_metadata inside
    # request.jwt.claims where a future policy might read it.
    claims = {"role": "authenticated", "sub": str(verified.subject)}
    principal = Principal(
        subject=verified.subject, actor=verified.subject,
        claims=claims, capabilities=_HUMAN,
    )
    if impersonation is None:
        return principal
    return _resolve_impersonation(principal, impersonation)


# --- the five producers ----------------------------------------------------

_RESOLVE_WORKSPACE = """
select w.org_id::text                              as org_id,
       marketing.is_workspace_member(w.id)          as is_member,
       (select u.is_active
          from core.platform_users u
         where u.id = auth.uid())                   as actor_active
  from marketing.workspaces w
 where w.id = %(workspace)s::uuid
"""


def authorized_workspace(
    workspace_id: Annotated[uuid.UUID, Path()],
    principal: Annotated[Principal, Depends(current_principal)],
) -> AuthorizedWorkspace:
    with principal.tx() as cur:
        cur.execute(_RESOLVE_WORKSPACE, {"workspace": str(workspace_id)})
        row = cur.fetchone()

    # THE PREDICATE IS is_workspace_member, NOT "the SELECT returned a row".
    #
    # This is the single most important line in the file, and it is verified
    # rather than reasoned: workspaces_select (20260903000005:288) uses the
    # WIDER core.is_org_member, while ad_sets, actions, approvals and every
    # other marketing policy use marketing.is_workspace_member. An org 'member'
    # with no workspace_members row passes the SELECT (True) and fails
    # is_workspace_member (False) - I confirmed both against the running
    # database. Treating the returned row as proof would let any member of an
    # organisation drive the tool pipeline against a sibling workspace they
    # cannot read, because everything downstream runs on the service connection
    # with the id "already proved".
    if row is None or not row["is_member"]:
        # 404, never 403. A 403 confirms the workspace exists, which rebuilds
        # the enumeration oracle 20260910000001 was written to close.
        raise HTTPException(404, "workspace not found")

    # Deactivation takes effect on the next request rather than at token expiry.
    # Folded into this query rather than its own round trip.
    if not row["actor_active"]:
        raise HTTPException(403, "this account is not active")

    # A support session names ONE organisation. Without this, swapping `sub` to
    # the target user would give the operator everything that user can see
    # across EVERY organisation they belong to, and
    # core.impersonation_sessions.org_id would be advisory.
    if principal.impersonation_org_id and principal.impersonation_org_id != row["org_id"]:
        raise HTTPException(404, "workspace not found")

    return AuthorizedWorkspace(
        _mint=_MINT, id=str(workspace_id), org_id=row["org_id"], principal=principal
    )


def _resolve_owned(principal: Principal, sql: str, key: str, ident: str) -> AuthorizedWorkspace:
    """Shared body of the three entity-keyed resolvers.

    Each SELECT runs on the TENANT connection, so actions_select /
    approvals_select / meta_connections_select - all of which use
    marketing.is_workspace_member - decide whether the row exists at all. Zero
    rows is a 404 for the same reason as above.
    """
    with principal.tx() as cur:
        cur.execute(sql, {key: ident})
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"{key.replace('_', ' ')} not found")
    if principal.impersonation_org_id and principal.impersonation_org_id != row["org_id"]:
        raise HTTPException(404, f"{key.replace('_', ' ')} not found")
    return AuthorizedWorkspace(
        _mint=_MINT, id=row["workspace_id"], org_id=row["org_id"], principal=principal
    )


def authorized_action(
    action_id: Annotated[uuid.UUID, Path()],
    principal: Annotated[Principal, Depends(current_principal)],
) -> AuthorizedWorkspace:
    """POST /api/actions/{action_id}/rollback.

    Today this route reads marketing.actions on the privileged connection and
    hands the workspace straight to ToolPipeline.invoke, which mutates Meta - so
    any signed-in user who learns an action UUID can roll back another tenant's
    action. It is the highest-consequence route in the product and the one both
    the winner design and its runner-up omitted.
    """
    principal.require(Capability.ROLLBACK_ACTION)
    return _resolve_owned(
        principal,
        """select a.workspace_id::text as workspace_id, w.org_id::text as org_id
             from marketing.actions a
             join marketing.workspaces w on w.id = a.workspace_id
            where a.id = %(action_id)s::uuid""",
        "action_id", str(action_id),
    )


def authorized_approval(
    approval_id: Annotated[uuid.UUID, Path()],
    principal: Annotated[Principal, Depends(current_principal)],
) -> AuthorizedWorkspace:
    principal.require(Capability.RESPOND_TO_APPROVAL)
    return _resolve_owned(
        principal,
        """select a.workspace_id::text as workspace_id, w.org_id::text as org_id
             from marketing.approvals a
             join marketing.workspaces w on w.id = a.workspace_id
            where a.id = %(approval_id)s::uuid""",
        "approval_id", str(approval_id),
    )


def authorized_ad_account(
    ad_account_id: Annotated[str, Path()],
    principal: Annotated[Principal, Depends(current_principal)],
) -> AuthorizedWorkspace:
    """GET /api/audit/account/{ad_account_id} takes an ad account with no
    workspace at all and calls the Meta driver directly. meta_connections_select
    is what decides ownership; this is the line that asks it."""
    return _resolve_owned(
        principal,
        """select c.workspace_id::text as workspace_id, w.org_id::text as org_id
             from marketing.meta_connections c
             join marketing.workspaces w on w.id = c.workspace_id
            where c.ad_account_id = %(ad_account_id)s""",
        "ad_account_id", ad_account_id,
    )


def system_workspace(workspace_id: str, org_id: str) -> AuthorizedWorkspace:
    """app/jobs/ only. The proof it offers is "this id came from a SELECT on our
    own table", not "a caller named it"."""
    return AuthorizedWorkspace(
        _mint=_MINT, id=workspace_id, org_id=org_id,
        principal=Principal(subject=None, actor=None, claims={}, capabilities=_SYSTEM),
    )
```

`PostgresPolicyStore.workspace_policy(ws: AuthorizedWorkspace)`, `PostgresAuditSink.pre(*, ws: AuthorizedWorkspace, …)` and `ToolRequest(workspace=ws)` are re-typed. **The signature is the enforcement**: a privileged read of a caller-supplied string is no longer a representable mistake.

### The two structural backstops

**`F:/Marketing AI OS/apps/agent-runtime/app/auth/middleware.py`** — default-deny at the edge, so a route added next month is closed the moment it exists:

```python
class PrincipalMiddleware(BaseHTTPMiddleware):
    """A dependency protects the routes that ask for it. This protects the ones
    that forget. Anything outside PUBLIC_PATHS is refused before routing, so
    opening an endpoint means editing a frozenset a reviewer will notice.

    Verifies once and stashes on request.state; current_principal reads the
    stash so the token is not parsed twice, but re-verifies if it is absent -
    the dependency must still be correct in isolation and under TestClient.
    """
    async def dispatch(self, request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        try:
            request.state.verified_token = verify(_raw_bearer(request))
        except KeyServerUnavailable:
            return JSONResponse({"detail": "authentication is temporarily unavailable"}, 503)
        except (TokenRejected, KeyError):
            return JSONResponse({"detail": "invalid or expired token"}, 401)
        return await call_next(request)
```

**`F:/Marketing AI OS/apps/agent-runtime/app/auth/route_audit.py`** — the process refuses to start. This is the fix for the "audit proves presence, not use" defect all three judges raised:

```python
PUBLIC_PATHS = frozenset({
    "/health",            # liveness only; the detail moved to /api/health/detail
    "/api/brand",         # the public lock-up, by design
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
})

# Pure functions of their input: no database, no tenant data. They need a
# principal (metering, abuse) but no workspace.
UNSCOPED = frozenset({"/api/compliance/check", "/api/economics", "/api/cta/recommend"})

_PRODUCERS = {authorized_workspace, authorized_action, authorized_approval,
              authorized_ad_account}


def assert_every_route_is_guarded(app: FastAPI) -> None:
    """Three assertions, not one. The winner design checked only the first, so a
    route written as `def thing(workspace_id: str, p = Depends(current_principal))`
    booted cleanly and read every tenant's rows.
    """
    problems: list[str] = []
    for r in (x for x in app.routes if isinstance(x, APIRoute)):
        if r.path in PUBLIC_PATHS:
            continue
        calls = _flatten(r.dependant)

        # 1. authenticated
        if current_principal not in calls:
            problems.append(f"{sorted(r.methods)} {r.path}: no principal dependency")

        # 2. scoped to a tenant - unless explicitly listed as pure
        if r.path not in UNSCOPED and not (_PRODUCERS & calls):
            problems.append(
                f"{sorted(r.methods)} {r.path}: authenticated but unscoped. Depend on "
                f"an AuthorizedWorkspace producer, or add the path to UNSCOPED with a "
                f"written reason."
            )

        # 3. the tenant id may be a PATH parameter and nothing else. A query
        #    parameter or body field named workspace_id is the bug this whole
        #    exercise exists to close, wearing a token.
        for f in (r.dependant.query_params + r.dependant.header_params
                  + r.dependant.cookie_params):
            if f.name in ("workspace_id", "org_id"):
                problems.append(f"{r.path}: `{f.name}` is a query/header parameter")
        for f in r.dependant.body_params:
            model = getattr(f.field_info, "annotation", None)
            for n in getattr(model, "model_fields", {}):
                if n in ("workspace_id", "org_id"):
                    problems.append(f"{r.path}: `{n}` is a body field on {model.__name__}")

    if problems:
        raise RuntimeError("routes are open or unscoped:\n  " + "\n  ".join(problems))
```

Called from the lifespan handler in `app/main.py`, right after `open_pools()`. A code review can miss a route; a failed boot cannot. (Nested body models are best-effort — the check walks one level. Say so in the docstring.)

---

## Step 6 — Impersonation: `F:/Marketing AI OS/supabase/migrations/20260911000002_impersonation_stamp.sql`

`core.platform_users.is_superadmin` already carries the comment *"read from the database on every check — never trusted from a JWT claim alone"*. Impersonation is the same rule one level up, and worse if broken: a claim outlives the session row, so a session ended at minute five would keep working until the token expires.

**Representation:** the token stays the superadmin's own. The *request* carries `X-Impersonation-Session: <uuid>`. `_resolve_impersonation` re-reads the row **every request** on the tenant connection under the superadmin's own claims:

```python
def _resolve_impersonation(operator: Principal, session_id: str) -> Principal:
    with operator.tx() as cur:
        cur.execute(
            """select s.org_id::text as org_id, s.target_user_id::text as target
                 from core.impersonation_sessions s
                where s.id = %s
                  and s.superadmin_id = auth.uid()
                  and s.ended_at is null
                  and s.expires_at > now()
                  -- A session with no target cannot reach tenant data. Enforced
                  -- here rather than by a CHECK constraint, because the FK is
                  -- `on delete set null` and a constraint would make deleting a
                  -- user fail on an old support session.
                  and s.target_user_id is not null
                  -- Impersonating another superadmin would fire every
                  -- `or core.is_superadmin()` branch in every policy and turn a
                  -- scoped errand into unbounded god mode.
                  and not core.is_superadmin(s.target_user_id)""",
            (session_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(403, "no live impersonation session")
    return Principal(
        # sub = the TARGET, so RLS reproduces exactly what that tenant sees -
        # which is the point of support access and needs no new policy. Keeping
        # sub as the superadmin would instead fire every is_superadmin() branch
        # and show them EVERY tenant.
        subject=uuid.UUID(row["target"]),
        actor=operator.subject,
        claims={"role": "authenticated", "sub": row["target"]},
        capabilities=_IMPERSONATED,
        impersonation_session_id=session_id,
        impersonation_org_id=row["org_id"],
    )
```

Setting `sub` to the target alone would make `core.log_audit` stamp the *tenant* as actor and leave `impersonated_by` null, because it looks the session up by `superadmin_id = v_actor`. The trail would be a lie. Hence:

```sql
create or replace function core.impersonation_stamp()
returns uuid
language plpgsql stable security definer set search_path = core, pg_catalog
as $fn$
declare
  v_id     uuid := nullif(current_setting('request.impersonation_session_id', true), '')::uuid;
  v_caller uuid := auth.uid();
  v_by     uuid;
begin
  if v_id is null then return null; end if;

  select s.superadmin_id into v_by
    from core.impersonation_sessions s
   where s.id = v_id
     and s.ended_at is null
     and s.expires_at > now()
     and s.target_user_id is not null
     -- On the TENANT connection auth.uid() is the impersonated target, so the
     -- GUC is bound to the session's own target and forging it would require
     -- naming a live session whose target is already you - which stamps your
     -- own action as impersonated and harms nobody but your attribution.
     --
     -- On the SERVICE connection auth.uid() is null by construction (no claims
     -- are ever set there), so this predicate is skipped. That branch is not a
     -- weakening; it is the whole point. Without it the pipeline writes - the
     -- ones that MOVE MONEY - would be the only unstamped records in a support
     -- session, because service_tx sets the GUC from a session the runtime
     -- already re-validated against the superadmin's verified token earlier in
     -- the same request. The winner design specified an auth.uid() predicate
     -- unconditionally and therefore could never stamp the privileged path.
     and (v_caller is null or s.target_user_id = v_caller);

  return v_by;
end;
$fn$;

revoke execute on function core.impersonation_stamp() from public;
grant execute on function core.impersonation_stamp() to authenticated, advit_backend;

-- core.log_audit is re-issued from 20260907000001 with ONE line changed:
--   v_imp_by := coalesce(core.impersonation_stamp(),
--                        <the existing superadmin_id = v_actor lookup>);
-- actor_id stays the impersonated user (that is who the action appears as) and
-- impersonated_by names the operator - which is what that column pair is for.

alter table marketing.actions
  add column impersonation_session_id uuid references core.impersonation_sessions(id);

comment on column marketing.actions.impersonation_session_id is
  'The money-moving record names the support session itself, not only the '
  'governance log beside it. "Who changed this budget" must be answerable from '
  'the row that changed it.';
```

`PostgresAuditSink.pre` threads `ws.principal.impersonation_session_id` into `service_tx(impersonation_id=…)` and into the new column.

Superadmin work *without* impersonation (reading the platform trail, activating an org) needs no special path — the tenant connection with their own `sub`, and the `or core.is_superadmin()` branches already in every policy.

---

## Step 7 — The web app (`F:/Marketing AI OS/apps/web`)

Add `@supabase/supabase-js` and `@supabase/ssr`. Scope `SUPABASE_SERVICE_ROLE_KEY` **out** of this app's environment — it would otherwise be inherited from the root `.env`.

**Next 16 shapes, verified against `node_modules/next/dist/docs/`:** `middleware.ts` is deprecated and renamed to **`proxy.ts`** exporting `proxy()` (`01-app/03-api-reference/03-file-conventions/proxy.md`; codemod `npx @next/codemod@canary middleware-to-proxy .`). `cookies()` is async. Route Handler `params` is a Promise. `export const dynamic` is gone. fetch is uncached by default — the comment already in `api.ts` knows this.

**The browser never reaches FastAPI.** Browser → Next server (Server Component render, or Server Action for mutations) → runtime over the private Docker network. **No `/api/proxy/[...path]` route handler**, because the one way this pattern fails catastrophically is a confused deputy — a handler forwarding an arbitrary path or workspace over the privileged channel — and the only reliable defence is that such a call cannot be spelled.

- `apps/web/src/lib/supabase/server.ts` — `createServerClient` over the awaited cookie store, anon key only.
- `apps/web/src/proxy.ts` — refreshes the session, bounces anonymous traffic. Commented explicitly as **UX, not the authorization boundary**: the Next docs note Proxy may be deployed to a CDN separately from render code, so from the API's point of view it is a client-side check. Uses `supabase.auth.getUser()`, **never `getSession()`** — `getSession()` returns whatever is in the cookie without asking the auth server and is spoofable by anyone who can write one.
- `apps/web/src/lib/session.ts` — `currentUser()` (wrapped in React `cache()`), and `requireWorkspace(id)` returning a **branded** `AuthorizedWorkspace` with a private `unique symbol` and no exported constructor, proved through PostgREST as the user so RLS is the check.
- `apps/web/src/lib/api.ts` — rewritten: `call()` gains a `bearer()` that reads the session server-side, and every method takes a branded `AuthorizedWorkspace` as its first parameter. **A handler that forgets the check cannot type-check.** The `Health` type loses the fields that moved to `/api/health/detail`.
- Delete `DEMO_WORKSPACE_ID` from `apps/web/src/app/chat/page.tsx`. A hard-coded workspace constant in a page is exactly the client-side tenancy this design removes.

**CSRF, paid explicitly rather than assumed away.** The alternative — handing the access token to the browser and calling FastAPI directly — has *no* CSRF exposure at all, because an `Authorization` header is never attached automatically. That is genuinely its one advantage. Its cost: the token must be readable by JS, so any XSS in the dashboard becomes a stealable, replayable credential for every tenant route, and FastAPI must be internet-facing with a CORS allowlist (a permanent opportunity to misconfigure `allow_origins=["*"]` beside `allow_credentials=True`). We take the CSRF cost, four ways: (1) mutations are **Server Actions only** — Next 16 checks Origin against Host for them; (2) set `serverActions.allowedOrigins` in `next.config.ts`, or Coolify's `X-Forwarded-Host` makes that check fail unpredictably; (3) cookies `httpOnly, secure, sameSite=lax`, which blocks cross-site subresource requests — so **no GET may mutate**; (4) if a streaming GET route handler is ever added for chat, it must check `Sec-Fetch-Site === "same-origin"` itself, because route handlers get none of the Server Action protections.

Residual leak surface: the access token lives in the Next server's memory and an httpOnly cookie. Never log the header; never echo upstream response headers wholesale. Product cost, stated: no browser Supabase client means no realtime subscriptions and a server round trip per interaction.

---

## Step 8 — The unattended paths: `F:/Marketing AI OS/apps/agent-runtime/app/jobs/runner.py`

A **separate process with no HTTP surface at all** — `python -m app.jobs.runner`, its own Coolify service, APScheduler in-process. Not a router on the API app. Not a protected endpoint. None. (Note: no scheduler exists yet; this is new code, so there is nothing to migrate and nothing to break.)

It authenticates by holding the `advit_jobs` Postgres password. `auth.uid()` is null, so `core.log_audit` takes its trusted branch and honours `p_actor_type => 'automation'` with the job name in the payload — attribution a tenant is structurally forbidden from producing.

**Why this cannot be abused to bypass the per-request check: its workspace ids come from a SELECT over our own tables, never from a request.**

```python
DUE_WORKSPACES = """
select w.id::text as id, w.org_id::text as org_id
  from marketing.workspaces w
 where not w.is_paused and core.access_mode(w.org_id) = 'full'
 order by w.id
"""
# Each row becomes system_workspace(id, org_id) - the same guarded constructor,
# and the only producer that is not a request resolver. There is no input
# channel. The 07:30 brief, the 20:30 business-truth prompt and the 15-60 minute
# monitoring loop all enumerate.
```

**The trap I am refusing, by name.** `.env.example` ships `AGENT_RUNTIME_SECRET` — *"Shared secret for web -> agent-runtime calls"* — with zero code references. The tempting design is `POST /internal/jobs/run?workspace_id=X` with `X-Runtime-Secret`. That is today's vulnerability with one extra header: a bearer credential with no subject, no expiry, no audience, no per-actor revocation and no `pg_stat_activity` attribution, combined with a caller-supplied `workspace_id`. **Delete the variable** rather than leave the temptation in the repo.

If a manual trigger is ever wanted, it is `POST /api/workspaces/{workspace_id}/brief/run` — an ordinary authenticated route taking an `AuthorizedWorkspace` — that **enqueues** an intent row RLS permitted it to write. The job loop still re-derives the workspace from its own row. The HTTP caller never hands a workspace id to the privileged path.

---

## Step 9 — Order, and why there is no `REQUIRE_AUTH` flag

1. **Step 0** — unpublish the port in Coolify. *Zero code. The exploit dies here.*
2. Migration `20260911000001_runtime_roles.sql` + local seed. *Nothing uses the roles yet.*
3. **Runtime PR 1** — `app/db/pools.py`, every `psycopg.connect` moved, the two `p_org` fixes, `responded_by = auth.uid()`, `/health` split (+ its test and the `Health` type). *No auth yet. Deploy, watch, confirm no privilege errors.*
4. **Web PR** — `@supabase/ssr`, `/login`, `proxy.ts`, `session.ts`, branded `AuthorizedWorkspace`, `bearer()` in `api.ts`, `DEMO_WORKSPACE_ID` deleted. **Sends the header; the runtime still ignores it.** Nothing can break.
5. **Runtime PR 2** — `app/auth/*`, routes moved under `/api/workspaces/{workspace_id}/…`, the three entity resolvers, the middleware and the boot audit **in the same commit**, so there is never a commit where the audit exists but tolerates gaps. Because the port is closed and the caller is already correct, this is a tightening, not a cutover.
6. Migration `20260911000002_impersonation_stamp.sql` + **Runtime PR 3** (`X-Impersonation-Session`) + `apps/superadmin`.
7. **Runtime PR 4** — `app/jobs/runner.py` as its own service. Last, deliberately: it is the only component holding a privileged credential with no human in the loop, and it should be built after the boundary it must not cross is enforced and tested.
8. Only now, if ever, publish anything. Nothing above requires re-opening the port.

The ordering principle, grafted from the runner-up: **make the caller correct first, then make the callee strict.** That is what removes the need for a flag.

**Is there an interim `REQUIRE_AUTH` flag? No, and it is a trap — specifically this one:**

- `Settings` is pydantic-settings with defaults. A boolean defaulting to false is *absent* from the production `.env` (because someone copied `.env.example`), gets its default silently, and the service is open **while the config file documents that auth is implemented**. That is worse than no auth, because it defeats the audit.
- It has to be read at request time, so `assert_every_route_is_guarded` cannot be written against it — the guarantee would have to be disabled in exactly the environment that most needs it.
- Two code paths; CI exercises the one production does not.
- Nothing pages when it is wrong.

If overruled, the only defensible shape is inverted and fail-closed: `auth_disabled_until: datetime = Field(...)` — **required, no default, so the process refuses to boot without an explicit value** — with a validator rejecting anything more than seven days out, surfaced in `/health` so monitoring can alarm on it. It is still strictly worse than Step 0, which achieves the same thing with a Coolify setting and no code.

---

## Tests (house style)

`F:/Marketing AI OS/packages/saas-core-db/tests/conftest.py` gains `tenant_dsn` / `service_dsn` fixtures.

**`packages/saas-core-db/tests/test_runtime_roles.py`**
- `test_the_tenant_credential_cannot_reach_marketing_without_first_becoming_authenticated` — the fail-closed property the whole NOINHERIT choice buys; with INHERIT this silently succeeds
- `test_the_tenant_credential_is_not_a_member_of_service_role_or_advit_backend` — one stray grant would reinstate cross-tenant reach through a single `set role`
- `test_the_backend_role_is_not_bypassrls_and_cannot_read_outside_core_and_marketing` — the shared-cluster property: a leaked runtime password must not reach the HRMS product
- `test_a_tenant_connection_cannot_insert_into_the_action_spine` — proves the premise the split rests on, rather than assuming it
- `test_the_role_and_claims_do_not_survive_a_connection_returning_to_the_pool` — a `set` where `set local` was meant runs the next caller's request as the previous caller, a cross-tenant read no route test would ever catch
- `test_a_workspace_policy_read_as_a_non_member_reports_zero_committed_spend` — pins the documented weakness so nobody later "simplifies" cap arithmetic onto the tenant connection: an invisible ad set is indistinguishable from an absent one, and a cap computed from a filtered sum is not a cap
- `test_an_org_member_without_a_workspace_grant_passes_workspaces_select_but_fails_is_workspace_member` — the exact asymmetry the gate must respect, asserted so a future policy change surfaces it

**`packages/saas-core-db/tests/test_impersonation_stamp.py`**
- `test_log_audit_stamps_impersonated_by_from_the_session_row_and_never_from_a_claim`
- `test_the_stamp_still_fires_on_the_service_connection_where_auth_uid_is_null` — the branch the winner design got wrong; without it the money-moving writes are the only unstamped records
- `test_an_ended_impersonation_session_leaves_the_next_write_unstamped_and_is_refused` — ending a session must end its authority on the next request, not at token expiry
- `test_a_session_with_no_target_user_cannot_be_used_to_reach_tenant_data`
- `test_a_workspace_scoped_audit_row_written_from_a_tenant_connection_names_its_organisation` — the verified `audit_org_required` break, pinned so PR 1 cannot regress

**`apps/agent-runtime/tests/test_auth_tokens.py`**
- `test_a_request_without_a_bearer_token_is_refused_before_any_query_runs` — the live vulnerability as a test: `GET /api/workspaces/<uuid>/dashboard` with no header must be 401 and must not touch the database
- `test_a_valid_token_for_workspace_a_cannot_read_workspace_b_and_is_told_it_does_not_exist` — 404 not 403, because a 403 confirms existence and rebuilds the enumeration oracle 20260910000001 closed
- `test_an_org_member_who_is_not_a_workspace_member_cannot_drive_the_tool_pipeline` — the intra-org boundary, the defect all three judges found
- `test_an_expired_token_is_refused_even_one_second_past_the_thirty_second_leeway`
- `test_a_token_signed_with_the_wrong_key_is_refused`
- `test_a_token_signed_hs256_against_the_published_jwks_public_key_is_refused` — algorithm confusion; blocked because the algorithm list comes from configuration and never from the header
- `test_a_supabase_service_role_token_presented_over_http_is_refused` — it has no `sub`, so forwarding its claims would make `auth.uid()` null and send `core.assert_org_visible` down its backend branch, reopening from HTTP the hole 20260910000003 closed in SQL
- `test_an_unreachable_key_server_degrades_to_503_and_never_to_401`

**`apps/agent-runtime/tests/test_route_audit.py`**
- `test_every_route_declares_the_principal_dependency_or_the_process_refuses_to_start`
- `test_an_authenticated_route_that_reaches_no_authorized_workspace_producer_fails_the_boot` — the gap between "has a token" and "is scoped to a tenant"
- `test_a_route_declaring_workspace_id_as_a_query_parameter_or_body_field_fails_the_boot`
- `test_the_rollback_route_resolves_its_workspace_from_the_action_row_under_rls` — a signed-in user who learns an action UUID must not roll back another tenant's action

**`apps/agent-runtime/tests/test_service_connection_surface.py`**
- `test_service_tx_is_imported_only_by_the_policy_store_the_rule_loader_the_secret_reader_and_the_jobs_package` — the privileged connection is safe only while its call sites stay enumerable
- `test_no_runtime_module_reads_the_superuser_database_url`

**`apps/agent-runtime/tests/test_db_pools.py`**
- `test_the_pool_reset_hook_returns_the_connection_idle_so_the_pool_is_not_silently_a_connection_factory` — psycopg_pool discards an `INTRANS` connection after the reset callback
- `test_a_chat_run_does_not_hold_a_tenant_connection_across_the_model_call`
- `test_the_orchestrator_refuses_to_run_with_no_bound_tenant_transaction` — fail closed rather than falling back to the privileged connection
- `test_the_mutation_lock_connection_does_not_use_the_pooled_port`

**`apps/agent-runtime/tests/test_jobs.py`**
- `test_the_scheduler_enumerates_due_workspaces_and_accepts_no_caller_supplied_identifier`
- `test_the_job_process_registers_no_fastapi_app_at_all` — the anti-bypass property, asserted rather than assumed
- `test_a_scheduled_action_is_audited_as_automation_and_never_as_a_user`

**`apps/agent-runtime/tests/test_pipeline_integration.py`** (additions)
- `test_an_impersonated_budget_change_names_the_support_session_on_the_action_row`
- `test_a_support_session_cannot_discharge_an_approval`
- `test_an_approval_response_records_the_signature_from_the_session_and_not_from_the_payload`

**`apps/web`** — `test_the_runtime_client_cannot_be_called_without_a_branded_authorized_workspace` (a `tsc --noEmit` fixture asserting the negative case fails to compile), `test_a_server_action_from_a_foreign_origin_is_refused`.

---

## What is NOT covered, and the residual risk

1. **Session revocation.** Local JWT verification cannot see a sign-out; a signed-out user's access token works until `exp`. Cutting `auth.jwt_expiry` to 900s shrinks the window at the cost of refresh traffic, and the `is_active` read catches *deactivation* on the next request. Genuine revocation needs a per-request GoTrue call or a denylist. I have chosen neither, deliberately.

2. **Route-level role checks stay in Python.** RLS gives row visibility. It does not give "only an owner or admin may raise a cap", and it does not encode `marketing.workspace_members.role`. Those remain `if` statements reading from the database — and the boot audit catches a missing *scope*, not a missing *role* check.

3. **The read/write asymmetry of the split is permanent.** Picking the wrong pool for a **write** fails loudly (the grant matrix raises `permission denied`). Picking the wrong pool for a **read** fails *silently* — an aggregate misplaced on the tenant connection returns zeros and a cap check passes. `workspace_policy` is pinned by a test; the general class is defended only by the rule in the `pools.py` docstring and a judgment call every new query author must make. **This is the design's real long-term liability.** Mitigation worth adding later: have `WorkspacePolicy` carry the connection it was read on and have the pipeline assert it.

4. **`AuthorizedWorkspace` is a runtime guard, not a compiler-enforced newtype.** Determined code can reach `app.auth.scope._MINT`. It fails loudly and there is a test, but in a language with real newtypes this would be unforgeable and here it is merely inconvenient to forge. The TypeScript side *is* compiler-enforced; the Python side is not.

5. **Impersonation reproduces the tenant's view faithfully** — which is the point and the exposure. Support staff see business truth, margins and creative. Under DPDP (penalties to ₹250 crore per contravention) that is real. The controls are the 4-hour ceiling, the required reason, the tenant-visible session row, the per-request re-validation, the org pin, the superadmin-target refusal, and the two-place stamp. There is no technical control that lets someone debug a dashboard without seeing what is on it.

6. **`advit_backend` policy maintenance.** A `marketing` table added later without an `advit_backend_all` policy breaks the service path. That is fail-closed and a standing test (`test_every_table_in_core_and_marketing_carries_a_backend_policy_or_the_split_is_incomplete`) catches it — but it is ongoing work `grant service_role` would not require. I judged the shared-cluster blast radius worth the maintenance; the escape hatch, if it proves too heavy, is `grant service_role to advit_service`, at the cost of handing that credential BYPASSRLS over HRMS.

7. **`advit_tenant` inherits whatever grants HRMS gives `authenticated`** on its own tables. Our code never queries them and HRMS's RLS would evaluate with our tenant's `sub` and match nothing, so practical exposure is nil — but the privilege exists. That is a property of two products on one cluster, not something this design removes.

8. **The introduction of pooling creates a hazard that does not exist today.** `PostgresLockManager` holds a session advisory lock across several transactions on purpose. `[db.pooler] enabled = false` right now, so nothing is broken — but the natural Coolify move is to put Supavisor in transaction mode in front of everything, at which point the lock is held on a shared server connection and may be unlocked on a different one, with nothing failing visibly. It relies on a comment and one test to stay correct.

9. **Step 0 does most of the security work and none of the design work.** An honest reading: steps 1–9 are the right architecture, but the vulnerability closes at step 0. If the team stopped after step 0 they would be far safer and would have shipped nothing. That is the correct order of operations, and it makes everything below it optional under time pressure — which is worth saying out loud rather than discovering under one.