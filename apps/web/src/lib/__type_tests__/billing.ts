/**
 * A compile-time test. It is never imported and never runs.
 *
 * `@ts-expect-error` fails the build when the line below it does NOT error -
 * so if a billing method ever starts accepting a bare `string` again, or an
 * object that merely has the right shape, `tsc --noEmit` fails on the unused
 * expectation. The assertion and the thing it asserts are the same line.
 *
 * The billing client lives in its own file and reaches the runtime through
 * `runtimeCall`, which takes a path string; this file is what stops that
 * convenience from becoming a way to spell a call for a workspace nobody
 * proved.
 */

import { billing } from "@/lib/billing";
import type { AuthorizedWorkspace } from "@/lib/session";

// @ts-expect-error a bare workspace id is not proof of anything
void billing.plans("00000000-0000-4000-8000-000000000050");

// @ts-expect-error nor is an object that merely has the right shape
void billing.subscription({ id: "00000000-0000-4000-8000-000000000050" });

// @ts-expect-error the coupon route is a mutation and is held to the same line
void billing.applyCoupon("00000000-0000-4000-8000-000000000050", "DIWALI25");

// @ts-expect-error invoices are the organisation's money and need the proof too
void billing.invoices({ id: "not-a-uuid" });

// @ts-expect-error the role read is keyed by the same proof
void billing.role("00000000-0000-4000-8000-000000000050");

// @ts-expect-error where to pay and what was paid are the organisation's money too
void billing.payments("00000000-0000-4000-8000-000000000050");

// @ts-expect-error opening a payment request for a guessed workspace's invoice must not be spellable
void billing.requestPayment({ id: "00000000-0000-4000-8000-000000000050" }, "00000000-0000-4000-8000-000000000060");

// @ts-expect-error nor is recording a reference against one
void billing.submitPayment("00000000-0000-4000-8000-000000000050", "00000000-0000-4000-8000-000000000070", {
  method: "upi",
  reference: "UTR0001",
  paid_on: null,
});

// The positive case, so this file also fails if the type becomes impossible to
// satisfy - a brand nobody can construct is not safety, it is an outage.
declare const proved: AuthorizedWorkspace;
void billing.plans(proved);
void billing.subscription(proved);
void billing.applyCoupon(proved, "DIWALI25");
void billing.invoices(proved);
void billing.role(proved);
void billing.payments(proved);
void billing.requestPayment(proved, "00000000-0000-4000-8000-000000000060");
void billing.submitPayment(proved, "00000000-0000-4000-8000-000000000070", {
  method: "bank_transfer",
  reference: "UTR0001",
  paid_on: "2026-09-16",
});
