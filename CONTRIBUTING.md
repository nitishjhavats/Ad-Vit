# Contributing to Ad-Vit

Thanks for helping. Issues and pull requests are welcome.

## Getting set up

See the **Prerequisites** and **Running** sections of [README.md](README.md):
Docker Desktop, Node 22+, Python 3.11+, and a local Supabase stack
(`npx supabase start`).

## Before you open a pull request

```bash
# database and RLS guards
python -m pytest packages/saas-core-db

# agent runtime
cd apps/agent-runtime && python -m pytest

# TypeScript, including the compile-time workspace-id guard
npm ci && npm run typecheck --workspaces --if-present
```

CI runs the same checks on every push and pull request.

## Rules that keep this codebase safe

- **Migrations are forward-only.** Add a new file under `supabase/migrations/`;
  never edit an applied one. Revoke `EXECUTE` from `PUBLIC` on every new function
  (a standing test enforces this). Add a line to `app/db/expectations.py`.
- **No secrets in git.** Not in migrations, seeds, Dockerfiles or `ARG`s. Local
  fixtures use `127.0.0.1` and obviously fake passwords only.
- **Tenant isolation is enforced by RLS**, not by application `where` clauses
  alone. A new table needs policies and a test in `packages/saas-core-db/tests`.
- **A stage that did not run reports `not_evaluated`, never `pass`.**
- **Writes to Meta stay behind both switches** (`META_WRITE_ALLOWLIST` and
  `meta_connections.write_enabled`) and creation and activation are never one call.

## Pull requests

Keep changes focused, explain the why in the description, and add or update tests.
By contributing you agree that your contribution is licensed under the
[AGPL-3.0](LICENSE).
