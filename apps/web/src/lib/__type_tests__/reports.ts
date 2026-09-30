/**
 * A compile-time test. It is never imported and never runs.
 *
 * The reports client is a second file that talks to the runtime, and the
 * property `authorized-workspace.ts` proves for `lib/api.ts` has to hold here
 * too: the report for a workspace cannot be fetched with a bare id. Each
 * `@ts-expect-error` fails the build when the line below it stops erroring.
 */

import { reports } from "@/lib/reports";
import type { AuthorizedWorkspace } from "@/lib/session";

// @ts-expect-error a bare workspace id is not proof of anything
void reports.get("00000000-0000-4000-8000-000000000050");

// @ts-expect-error nor is an object that merely has the right shape
void reports.get({ id: "00000000-0000-4000-8000-000000000050" }, 30);

// @ts-expect-error an approval response is approve or reject, never anything else
void reports.respond("7f3a", "modify", null);

// The positive cases, so this file also fails if the types become impossible
// to satisfy.
declare const proved: AuthorizedWorkspace;
void reports.get(proved);
void reports.get(proved, 90);
void reports.respond("7f3a", "approve", null);
void reports.respond("7f3a", "reject", "the budget is already at the ceiling");
