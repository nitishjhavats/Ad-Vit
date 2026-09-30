import { Suspense } from "react";

import {
  changePlan,
  clearOverride,
  issueSignInLink,
  setOrganisationStatus,
  setOverride,
  setSubscriptionStatus,
  takeInviteLink,
} from "@/app/actions";
import { api, type Entitlement } from "@/lib/api";
import { requireOperator, type Operator } from "@/lib/session";
import { Card, Problem, Skeleton, Tag, button, buttonQuiet, input, inr, day, when } from "@/components/ui";

export const metadata = { title: "Organisation" };

function Flash({ error, ok }: { error?: string; ok?: string }) {
  if (error) {
    return (
      <p className="rounded-md border border-red-300 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950/40">
        <span className="font-medium">Refused: </span>
        {error}
      </p>
    );
  }
  if (ok) {
    return (
      <p className="rounded-md border border-emerald-300 bg-emerald-50 p-3 text-sm dark:border-emerald-900 dark:bg-emerald-950/40">
        {ok}
      </p>
    );
  }
  return null;
}

function OverrideRow({ e, orgId }: { e: Entitlement; orgId: string }) {
  const shown = typeof e.value === "boolean" ? (e.value ? "yes" : "no") : String(e.value);
  return (
    <tr className="border-t border-slate-200 align-top dark:border-slate-800">
      <td className="py-2 pr-3">
        <p className="font-medium">{e.name}</p>
        <p className="text-xs text-slate-500">{e.feature_key}</p>
      </td>
      <td className="py-2 pr-3 tabular-nums">{shown}</td>
      <td className="py-2 pr-3">
        <Tag tone={e.source === "override" ? "warn" : "muted"}>{e.source}</Tag>
        {e.source === "override" && (
          <p className="mt-1 max-w-xs text-xs text-slate-500">
            {e.reason}
            {e.expires_at ? ` · until ${day(e.expires_at)}` : ""}
          </p>
        )}
      </td>
      <td className="py-2 pr-3">
        <form action={setOverride} className="flex flex-wrap items-end gap-2">
          <input type="hidden" name="org_id" value={orgId} />
          <input type="hidden" name="feature_key" value={e.feature_key} />
          <input type="hidden" name="value_type" value={e.value_type} />
          {e.value_type === "boolean" ? (
            <select name="value" defaultValue={String(e.value)} className={`${input} mt-0 w-24`}>
              <option value="true">yes</option>
              <option value="false">no</option>
            </select>
          ) : (
            <input
              name="value"
              type={e.value_type === "integer" ? "number" : "text"}
              defaultValue={shown}
              required
              className={`${input} mt-0 w-28`}
            />
          )}
          <input
            name="reason"
            placeholder="reason (required)"
            required
            minLength={3}
            className={`${input} mt-0 w-56`}
          />
          <input name="expires_at" type="date" className={`${input} mt-0 w-40`} title="expires (optional)" />
          <button type="submit" className={button}>
            Set override
          </button>
        </form>
        {e.source === "override" && (
          <form action={clearOverride} className="mt-2">
            <input type="hidden" name="org_id" value={orgId} />
            <input type="hidden" name="feature_key" value={e.feature_key} />
            <button type="submit" className={buttonQuiet}>
              Clear override
            </button>
          </form>
        )}
      </td>
    </tr>
  );
}

