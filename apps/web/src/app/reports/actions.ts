"use server";

/**
 * The two acts on the Suggestions tab, as Server Actions.
 *
 * Next checks Origin against Host on each of these, which is what makes them
 * CSRF-safe while the session lives in an httpOnly cookie. Each one re-proves
 * the session (`defaultWorkspace` redirects to /login without one) before
 * calling the runtime, and the runtime re-proves ownership of the approval
 * row from the database on the request - so the action here contributes the
 * form parsing and the redirect, and nothing that decides.
 *
 * An approval is not a status change. The runtime REDEEMS it: the proposal
 * the owner signed runs through the tool pipeline - guardrails re-evaluated
 * against current spend, the authorisation binding checked, the read-back
 * verified - and the `?ok=` says what that came to, because "approved" and
 * "executed" are different facts and the owner is owed the second one.
 *
 * Refusals come back as `?error=` rather than as a thrown error, so the reason
 * the runtime gave - "a rejection reason is required", "not pending" - is
 * shown beside the suggestion that caused it.
 */

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";

import { NotAuthorized, Refused, RuntimeUnreachable } from "@/lib/api";
import { reports, type ApprovalResult } from "@/lib/reports";
import { defaultWorkspace } from "@/lib/session";

const PATH = "/reports";

function back(error?: string, ok?: string): never {
  const q = new URLSearchParams({ tab: "suggestions" });
  if (error) q.set("error", error);
  if (ok) q.set("ok", ok);
  revalidatePath(PATH);
  redirect(`${PATH}?${q.toString()}`);
}

const str = (fd: FormData, k: string) => String(fd.get(k) ?? "").trim();

/** What an approval came to, in the owner's terms. */
function outcome(result: ApprovalResult): string {
  const run = result.execution;
  if (!run || !run.attempted) {
    return `approved; ${run?.reason ?? "nothing executable was attached, so nothing ran"}`;
  }
  if (run.decision === "executed" && run.verified) return "approved and executed; the read-back matched";
  if (run.decision === "executed") return "approved and executed, but the read-back did not verify - check the account";
  return `approved, but execution was ${run.decision ?? "refused"}: ${run.message ?? run.reason ?? "no reason given"}`;
}

export async function approveSuggestion(formData: FormData) {
  const ws = await defaultWorkspace();
  if (!ws) back("this account is not a member of any workspace");
  const id = str(formData, "approval_id");
  if (!id) back("no approval named");

  let result: ApprovalResult;
  try {
    result = await reports.respond(id, "approve", null);
  } catch (e) {
    // Refused carries the runtime's reason; the other two carry a URL and
    // a workspace id, which the read path's <Problem> withholds and this
    // action withholds too.
    if (e instanceof Refused) back(e.detail);
    if (e instanceof NotAuthorized) back("your session has expired; sign in again");
    if (e instanceof RuntimeUnreachable) back("the agent runtime is not reachable; nothing was changed");
    back("the runtime could not record the decision");
  }
  back(undefined, outcome(result));
}

export async function rejectSuggestion(formData: FormData) {
  const ws = await defaultWorkspace();
  if (!ws) back("this account is not a member of any workspace");
  const id = str(formData, "approval_id");
  const reason = str(formData, "reason");
  if (!id) back("no approval named");
  // The runtime refuses this too (422). Checked here as well so the owner
  // sees the message without a round trip, not so the runtime can skip it.
  if (!reason) back("a rejection reason is required: it is stored as a training signal");

  try {
    await reports.respond(id, "reject", reason);
  } catch (e) {
    // Refused carries the runtime's reason; the other two carry a URL and
    // a workspace id, which the read path's <Problem> withholds and this
    // action withholds too.
    if (e instanceof Refused) back(e.detail);
    if (e instanceof NotAuthorized) back("your session has expired; sign in again");
    if (e instanceof RuntimeUnreachable) back("the agent runtime is not reachable; nothing was changed");
    back("the runtime could not record the decision");
  }
  back(undefined, "rejected; the reason is recorded as a training signal");
}
