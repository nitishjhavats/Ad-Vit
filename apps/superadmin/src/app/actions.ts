"use server";

/**
 * Every mutation the console makes, as a Server Action.
 *
 * Next checks Origin against Host on each of these, which is what makes them
 * CSRF-safe while the session lives in an httpOnly cookie. Each one re-proves
 * the operator (`requireOperator`) before calling the runtime, and the runtime
 * re-proves it again from the database on the request - so the action here
 * contributes the form parsing and the redirect, and nothing that decides.
 *
 * Refusals come back as a `?error=` on the page rather than as a thrown
 * error, so the reason the runtime gave - "reason_required", "wrong plan",
 * "as_of_future" - is shown beside the form that caused it.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";

import { api, Refused, type PaymentReview } from "@/lib/api";
import { requireOperator } from "@/lib/session";

function back(path: string, error?: string, ok?: string): never {
  const q = new URLSearchParams();
  if (error) q.set("error", error);
  if (ok) q.set("ok", ok);
  const qs = q.toString();
  revalidatePath(path);
  redirect(qs ? `${path}?${qs}` : path);
}

/**
 * Run one runtime call and land back on `path` with the outcome. The ok
 * sentence is usually fixed before the call; when what happened is only
 * known from the answer (a payment review says where the subscription
 * ended up), it is a function of the result.
 */
async function attempt<T>(
  path: string,
  ok: string | ((result: T) => string),
  fn: () => Promise<T>,
): Promise<never> {
  let result: T;
  try {
    result = await fn();
  } catch (e) {
    if (e instanceof Refused) back(path, e.detail);
    back(path, e instanceof Error ? e.message : String(e));
  }
  back(path, undefined, typeof ok === "function" ? ok(result) : ok);
}

const str = (fd: FormData, k: string) => String(fd.get(k) ?? "").trim();

const INVITE_COOKIE = "advit_invite_link";

/** What the organisation page shows once: the link, and whom it is for. */
export type IssuedLink = { email: string; link: string; kind: "invitation" | "sign-in" };

async function leaveLink(orgId: string, issued: IssuedLink): Promise<void> {
  // The link is a one-time credential: whoever opens it sets that person's
  // password. It must not ride the redirect as ?ok= - a query string lands
  // in browser history, the proxy's access log and the next page's Referer,
  // and the next action on the organisation page replaces it. It rides a
  // cookie the organisation page reads once and clears: httpOnly, this
  // path only, gone in ten minutes either way.
  const jar = await cookies();
  jar.set(INVITE_COOKIE, JSON.stringify(issued), {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: `/organisations/${orgId}`,
    maxAge: 600,
  });
}

// ---------------------------------------------------------------------------
// Organisations
// ---------------------------------------------------------------------------

/**
 * Onboarding's first step. Unlike the other actions this one does not land
 * back on the form: a success has a page of its own - the organisation that
 * now exists - and that is where the operator is sent, with the invitation
 * link in the flash when GoTrue issued one. Nothing sends that link; the
 * sentence beside it says so, because an operator who assumes an email went
 * out leaves a customer who cannot sign in.
 */
export async function createOrganisation(formData: FormData) {
  const op = await requireOperator();
  const slug = str(formData, "slug");
  const daily = str(formData, "daily_cap_inr");
  const monthly = str(formData, "monthly_cap_inr");
  const owner_email = str(formData, "owner_email").toLowerCase();
  const body = {
    name: str(formData, "name"),
    slug: slug || null,
    plan_key: str(formData, "plan_key"),
    industry_key: str(formData, "industry_key"),
    workspace_name: str(formData, "workspace_name"),
    owner_email,
    trial: formData.get("trial") === "on",
    ...(daily ? { daily_cap_inr: Number(daily) } : {}),
    ...(monthly ? { monthly_cap_inr: Number(monthly) } : {}),
  };

  let created;
  try {
    created = await api.createOrganisation(op, body);
  } catch (e) {
    if (e instanceof Refused) back("/organisations/new", e.detail);
    back("/organisations/new", e instanceof Error ? e.message : String(e));
  }

  if (created.action_link) {
    await leaveLink(created.org_id, { email: owner_email, link: created.action_link, kind: "invitation" });
  }
  const ok = created.action_link
    ? `${created.slug} created. ${owner_email} had no account, so one was invited - the link is below.`
    : `${created.slug} created; ${owner_email} already had an account and is its owner.`;
  back(`/organisations/${created.org_id}`, undefined, ok);
}