async function Detail({ op, id, error, ok }: { op: Operator; id: string; error?: string; ok?: string }) {
  let org, plans;
  try {
    [org, plans] = await Promise.all([api.organisation(op, id), api.plans(op)]);
  } catch (e) {
    return <Problem error={e} />;
  }
  // Left by createOrganisation when GoTrue issued an invitation, or by
  // issueSignInLink for an existing member. Shown here, once, as text the
  // operator copies; nothing has emailed it.
  const issued = await takeInviteLink();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{org.name}</h1>
        <p className="mt-1 text-sm text-slate-500">
          {org.slug} · created {day(org.created_at)} · {org.legal_name ?? "no legal name"} ·{" "}
          {org.gstin ?? "no GSTIN"} · state {org.state_code ?? "—"} · {org.billing_email ?? "no billing email"}
        </p>
      </div>

      <Flash error={error} ok={ok} />
      {issued && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-900 dark:bg-amber-950/40">
          <p className="font-medium">
            {issued.kind === "invitation" ? "Invitation link" : "Sign-in link"} for {issued.email}
          </p>
          <p className="mt-1 text-slate-700 dark:text-slate-300">
            Send this to them yourself; no email was sent. It opens a page on the tenant app with
            one Continue button; whoever presses it is signed in and sets the password - so treat
            the link as a password. It works once and expires in about a day. This panel shows it
            for ten minutes, then it is gone from here; if it expires unused, issue a new one from
            the Members card below.
          </p>
          <input
            readOnly
            value={issued.link}
            className="mt-2 w-full rounded-md border border-slate-300 bg-white px-2 py-1 font-mono text-xs dark:border-slate-700 dark:bg-slate-900"
          />
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Organisation status" hint="The verdict core.access_mode reads. Only an operator can move it, and a suspension carries its reason into the trail.">
          <p className="mb-3 text-sm">
            <Tag tone={org.status === "active" ? "ok" : org.status === "suspended" ? "bad" : "warn"}>
              {org.status.replace("_", " ")}
            </Tag>
            {org.suspension_reason && (
              <span className="ml-2 text-slate-600 dark:text-slate-400">
                — {org.suspension_reason} ({when(org.suspended_at)})
              </span>
            )}
            {org.status === "active" && org.activated_at && (
              <span className="ml-2 text-xs text-slate-500">since {day(org.activated_at)}</span>
            )}
          </p>
          <form action={setOrganisationStatus} className="flex flex-wrap items-end gap-2">
            <input type="hidden" name="org_id" value={org.id} />
            <label className="text-sm">
              <span className="text-slate-600 dark:text-slate-400">Set to</span>
              <select name="status" defaultValue={org.status === "active" ? "suspended" : "active"} className={`${input} w-44`}>
                <option value="active">active</option>
                <option value="suspended">suspended</option>
                <option value="pending_activation">pending activation</option>
              </select>
            </label>
            <label className="grow text-sm">
              <span className="text-slate-600 dark:text-slate-400">Reason (required to suspend)</span>
              <input name="reason" className={input} />
            </label>
            <button type="submit" className={button}>
              Apply
            </button>
          </form>
        </Card>

        <Card title="Subscription" hint="A plan change takes effect on the next invoice; the one already issued is a snapshot.">
          <p className="mb-3 text-sm">
            <span className="font-medium">{org.plan_name ?? "no plan"}</span>{" "}
            {org.subscription_status && <Tag tone="muted">{org.subscription_status.replace("_", " ")}</Tag>}
            {org.coupon_code && <span className="ml-2 text-xs text-slate-500">coupon {org.coupon_code}</span>}
            {org.current_period_end && (
              <span className="ml-2 text-xs text-slate-500">period ends {day(org.current_period_end)}</span>
            )}
          </p>
          <form action={changePlan} className="mb-3 flex flex-wrap items-end gap-2">
            <input type="hidden" name="org_id" value={org.id} />
            <label className="text-sm">
              <span className="text-slate-600 dark:text-slate-400">Move to plan</span>
              <select name="plan_key" defaultValue={org.plan_key ?? ""} className={`${input} w-56`}>
                {plans.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.name} · {inr(p.price_inr)}/{p.billing_period}
                    {p.is_active ? "" : " (inactive)"}
                  </option>
                ))}
              </select>
            </label>
            <button type="submit" className={button}>
              Change plan
            </button>
          </form>
          <form action={setSubscriptionStatus} className="flex flex-wrap items-end gap-2">
            <input type="hidden" name="org_id" value={org.id} />
            <label className="text-sm">
              <span className="text-slate-600 dark:text-slate-400">Subscription state</span>
              <select name="status" defaultValue={org.subscription_status ?? "active"} className={`${input} w-44`}>
                {["trialing", "pending_payment", "active", "past_due", "grace", "expired", "suspended"].map((s) => (
                  <option key={s} value={s}>
                    {s.replace("_", " ")}
                  </option>
                ))}
              </select>
            </label>
            <label className="grow text-sm">
              <span className="text-slate-600 dark:text-slate-400">Reason (required when narrowing access)</span>
              <input name="reason" className={input} />
            </label>
            <button type="submit" className={buttonQuiet}>
              Set state
            </button>
          </form>
        </Card>
      </div>

      <Card
        title="Entitlements"
        hint="Resolved override → plan → default, by core.org_entitlements, the same function the tenant's plan page reads. An override needs a reason; who set it is written from the session."
      >
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="py-2 pr-3">Feature</th>
                <th className="py-2 pr-3">Value</th>
                <th className="py-2 pr-3">Source</th>
                <th className="py-2 pr-3">Override</th>
              </tr>
            </thead>
            <tbody>
              {org.entitlements.map((e) => (
                <OverrideRow key={e.feature_key} e={e} orgId={org.id} />
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Members">
          <ul className="space-y-1 text-sm">
            {org.members.map((m) => (
              <li key={m.user_id} className="flex items-center justify-between gap-3">
                <span>
                  {m.full_name ?? m.email} <span className="text-xs text-slate-500">{m.email}</span>
                </span>
                <span className="flex items-center gap-2">
                  <Tag tone="muted">{m.role}</Tag>
                  {/* A fresh one-time link for an account that exists: the
                      invitation that expired unsent, the forgotten password.
                      There is no email in this product, so this button is the
                      whole "forgot password" path, and it is audited. */}
                  <form action={issueSignInLink}>
                    <input type="hidden" name="org_id" value={org.id} />
                    <input type="hidden" name="user_id" value={m.user_id} />
                    <button type="submit" className={buttonQuiet} title="Issue a one-time sign-in link for this member; no email is sent">
                      Sign-in link
                    </button>
                  </form>
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-slate-500">
            A sign-in link signs its holder in once and asks them to choose a password. Use it for
            an invitation that expired before it was sent, or a member who has forgotten their
            password. Each one is written to the audit trail.
          </p>
        </Card>
        <Card title="Workspaces">
          <ul className="space-y-2 text-sm">
            {org.workspaces.map((w) => (
              <li key={w.id} className="rounded-md border border-slate-200 p-2 dark:border-slate-800">
                <p className="font-medium">
                  {w.name} <span className="text-xs text-slate-500">{w.industry_key}</span>
                  {w.is_paused && <Tag tone="warn">paused</Tag>}
                </p>
                <p className="text-xs text-slate-500">
                  L{w.autonomy_level} · caps {inr(w.daily_cap_inr)}/day, {inr(w.monthly_cap_inr)}/month ·{" "}
                  {w.ad_accounts.length === 0
                    ? "no ad account"
                    : w.ad_accounts.map((a) => `${a.ad_account_id} (${a.health}${a.write_enabled ? ", writable" : ""})`).join(", ")}
                </p>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <Card title="Invoices" action={<a href={`/audit?org=${org.id}`} className="text-sm underline underline-offset-2">This organisation&rsquo;s trail</a>}>
        {org.invoices.length === 0 ? (
          <p className="text-sm text-slate-500">None yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="py-2 pr-3">Number</th>
                <th className="py-2 pr-3">Period</th>
                <th className="py-2 pr-3">Total</th>
                <th className="py-2 pr-3">GST</th>
                <th className="py-2 pr-3">Status</th>
              </tr>
            </thead>
            <tbody>
              {org.invoices.map((i) => (
                <tr key={i.id} className="border-t border-slate-200 dark:border-slate-800">
                  <td className="py-2 pr-3 font-mono text-xs">{i.number ?? "—"}</td>
                  <td className="py-2 pr-3">
                    {i.period_start} → {i.period_end}
                  </td>
                  <td className="py-2 pr-3 tabular-nums">{inr(i.total_inr)}</td>
                  <td className="py-2 pr-3">{i.gst_split === "cgst_sgst" ? "CGST+SGST" : "IGST"}</td>
                  <td className="py-2 pr-3">
                    <Tag tone={i.status === "paid" ? "ok" : i.status === "draft" ? "warn" : "muted"}>{i.status}</Tag>
                    {i.draft_reason && <p className="text-xs text-slate-500">{i.draft_reason}</p>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

export default async function OrganisationPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ error?: string; ok?: string }>;
}) {
  const op = await requireOperator();
  const { id } = await params;
  const { error, ok } = await searchParams;
  return (
    <Suspense fallback={<Skeleton rows={6} />}>
      <Detail op={op} id={id} error={error} ok={ok} />
    </Suspense>
  );
}
