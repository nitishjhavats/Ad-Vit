import { redirect } from "next/navigation";

import { confirmInvitation } from "@/app/auth/confirm/actions";
import { isAcceptedType } from "@/app/auth/confirm/types";
import { BrandLockup } from "@/components/BrandLockup";

export const metadata = { title: "Your invitation" };

/**
 * Where an invitation link lands. A page, not a handler, and the GET does
 * nothing but render: the one-time hash rides in hidden fields to a Server
 * Action that a person's click submits. A link pasted into a chat or a
 * mailbox is fetched first by that client's preview crawler; a GET that spent
 * the hash spent it on the crawler and left the owner with a used link and
 * no way to get another. Crawlers do not press buttons.
 */
export default async function ConfirmPage({
  searchParams,
}: {
  searchParams: Promise<{ token_hash?: string; type?: string }>;
}) {
  const { token_hash: tokenHash, type } = await searchParams;
  // A link we would not have composed goes to the same one-word refusal the
  // action gives, so the URL says nothing about which check failed.
  if (!tokenHash || !isAcceptedType(type)) redirect("/login?error=invite");

  return (
    <div className="mx-auto flex min-h-[60vh] max-w-sm flex-col justify-center space-y-6">
      <BrandLockup />
      <div className="space-y-2 text-sm text-slate-600 dark:text-slate-400">
        <p className="text-base font-medium text-slate-900 dark:text-slate-100">
          You have been invited to ad-vit.
        </p>
        <p>
          Press Continue to open your account. You will choose a password on the next screen.
          This link works once.
        </p>
      </div>
      <form action={confirmInvitation}>
        <input type="hidden" name="token_hash" value={tokenHash} />
        <input type="hidden" name="type" value={type} />
        <button
          type="submit"
          className="w-full rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900"
        >
          Continue
        </button>
      </form>
    </div>
  );
}
