# Deploying Ad-Vit

Ad-Vit is four processes and one database. This guide is provider-neutral; the
reference deployment runs each process as a container built from `infra/docker/`
and managed by Coolify, on a Supabase (Postgres + GoTrue + Storage) cluster.

| Process | Image | Serves |
|---|---|---|
| `agent-runtime` | `infra/docker/agent-runtime.Dockerfile` (target `api`, the default) | FastAPI on `:8000`, internal only |
| `jobs` | same Dockerfile, build target `jobs` | no port, no HTTP surface |
| `web` | `infra/docker/web.Dockerfile` | tenant app, Next.js on `:3000` |
| `superadmin` | `infra/docker/superadmin.Dockerfile` | operator console, Next.js on `:3001`. Put it on its own hostname so it can be firewalled to staff |

Never publish the agent runtime to the internet. `web` and `superadmin` reach it
over a private network at `AGENT_RUNTIME_URL`.

## Database

Ad-Vit owns the `core` schema (organisations, RBAC, entitlements, billing,
audit) and the `t_advit` product schema. If you share a Supabase cluster with
other products, never run anything cluster-wide: no `supabase db reset`, no
`truncate`, nothing that names another schema.

Migrations live in `supabase/migrations/` and are forward-only. Apply them in
filename order with the Supabase CLI or any SQL runner, then record what ran.
**Seeds in `supabase/seeds/` are local fixtures only** (dev users with
committed passwords, a demo organisation, one fictional workspace). Never apply
them to production.

The code checks the schema at boot (`app/db/expectations.py`). A container whose
build needs a migration the database lacks answers **503 on `/health`** and your
orchestrator should keep the previous container serving; the jobs process exits
non-zero and keeps restarting. Adding a migration means adding one line to
`expectations.py`, and `test_schema_expectations` fails until you do.

### Runtime roles

Migration `20260911000007_runtime_roles.sql` creates two `NOLOGIN` roles,
`advit_tenant` and `advit_service`. Give them login passwords with a deploy-time
step from a generated secret, not from a migration in git. The runtime never
connects as `postgres`.

## Release order

1. Apply new migrations.
2. Deploy `agent-runtime`, then `jobs`, then `web`, then `superadmin`.

## Environment variables, per app

Values belong in your secret store, never in git. Names, and which app reads them:

| Key | runtime | jobs | web | superadmin | Purpose |
|---|---|---|---|---|---|
| `TENANT_DATABASE_URL`, `SERVICE_DATABASE_URL` | ✓ | ✓ | | | the two login roles |
| `SUPABASE_URL` | ✓ | ✓ | | | JWT issuer default, Storage base URL |
| `SUPABASE_JWKS_URL` (or `SUPABASE_JWT_SECRET`) | ✓ | | | | verify Supabase Auth tokens locally |
| `ADVIT_SECRETS_MASTER_KEY` | ✓ | ✓ | | | AES-256-GCM master key for stored customer keys (BYOK) |
| `SUPABASE_SERVICE_ROLE_KEY` | ✓ | | | | signed uploads, inviting an owner. **Runtime only**, it bypasses storage policies |
| `WEB_BASE_URL` | ✓ | | | | where an invitation link lands (`/auth/confirm`). No default on purpose |
| `SELLER_GSTIN`, `SELLER_STATE_CODE`, `SELLER_LEGAL_NAME` | | ✓ | | | invoice generation. Unset means invoices stay drafts |
| `SELLER_UPI_ID`, `SELLER_BANK_*` | ✓ | | | | where customers pay. Unset means payment requests answer 503 |
| `PAYMENT_WINDOW_HOURS` | ✓ | | | | hours an owner has to pay after asking (default 4) |
| `META_DRIVER`, `META_*` | ✓ | ✓ | | | Meta access. `fixture` (default) cannot spend |
| `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` | ✓ | ✓ | | | model routing |
| `OTEL_SERVICE_NAME` | ✓ | ✓ | | | tracing |
| `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` | | | ✓ | ✓ | Supabase Auth client |
| `AGENT_RUNTIME_URL` | | | ✓ | ✓ | the runtime's private address, never a public host |

See `.env.example` for a commented template of every variable.

> **Do not pass secrets as Docker build `ARG`s.** `ARG` values are stored in the
> image history and printed in build logs. Inject secrets at runtime as
> environment variables, or use BuildKit `--secret` mounts. Only
> `NEXT_PUBLIC_*` values (which are public by design) belong at build time.

## Scheduled work

The jobs process ticks every minute and asks the database which workspaces have
not had today's work yet, in their own local day. Two replicas are safe. In IST:
`platform_watch` at 05:00, `settle_subscriptions` at 00:15 and `raise_invoices`
at 00:30, plus per-workspace `ingest_metrics` (06:00) and `close_the_loop`
(06:30).

## Money

There is no payment gateway. An owner asks to pay an invoice, is shown your
`SELLER_*` details with a time window, pays by UPI or NEFT against the invoice
number and types the reference. An operator matches it on the bank statement in
the console's Payments inbox and approves or rejects. Confirm the GST rate and
SAC code with your CA before issuing real invoices.

## Hardening checklist

- Firewall the operator console to staff.
- Do not expose your orchestrator's admin port to the internet.
- Keep `META_DRIVER=fixture` until your Meta app is through App Review.
- Rotate `ADVIT_SECRETS_MASTER_KEY` using `ADVIT_SECRETS_MASTER_KEY_2`, deploy,
  re-encrypt, then remove the old one.
- Set up database backups and a restore drill before you take real customers.
