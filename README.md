# Ad-Vit : Agentic Meta ads AI OS

<sub>by **[Broadmate Global](https://broadmate.org)** · hosted at [ad-vit.broadmate.org](https://ad-vit.broadmate.org) · licensed AGPL-3.0</sub>

An open-source, multi-tenant **agentic Meta advertising OS**, built for Indian
Ayurveda / D2C advertisers and on a reusable SaaS platform core. Agents propose,
a policy layer and a compliance gate check, a human approves, and every change
is read back from Meta and audited.

- **Agent runtime** (FastAPI + LangGraph): orchestrator, policy layer, compliance
  gate, model router and Meta drivers. No agent names a model, it names a role.
- **Tenant app** (Next.js): dashboard, chat, reports and suggestions, creative
  studio, billing, settings.
- **Operator console** (Next.js): organisations, coupons, Platform Watch inbox,
  invoices, payments inbox and the audit trail.
- **Unattended jobs**: a separate process with no HTTP surface that ingests
  metrics, closes the loop, raises invoices and settles subscriptions.
- **SaaS core** in its own product-agnostic `core` schema: organisations, RBAC,
  entitlements, billing and an append-only audit log, enforced by Postgres RLS.
- **Safe by default**: the Meta driver replays fixtures and cannot spend, writes
  need two independent switches, and creation and activation are never one call.

Two specifications drive it: a PRD for the product and a Common SaaS Core brief
for the control plane. Only Ad-Vit is built on the core today, but the core is
kept product-agnostic so lifting it into a shared control plane is a move, not a
rewrite.

The suite covers the agent runtime, the database (RLS, entitlements, billing,
audit) and a TypeScript compile-time test that a runtime call cannot take an
unproved workspace id.

## Hosted vs self-hosted

| | Self-hosted (this repo) | Hosted by Broadmate Global |
|---|---|---|
| Cost | Free, AGPL-3.0 | Paid service at [ad-vit.broadmate.org](https://ad-vit.broadmate.org) |
| Setup | You run Supabase, the runtime, the web app, the console and the jobs process | None, sign up and connect your ad account |
| Meta app and App Review | Your own credentials | Handled for you |
| Updates and backups | You | Managed |
| Support | GitHub issues | support@broadmate.org |

If you want to use Ad-Vit without running it yourself, you can buy the hosted
service at **[ad-vit.broadmate.org](https://ad-vit.broadmate.org)**.

## Layout

```
apps/
  agent-runtime/     FastAPI + LangGraph orchestrator + policy layer +
                     compliance gate + model router + Meta drivers
  web/               tenant surface: dashboard, chat, reports/analytics/
                     suggestions, creative studio, billing, settings (Next 16)
  superadmin/        operator console: organisations, coupons, Platform
                     Watch inbox, invoices, the audit trail (Next 16, port 3001)
config/
  routing.yaml       model routing by task class, grounded in live pricing
packages/
  saas-core-db/      core schema tests
  marketing-db/      Meta fixtures captured from the live accounts
  saas-core-auth/    session, RBAC helpers    (not yet built)
  saas-core-billing/ plans, coupons, GST      (not yet built)
  contracts/         OpenAPI source of truth  (not yet built)
supabase/
  migrations/        core + marketing schema, forward-only
  seeds/             LOCAL fixtures only: dev users, demo orgs, a workspace,
                     runtime-role passwords. Catalogue and packs are migrations.
```

## Prerequisites

Docker Desktop, Node 22+, Python 3.11+.

> **Docker Desktop 4.48 note.** If Docker refuses to start with
> `initializing Inference manager` or `initializing Secrets Engine`, it is an
> orphaned unix socket it can create but not delete. Clear them from WSL while
> Docker is stopped:
> ```bash
> wsl -d Ubuntu -e sh -c 'rm -f /mnt/c/Users/$USER/AppData/Local/Docker/run/* /mnt/c/Users/$USER/AppData/Local/docker-secrets-engine/engine.sock'
> ```

## Running

```bash
npm install
npx supabase start
```

`supabase start` prints the API URL and keys — copy them into `.env` from
`.env.example`. Reset the database and reload all seeds with
`npx supabase db reset`. Studio runs at http://127.0.0.1:54323, Postgres on
54322.

The agent runtime uses its **own virtualenv**, so installing it cannot disturb
packages elsewhere on the machine:

```bash
cd "apps/agent-runtime" && python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

Then start it:

```bash
cd "apps/agent-runtime" && ./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

Interactive API docs at http://127.0.0.1:8000/docs.

Every route except `/health` and `/api/brand` needs a bearer token. Sign in
against the local Supabase to get one — the seeded owner is
`owner@advit.local` / `owner-dev-password`:

```bash
curl -s "http://127.0.0.1:54321/auth/v1/token?grant_type=password" -H "apikey: $SUPABASE_ANON_KEY" -H "Content-Type: application/json" -d '{"email":"owner@advit.local","password":"owner-dev-password"}'
```

### Releasing

Production is a container deployment (the reference setup uses Coolify) on a Supabase cluster; the order
(push → apply migrations → deploy runtime, jobs, web, console) and the env contract per app are in [docs/deployment.md](docs/deployment.md).
The build states what it needs from the database in `app/db/expectations.py`
and `/health` answers 503 until the database has it.

### The operator console

`apps/superadmin` is a second Next app on port 3001, deployed on its own
hostname so it can be firewalled to staff. It signs in against the same
Supabase Auth and asks the runtime `GET /api/admin/me` on every render; a
signed-in tenant gets the same 404 there that every `/api/admin` route gives,
because `core.is_superadmin()` is read from the database per request and never
from a claim. Locally, the seeded operator is `superadmin@advit.local` /
`superadmin-dev-password` (both in `supabase/seeds/01_core.sql`).

```bash
npm run dev:superadmin
```

It needs `apps/superadmin/.env.local` with `NEXT_PUBLIC_SUPABASE_URL`,
`NEXT_PUBLIC_SUPABASE_ANON_KEY` (from `npx supabase status -o env`) and
`AGENT_RUNTIME_URL`. Everything it can do is on the tenant connection under
the operator's own claims - `apps/agent-runtime/app/routes_admin.py` reaches
nothing that is not subject to RLS.

### The unattended process

A **separate process with no HTTP surface at all**. It holds the `advit_jobs`
Postgres credential, and its workspace ids come from a `SELECT` over our own
tables rather than from any caller — which is what stops the scheduler being a
way around the per-request tenancy check:

```bash
cd "apps/agent-runtime" && ./.venv/Scripts/python.exe -m app.jobs.runner
```

It ticks every minute and asks the database which workspaces have not had
today's work yet, **in their own local day** — rather than holding "fire at
07:30" in memory. That is what makes two replicas safe (the unique constraint on
`t_advit.job_runs` decides, not a leader election), a window missed during a
deploy run late rather than be skipped, and 07:30 mean 07:30 where the customer
is.

## What works today

No model key is required for any of this — every figure is computed in SQL or
by deterministic code.

| Endpoint | What it does |
|---|---|
| `GET /health` | Liveness and the product lock-up. Public, so it publishes nothing operational |
| `GET /api/health/detail` | The driver and the write allowlist. Authenticated |
| `POST /api/compliance/check` | The pre-flight gate: both layers, spans quoted, sources cited |
| `GET /api/audit/account/{id}` | Structural and measurement audit with a scored punch-list |
| `GET /api/workspaces/{id}/connections/health` | Autonomy intent vs effective, access mode, per-account write state |
| `GET /api/workspaces/{id}/approvals` | Pending proposals with their reasoning and horizon |
| `POST /api/approvals/{id}/respond` | Approve, modify or reject — and an approval **executes** the action it authorised |
| `POST /api/workspaces/{id}/sync` | Read every connected ad account and write what Meta reports |
| `POST /api/actions/{id}/rollback` | Revert — itself routed through the policy layer |
| `GET /api/dashboard` | KPI band; money row first |
| `POST /api/daily-truth` | Business truth, English or Hinglish, structured or free text |
| `POST /api/economics` | RTO-adjusted contribution margin and the derived CAC ceiling |
| `POST /api/cta/recommend` | Conversion destination from economics, not preference |
| `POST /api/chat` | The control surface — full run, with facts and cost |
| `GET /api/brand` | The lock-up, in JSON / HTML / Markdown / text |

Run against a connected account, the audit returns a scored punch-list with
blocking findings called out (for example, an ad account with no dataset/pixel
cannot run the closed conversion loop this product optimises).

## Tests

```bash
python -m pip install -r packages/saas-core-db/requirements.txt
python -m pytest packages/saas-core-db                      # schema, RLS, entitlements, ruleset
cd "apps/agent-runtime" && ./.venv/Scripts/python.exe -m pytest
```

| Suite | What it guards |
|---|---|
| `test_rls_isolation.py` | Cross-tenant reads return **zero rows**. Privilege-escalation attempts, audit immutability. |
| `test_entitlements.py` | Resolution order (override → plan → default) and the access-mode ladder. |
| `test_policy_rules_seed.py` | Seeded patterns run through Postgres' own regex engine. |
| `test_compliance_gate.py` | PRD Appendix D.3 against a fixed ruleset. |
| `test_compliance_from_database.py` | The same, against the ruleset that actually ships. |
| `test_tool_pipeline.py` | All fifteen steps: allow-lists, autonomy, guardrails, approvals, verification, idempotency. |
| `test_pipeline_integration.py` | The adapters against the real schema. |
| `test_api.py` | The HTTP surface, including the negative cases. |

## Three ideas worth knowing before reading the code

**Entitlements are the authorization spine, not a billing ornament.** The PRD
independently specifies per-workspace token budgets, autonomy ceilings L0–L4 and
spend caps; the platform brief independently asks for feature and limit control.
They are the same system. `t_advit.effective_autonomy()` is
`min(workspace intent, plan entitlement)`, floored to L0 when the workspace is
paused or the organisation is not in full access — and the runtime reads that
function rather than recomputing it, so SQL and Python cannot drift.

**A stage that did not run reports `not_evaluated`, never `pass`.** The
compliance gate implements stages 1–6 and 9 of the PRD's nine. Coverage is
reported as three *disjoint* sets — evaluated, partial, skipped — because a
stage can be genuinely half-covered, and a report claiming a stage was both
checked and unchecked undermines itself. Clean copy returns `NOT_EVALUATED`.

**Policy freshness is per jurisdiction.** Meta's advertising policy changed
materially twice in 2026; the DMR Act did not. Meta rules get a 90-day window,
Indian instruments 365. Stale rules are named in every API response so the
caller can say it needs to verify the current rule instead of asserting an old
one.

## Safety posture

- **The Meta driver defaults to `fixture`** — no network client, so it cannot
  spend.
- **Writes need two independent switches**: `META_WRITE_ALLOWLIST` (the
  operator's) and `meta_connections.write_enabled` (the product's). The demo
  seed enables exactly one fictional account, `1000000000000003`, which has **no
  payment method** — an accidental activation cannot spend. Every other account
  is read-only.
- **Creation and activation are never one call.** A misread budget becomes a
  paused artefact costing nothing.
- **Guardrails precede approvals.** An action that would breach a hard cap is
  refused outright, so nobody is asked to approve a known breach.
- **`core.audit_log` is append-only** by trigger; privileged code cannot rewrite
  history either.
- **`t_advit.actions` refuses any `critical`-class row without an approval**, as
  a check constraint.
- **Success is a read-back that matches the proposal**, not a 200 response.

## Verify before commercial use

- The **Schedule J term list** in `supabase/migrations/20260917000003_the_packs_are_platform_data.sql` is
  encoded from public summaries of the Drugs & Magic Remedies Act, and it
  includes piles, haemorrhoids, bawasir and fistula — directly relevant to the
  Ayurveda / D2C advertisers this product targets. **Confirm against the current Schedule, with counsel, before relying
  on it.** Rules needing this carry `needs_legal_verification`.
- **GST rate and SAC code** (18%, 998314) are defaults in `.env.example`.
  Confirm both with your CA before issuing a real invoice.
- Seller GSTIN, legal entity name and registered address are required before
  invoices can be generated.

## Model routing

Declarative, in `config/routing.yaml`. No agent names a model — it names a
**role**, the role maps to a **class**, and the class resolves to a model plus a
fallback chain. Following the frontier is a config change and an eval run.

| Class | Model | Rate /MTok | Measured cost per call |
|---|---|---|---|
| Judgement | `anthropic/claude-opus-5` | $5 / $25 | ~₹2.47 |
| Working | `anthropic/claude-sonnet-5` | $2 / $10 | ~₹0.77 |
| Bulk | `moonshotai/kimi-k2.5` | $0.45 / $2.25 | ~₹0.036 |
| Embedding | *unresolved — fails loudly* | — | — |

That **69× gap** between judgement and bulk is why compliance screens cheaply
and adjudicates expensively.

Prices were read from OpenRouter's `/models` endpoint rather than assumed, which
corrected the PRD: it budgets the bulk tier against Kimi K2.6 at ~$0.54/$2.28,
but K2.6 actually lists at $0.95/$4.00. K2.5 matches the PRD's stated economics.

## Not yet built

- Approval suspend/resume via LangGraph `interrupt()`. Today a run that needs
  approval ends `awaiting_approval` and `POST /api/approvals/{id}/respond`
  executes the bound request later; the graph itself is not suspended and
  resumed (the `media_buying` node exists and invokes the pipeline)
- `GraphApiDriver` — needs your own Meta app credentials; the session-scoped Ads
  MCP connection is not reachable from a server process
- A payment gateway. Money is collected by UPI or NEFT against the invoice
  number: the owner asks to pay from the billing page, is shown the seller's
  details with a four-hour window, pays outside the product and types the
  reference; an operator matches it on the bank statement in the console's
  Payments inbox and approves, which pays the invoice and puts the
  subscription on `active`. The clock walks the rest nightly
  (`settle_subscriptions` at 00:15 IST: trial end, period roll, past due,
  expiry). What is missing is only the gateway that would replace the
  operator's match
- Email of any kind: an operator creates the organisation from the console and
  hands the owner the invitation link by hand. The link opens `/auth/confirm`
  on the tenant app - one Continue button, so a chat client's link preview
  cannot spend the one-time hash - and the owner chooses a password. Nothing
  emails the link. A link that expires unused (about a day), or a forgotten
  password, is a **Sign-in link** from the console's Members card - a GoTrue
  recovery link, audited by whom it was for, never by what it was. There is
  no self-service "forgot password" because there is no email
- The Suggestions tab shows the question the CTA gate is holding a proposal
  behind (`t_advit.held_proposals`); answering it on the settings page
  releases the row, but the released proposal is not re-run - the next chat
  turn re-proposes with the destination written in

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).
Report security problems privately, see [SECURITY.md](SECURITY.md).

## License

[GNU AGPL-3.0](LICENSE). If you modify Ad-Vit and offer it to others as a
network service, you must make your modified source available to those users.
Broadmate Global's hosted service at [ad-vit.broadmate.org](https://ad-vit.broadmate.org)
is offered separately.
