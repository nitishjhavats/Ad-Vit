/**
 * A compile-time test. It is never imported and never runs.
 *
 * The creative studio's client inherits the rule from `lib/api.ts`: every
 * workspace-scoped call takes an `AuthorizedWorkspace`, and a bare string -
 * or an object that merely has the right shape - does not type-check.
 * `@ts-expect-error` fails the build when the line below it does NOT error, so
 * if `lib/creatives.ts` ever grows a method that accepts a string workspace,
 * `tsc --noEmit` fails on the unused expectation.
 *
 * The upload declaration is checked the same way: the content type is the
 * bucket's closed list, so a page cannot declare a `.exe` and let the runtime
 * be the first to say no.
 */

import { creatives } from "@/lib/creatives";
import type { AuthorizedWorkspace } from "@/lib/session";

// @ts-expect-error a bare workspace id is not proof of anything
void creatives.list("00000000-0000-4000-8000-000000000050");

// @ts-expect-error nor is an object that merely has the right shape
void creatives.get({ id: "00000000-0000-4000-8000-000000000050" }, "x");

// @ts-expect-error nor for the write that mints a Storage URL
void creatives.declareUpload("00000000-0000-4000-8000-000000000050", {
  original_name: "a.mp4",
  content_type: "video/mp4",
  size_bytes: 1,
  product_sku: null,
  ai_generated: null,
});

// @ts-expect-error nor for the call that spends model budget
void creatives.analyse({ id: "not-a-uuid" }, "x", "conversion");

// The positive case, so this file also fails if the type becomes impossible to
// satisfy - a brand nobody can construct is not safety, it is an outage.
declare const proved: AuthorizedWorkspace;
void creatives.list(proved);
void creatives.rubric(proved);
void creatives.get(proved, "00000000-0000-4000-8000-000000000051");
void creatives.analyse(proved, "00000000-0000-4000-8000-000000000051", "conversion");
void creatives.declareUpload(proved, {
  original_name: "a.mp4",
  content_type: "video/mp4",
  size_bytes: 1,
  product_sku: null,
  ai_generated: true,
});

// The content type is the bucket's list, closed.
void creatives.declareUpload(proved, {
  original_name: "a.exe",
  // @ts-expect-error not a type the bucket accepts
  content_type: "application/octet-stream",
  size_bytes: 1,
  product_sku: null,
  ai_generated: null,
});

// The objective is one of three; the length band depends on it.
// @ts-expect-error not an objective the rubric knows
void creatives.analyse(proved, "00000000-0000-4000-8000-000000000051", "virality");
