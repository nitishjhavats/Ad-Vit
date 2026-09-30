/**
 * A compile-time test. It is never imported and never runs.
 *
 * `@ts-expect-error` fails the build when the line below it does NOT error — so
 * if `AuthorizedWorkspace` ever stops being a branded type, or a runtime method
 * starts accepting a bare `string` again, `tsc --noEmit` fails on the unused
 * expectation. The assertion and the thing it asserts are the same line.
 *
 * This is the property that replaced the `WORKSPACE_ID` constant at the top of
 * two page files: which tenant a call acts on can no longer be answered by
 * whatever string was nearest.
 */

import { api } from "@/lib/api";
import type { AuthorizedWorkspace } from "@/lib/session";

// @ts-expect-error a bare workspace id is not proof of anything
void api.connections("00000000-0000-4000-8000-000000000050");

// @ts-expect-error nor is an object that merely has the right shape
void api.approvals({ id: "00000000-0000-4000-8000-000000000050" });

// @ts-expect-error nor is one cast from the session's own user id
void api.dashboard({ id: "not-a-uuid" });

// The positive case, so this file also fails if the type becomes impossible to
// satisfy — a brand nobody can construct is not safety, it is an outage.
declare const proved: AuthorizedWorkspace;
void api.connections(proved);
void api.approvals(proved);
void api.dashboard(proved, 30);
void api.chat(proved, "Budget badha do");
