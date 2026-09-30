import { Suspense } from "react";

import { createCoupon, setCouponActive } from "@/app/actions";
import { api } from "@/lib/api";
import { requireOperator, type Operator } from "@/lib/session";
import { Card, Problem, Skeleton, Tag, button, buttonQuiet, input, inr, day } from "@/components/ui";

export const metadata = { title: "Coupons" };

/**
 * A coupon is a percent off, a set of plans it applies to, an optional window
 * and an optional cap. The tenant sees, in a dropdown on their plan page,
 * exactly the coupons that apply to their plan and are live - the same
 * RLS-filtered rows core.apply_coupon accepts, so the two cannot disagree.
 *
 * The code and the percentage are not editable after creation: a coupon
 * redeemed at 25% must not later read 40% on the invoice that names it.
 * Deactivate and make a new one.
 */

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

async function Create({ op }: { op: Operator }) {
  let plans;
  try {
    plans = await api.plans(op);
  } catch (e) {
    return <Problem error={e} />;
  }
  return (
    <form action={createCoupon} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Code</span>
        <input name="code" required minLength={3} maxLength={32} pattern="[A-Za-z0-9_-]+" placeholder="DIWALI25" className={`${input} uppercase`} />
      </label>
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Name (shown to the tenant)</span>
        <input name="name" required maxLength={120} placeholder="Diwali launch offer" className={input} />
      </label>
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Percent off</span>
        <input name="percent_off" type="number" required min={0.01} max={100} step={0.01} className={input} />
      </label>
      <fieldset className="text-sm sm:col-span-2 lg:col-span-3">
        <legend className="text-slate-600 dark:text-slate-400">Applies to plans</legend>
        <div className="mt-1 flex flex-wrap gap-3">
          {plans.map((p) => (
            <label key={p.key} className="flex items-center gap-1.5 rounded-md border border-slate-300 px-2 py-1 dark:border-slate-700">
              <input type="checkbox" name="plan_keys" value={p.key} defaultChecked={p.is_active && p.key !== "standard"} />
              {p.name} <span className="text-xs text-slate-500">{inr(p.price_inr)}</span>
              {!p.is_active && <Tag tone="muted">inactive</Tag>}
            </label>
          ))}
        </div>
      </fieldset>
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Valid until (optional)</span>
        <input name="valid_to" type="date" className={input} />
      </label>
      <label className="text-sm">
        <span className="text-slate-600 dark:text-slate-400">Max redemptions (optional)</span>
        <input name="max_redemptions" type="number" min={1} className={input} />
      </label>
      <div className="flex items-end">
        <button type="submit" className={button}>
          Create coupon
        </button>
      </div>
    </form>
  );
}

async function List({ op }: { op: Operator }) {
  let coupons;
  try {
    coupons = await api.coupons(op);
  } catch (e) {
    return <Problem error={e} />;
  }
  if (coupons.length === 0) return <p className="text-sm text-slate-500">No coupons yet.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="py-2 pr-3">Code</th>
            <th className="py-2 pr-3">Off</th>
            <th className="py-2 pr-3">Plans</th>
            <th className="py-2 pr-3">Window</th>
            <th className="py-2 pr-3">Redeemed</th>
            <th className="py-2 pr-3">State</th>
            <th className="py-2 pr-3"></th>
          </tr>
        </thead>
        <tbody>
          {coupons.map((c) => {
            // "live" is decided by the runtime with the same predicate the
            // tenant's dropdown uses, not re-derived here against this clock.
            const exhausted = c.max_redemptions !== null && c.redemptions >= c.max_redemptions;
            const live = c.is_live;
            return (
              <tr key={c.id} className="border-t border-slate-200 dark:border-slate-800">
                <td className="py-2 pr-3">
                  <p className="font-mono font-medium">{c.code}</p>
                  <p className="text-xs text-slate-500">{c.name}</p>
                </td>
                <td className="py-2 pr-3 tabular-nums">{Number(c.percent_off)}%</td>
                <td className="py-2 pr-3">{c.plan_keys.join(", ")}</td>
                <td className="py-2 pr-3 text-xs">
                  {day(c.valid_from)} → {c.valid_to ? day(c.valid_to) : "open"}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {c.redemptions}
                  {c.max_redemptions !== null ? ` / ${c.max_redemptions}` : ""}
                </td>
                <td className="py-2 pr-3">
                  {live ? (
                    <Tag tone="ok">live</Tag>
                  ) : !c.is_active ? (
                    <Tag tone="muted">inactive</Tag>
                  ) : exhausted ? (
                    <Tag tone="warn">exhausted</Tag>
                  ) : (
                    <Tag tone="warn">outside its window</Tag>
                  )}
                </td>
                <td className="py-2 pr-3">
                  <form action={setCouponActive}>
                    <input type="hidden" name="coupon_id" value={c.id} />
                    <input type="hidden" name="is_active" value={c.is_active ? "false" : "true"} />
                    <button type="submit" className={buttonQuiet}>
                      {c.is_active ? "Deactivate" : "Reactivate"}
                    </button>
                  </form>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default async function CouponsPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; ok?: string }>;
}) {
  const op = await requireOperator();
  const { error, ok } = await searchParams;
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Coupons</h1>
      <Flash error={error} ok={ok} />
      <Card
        title="New coupon"
        hint="Tenants on the chosen plans see it in a dropdown and apply it themselves. Refusals (expired, exhausted, wrong plan) are decided in SQL by core.apply_coupon."
      >
        <Suspense fallback={<Skeleton />}>
          <Create op={op} />
        </Suspense>
      </Card>
      <Card title="All coupons" hint="Code and percentage are fixed once created; deactivate and make a new one.">
        <Suspense fallback={<Skeleton rows={3} />}>
          <List op={op} />
        </Suspense>
      </Card>
    </div>
  );
}
