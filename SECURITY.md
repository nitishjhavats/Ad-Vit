# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private vulnerability reporting: open the **Security** tab of this
repository and choose **Report a vulnerability**. Or email
**support@broadmate.org** with the subject `Ad-Vit security`.

Include what you found, how to reproduce it, and the impact you expect. We aim
to acknowledge a report within 3 working days and will keep you updated until it
is fixed.

## Scope

In scope: the agent runtime, the tenant app, the operator console, the jobs
process, and the SQL under `supabase/` (RLS policies, `SECURITY DEFINER`
functions, grants).

Areas we especially care about: cross-tenant data access, privilege escalation,
audit-log tampering, bypassing the policy layer or the two write switches, and
anything that could cause an ad account to spend money unintentionally.

Out of scope: the hosted service's infrastructure (report those privately to the
address above) and findings that need a compromised operator account.

## Secrets

This repository contains only local development fixtures (dev users, local
database passwords for `127.0.0.1`). If you find something that looks like a real
credential, please report it privately as above.