/**
 * A fresh sign-in link for a member whose account already exists: the
 * invitation that expired before it was sent, the owner who forgot their
 * password. The runtime issues a GoTrue recovery link scoped to a member of
 * this organisation and audits who it was for; the link comes back here,
 * into the same once-only cookie, and nowhere else.
 */
export async function issueSignInLink(formData: FormData) {
  const op = await requireOperator();
  const orgId = str(formData, "org_id");
  const userId = str(formData, "user_id");
  const path = `/organisations/${orgId}`;
  let issued;
  try {
    issued = await api.issueSignInLink(op, orgId, userId);
  } catch (e) {
    if (e instanceof Refused) back(path, e.detail);
    back(path, e instanceof Error ? e.message : String(e));
  }
  await leaveLink(orgId, { email: issued.email, link: issued.action_link, kind: "sign-in" });
  back(path, undefined, `a sign-in link for ${issued.email} is below; no email was sent`);
}

/** Read the link an action left for this page, and forget it. */
export async function takeInviteLink(): Promise<IssuedLink | null> {
  const jar = await cookies();
  const raw = jar.get(INVITE_COOKIE)?.value ?? null;
  if (!raw) return null;
  try {
    jar.delete(INVITE_COOKIE);
  } catch {
    // A Server Component render cannot write cookies; the maxAge still
    // ends it, and the page shows it once per render until then.
  }
  try {
    const parsed = JSON.parse(raw) as Partial<IssuedLink>;
    if (typeof parsed.link !== "string" || typeof parsed.email !== "string") return null;
    return { email: parsed.email, link: parsed.link, kind: parsed.kind === "sign-in" ? "sign-in" : "invitation" };
  } catch {
    // A cookie this code did not write renders nothing rather than something.
    return null;
  }
}

export async function setOrganisationStatus(formData: FormData) {
  const op = await requireOperator();
  const id = str(formData, "org_id");
  const status = str(formData, "status");
  const reason = str(formData, "reason") || null;
  return attempt(`/organisations/${id}`, `status set to ${status}`, () =>
    api.setOrganisationStatus(op, id, status, reason),
  );
}

export async function changePlan(formData: FormData) {
  const op = await requireOperator();
  const id = str(formData, "org_id");
  const plan_key = str(formData, "plan_key");
  return attempt(`/organisations/${id}`, `moved to ${plan_key}; takes effect on the next invoice`, () =>
    api.patchSubscription(op, id, { plan_key }),
  );
}

export async function setSubscriptionStatus(formData: FormData) {
  const op = await requireOperator();
  const id = str(formData, "org_id");
  const status = str(formData, "status");
  const reason = str(formData, "reason") || undefined;
  return attempt(`/organisations/${id}`, `subscription set to ${status}`, () =>
    api.patchSubscription(op, id, { status, reason }),
  );
}

export async function setOverride(formData: FormData) {
  const op = await requireOperator();
  const id = str(formData, "org_id");
  const feature_key = str(formData, "feature_key");
  const value_type = str(formData, "value_type");
  const raw = str(formData, "value");
  const reason = str(formData, "reason");
  const expires = str(formData, "expires_at");

  // Typed here so the runtime's value guard sees a number or a boolean, not
  // the string a form field produces. The guard still has the last word.
  let value: number | boolean | string = raw;
  if (value_type === "integer") value = Number(raw);
  if (value_type === "boolean") value = raw === "true";

  return attempt(`/organisations/${id}`, `override on ${feature_key} set`, () =>
    api.setOverride(op, id, feature_key, {
      value,
      reason,
      expires_at: expires ? new Date(expires).toISOString() : null,
    }),
  );
}

