import { Suspense } from "react";
import Link from "next/link";

import { createOrganisation } from "@/app/actions";
import { api } from "@/lib/api";
import { requireOperator, type Operator } from "@/lib/session";
import { Card, Problem, Skeleton, Tag, button, input, inr } from "@/components/ui";

export const metadata = { title: "New organisation" };

/**
 * The first step of onboarding. One form, one transaction on the runtime:
 * the organisation, its subscription on the chosen plan, its first workspace
 * on the chosen industry pack, and its owner. Everything the runtime refuses
 * - a taken slug, an unknown plan, an owner with no account when it cannot
 * invite one - comes back here as the reason, beside the form.
 *
 * The plan and industry lists are the runtime's own, so a plan that stops
 * being sold or a pack still in draft cannot be chosen here by mistake.
 */

function Flash({ error }: { error?: string }) {
  if (!error) return null;
  return (
    <p className="rounded-md border border-red-300 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950/40">
      <span className="font-medium">Refused: </span>
      {error}
    </p>
  );
}

async function Form({ op }: { op: Operator }) {
  let plans, industries;
  try {
    [plans, industries] = await Promise.all([api.plans(op), api.industries(op)]);
  } catch (e) {
    return <Problem error={e} />;
  }
  // The runtime refuses `standard` for a new organisation (plan_not_for_new_signups):
  // 20260917000002 keeps it active for the subscriptions already on it. The
  // form does not offer what the route would refuse.
  const sellable = plans.filter((p) => p.is_active && p.key !== "standard");
  const defaultPlan = sellable[0]?.key ?? plans[0]?.key ?? "";
  const defaultIndustry = industries.find((i) => i.status === "active")?.key ?? "";

  return (
    <form action={createOrganisation} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Organisation name</span>
        <input name="name" required minLength={2} maxLength={120} placeholder="Herbals of Haridwar" className={input} />
      </label>
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Slug (optional; derived from the name)</span>
        <input
          name="slug"
          minLength={3}
          maxLength={64}
          pattern="[a-z0-9]+(-[a-z0-9]+)*"
          placeholder="herbals-of-haridwar"
          title="lowercase words joined by single hyphens"
          className={input}
        />
      </label>
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Plan</span>
        <select name="plan_key" defaultValue={defaultPlan} required className={input}>
          {plans.map((p) => (
            <option key={p.key} value={p.key} disabled={!p.is_active || p.key === "standard"}>
              {p.name} · {inr(p.price_inr)}/{p.billing_period} · {p.trial_days}-day trial
              {!p.is_active ? " (inactive)" : p.key === "standard" ? " (existing subscriptions only)" : ""}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Industry pack (fixed once chosen)</span>
        <select name="industry_key" defaultValue={defaultIndustry} required className={input}>
          {industries.map((i) => (
            <option key={i.key} value={i.key} disabled={i.status !== "active"}>
              {i.display_name} · {i.pack_id}
              {i.status !== "active" ? ` (${i.status})` : ""}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">First workspace name</span>
        <input name="workspace_name" required maxLength={120} placeholder="Main account" className={input} />
      </label>
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Owner email</span>
        <input name="owner_email" type="email" required maxLength={254} placeholder="owner@customer.in" className={input} />
      </label>
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Daily spend cap (INR)</span>
        <input name="daily_cap_inr" type="number" min={1} step={1} defaultValue={2000} required className={input} />
      </label>
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Monthly spend cap (INR)</span>
        <input name="monthly_cap_inr" type="number" min={1} step={1} defaultValue={50000} required className={input} />
      </label>
      <label className="flex items-center gap-2 self-end pb-2 text-sm">
        <input type="checkbox" name="trial" defaultChecked />
        Start on the plan&rsquo;s trial <Tag tone="muted">unticked: active and invoiced from today</Tag>
      </label>
      <div className="flex items-end sm:col-span-2 lg:col-span-3">
        <button type="submit" className={button}>
          Create organisation
        </button>
      </div>
    </form>
  );
}

export default async function NewOrganisationPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const op = await requireOperator();
  const { error } = await searchParams;
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">New organisation</h1>
        <p className="mt-1 text-sm text-slate-500">
          <Link href="/organisations" className="underline underline-offset-2">
            Back to organisations
          </Link>
        </p>
      </div>
      <Flash error={error} />
      <Card
        title="Onboarding, step one"
        hint="One transaction: the organisation (active), its subscription, its first workspace at L1 under these caps, and its owner. An owner with no account is invited through Supabase Auth when the runtime holds the service-role key; the invitation link is shown on the next page and nothing emails it."
      >
        <Suspense fallback={<Skeleton rows={3} />}>
          <Form op={op} />
        </Suspense>
      </Card>
    </div>
  );
}
