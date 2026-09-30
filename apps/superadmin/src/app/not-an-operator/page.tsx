import { redirect } from "next/navigation";

import { currentOperator, currentUser } from "@/lib/session";

export const metadata = { title: "Nothing here" };

/**
 * A signed-in account the runtime does not recognise as an operator. Worded
 * as "nothing here" rather than "you are not allowed", for the same reason
 * the runtime answers 404: the console's existence is not a fact to hand a
 * tenant who typed the hostname.
 */
export default async function NotAnOperatorPage() {
  const user = await currentUser();
  if (!user) redirect("/login");
  if (await currentOperator()) redirect("/");

  return (
    <div className="mx-auto max-w-md rounded-md border border-slate-200 bg-white p-5 text-sm dark:border-slate-800 dark:bg-slate-900">
      <p className="font-medium">Nothing here for this account</p>
      <p className="mt-1 text-slate-600 dark:text-slate-400">
        You are signed in as {user.email}, and there is nothing at this address for that account.
        The tenant dashboard is where your workspaces live.
      </p>
    </div>
  );
}