export async function clearOverride(formData: FormData) {
  const op = await requireOperator();
  const id = str(formData, "org_id");
  const feature_key = str(formData, "feature_key");
  return attempt(`/organisations/${id}`, `override on ${feature_key} cleared; the plan value applies`, () =>
    api.clearOverride(op, id, feature_key),
  );
}

// ---------------------------------------------------------------------------
// Coupons
// ---------------------------------------------------------------------------

export async function createCoupon(formData: FormData) {
  const op = await requireOperator();
  const plan_keys = formData.getAll("plan_keys").map(String).filter(Boolean);
  const valid_to = str(formData, "valid_to");
  const cap = str(formData, "max_redemptions");
  const code = str(formData, "code").toUpperCase();
  return attempt("/coupons", `coupon ${code} created`, () =>
    api.createCoupon(op, {
      code,
      name: str(formData, "name"),
      percent_off: Number(str(formData, "percent_off")),
      plan_keys,
      valid_to: valid_to ? new Date(valid_to).toISOString() : null,
      max_redemptions: cap ? Number(cap) : null,
    }),
  );
}

export async function setCouponActive(formData: FormData) {
  const op = await requireOperator();
  const id = str(formData, "coupon_id");
  const active = str(formData, "is_active") === "true";
  return attempt("/coupons", active ? "coupon reactivated" : "coupon deactivated", () =>
    api.patchCoupon(op, id, { is_active: active }),
  );
}

// ---------------------------------------------------------------------------
// Payments
// ---------------------------------------------------------------------------

/**
 * The one act that confirms money. The operator has matched the reference
 * the tenant quoted against the bank statement (or failed to) and says so.
 * Approving marks the invoice paid and restores the subscription to active;
 * rejecting closes the row so the organisation can request again, and the
 * runtime refuses a rejection without a reason - the form asks for one too,
 * but the runtime has the last word.
 */
export async function reviewPayment(formData: FormData) {
  const op = await requireOperator();
  const id = str(formData, "payment_id");
  const verdict = str(formData, "verdict");
  const note = str(formData, "note");
  if (verdict !== "approved" && verdict !== "rejected") {
    back("/payments", `verdict must be approved or rejected, not "${verdict}"`);
  }
  return attempt(
    "/payments",
    (r: PaymentReview) =>
      // Worded from what the runtime reported, not from the verdict sent:
      // the invoice status and the subscription status are its answer.
      `${r.payment.invoice_number ?? "invoice"}: invoice ${r.invoice_status}` +
      `${verdict === "rejected" ? ", payment rejected" : ""}; subscription ${r.subscription_status}`,
    () => api.reviewPayment(op, id, { verdict, ...(note ? { note } : {}) }),
  );
}

// ---------------------------------------------------------------------------
// Platform Watch
// ---------------------------------------------------------------------------

export async function acknowledgeFinding(formData: FormData) {
  const op = await requireOperator();
  const id = str(formData, "finding_id");
  return attempt("/watch", "acknowledged", () => api.acknowledge(op, id));
}

export async function reverifyRule(formData: FormData) {
  const op = await requireOperator();
  const code = str(formData, "code");
  return attempt("/watch", `${code} re-verified as of today (IST)`, () => api.reverifyRule(op, code));
}

export async function reverifyKnowledge(formData: FormData) {
  const op = await requireOperator();
  const id = str(formData, "knowledge_id");
  return attempt("/watch", "knowledge row re-verified as of today (IST)", () => api.reverifyKnowledge(op, id));
}
