/**
 * A compile-time test for the settings client. It is never imported and never
 * runs.
 *
 * `@ts-expect-error` fails the build when the line below it does NOT error -
 * so if a settings method ever starts accepting a bare `string` for the
 * workspace, `tsc --noEmit` fails on the unused expectation. The write-side
 * methods are the ones that matter most: a key stored against a guessed
 * workspace id, or a CTA asserted for one, is precisely the confused deputy
 * the branded type exists to make unspellable.
 */

import { settings } from "@/lib/settings";
import type { AuthorizedWorkspace } from "@/lib/session";

// @ts-expect-error a bare workspace id is not proof of anything
void settings.byok("00000000-0000-4000-8000-000000000050");

// @ts-expect-error nor is it for the write that stores a key
void settings.storeKey("00000000-0000-4000-8000-000000000050", "sk-or-v1-0123456789abcdef");

// @ts-expect-error nor is an object that merely has the right shape
void settings.models({ id: "00000000-0000-4000-8000-000000000050" });

// @ts-expect-error nor for choosing a tier
void settings.chooseTier({ id: "00000000-0000-4000-8000-000000000050" }, "strategy", "cheap");

// @ts-expect-error nor for asserting where campaigns send people
void settings.chooseCta("00000000-0000-4000-8000-000000000050", "click_to_call", null);

// @ts-expect-error nor for reading what is on file
void settings.ctaOnFile({ id: "not-a-uuid" });

// The positive case, so this file also fails if the type becomes impossible to
// satisfy - a brand nobody can construct is not safety, it is an outage.
declare const proved: AuthorizedWorkspace;
void settings.byok(proved);
void settings.storeKey(proved, "sk-or-v1-0123456789abcdef");
void settings.models(proved);
void settings.chooseTier(proved, "strategy", "cheap");
void settings.chooseCta(proved, "click_to_call", "the sales team answers the phone");
void settings.ctaOnFile(proved);
