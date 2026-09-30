import type { Metadata } from "next";
import "./globals.css";
import { BrandFooter, BrandLockup } from "@/components/BrandLockup";
import { BRAND, BYLINE } from "@/lib/brand";
import { signOutAction } from "@/app/login/page";
import { currentOperator, currentUser } from "@/lib/session";

export const metadata: Metadata = {
  title: {
    default: `${BRAND.productName} · operator console`,
    template: `%s · ${BRAND.productName} console`,
  },
  description: `Operator console for ${BRAND.productName}, ${BYLINE}.`,
  applicationName: `${BRAND.productName} console`,
  // Staff surface. Search engines have no business indexing it, and a link
  // that leaks into a chat should not turn into a crawled page.
  robots: { index: false, follow: false },
};

const NAV: Array<[string, string]> = [
  ["/", "Overview"],
  ["/organisations", "Organisations"],
  ["/coupons", "Coupons"],
  ["/watch", "Platform Watch"],
  ["/invoices", "Invoices"],
  ["/payments", "Payments"],
  ["/audit", "Trail"],
];

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const user = await currentUser();
  // Resolved once per request; the pages read the same cache() entry.
  const operator = user ? await currentOperator() : null;

  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased dark:bg-slate-950 dark:text-slate-100">
        <div className="mx-auto flex min-h-screen max-w-6xl flex-col px-5 py-6 sm:px-8">
          <header className="mb-8 flex items-start justify-between gap-6 border-b border-slate-200 pb-6 dark:border-slate-800">
            <div className="flex items-start gap-4">
              <BrandLockup size="lg" />
              <span className="mt-1 rounded bg-slate-900 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-white dark:bg-slate-100 dark:text-slate-900">
                operator console
              </span>
            </div>
            <nav className="flex flex-wrap items-center justify-end gap-4 pt-1 text-sm text-slate-600 dark:text-slate-400">
              {operator &&
                NAV.map(([href, label]) => (
                  <a key={href} href={href} className="hover:text-slate-900 dark:hover:text-slate-100">
                    {label}
                  </a>
                ))}
              {user && (
                <>
                  <span className="text-slate-400 dark:text-slate-600">{user.email}</span>
                  <form action={signOutAction}>
                    <button type="submit" className="hover:text-slate-900 dark:hover:text-slate-100">
                      Sign out
                    </button>
                  </form>
                </>
              )}
            </nav>
          </header>

          <main className="flex-1">{children}</main>

          <footer className="mt-12 border-t border-slate-200 pt-5 dark:border-slate-800">
            <BrandFooter />
          </footer>
        </div>
      </body>
    </html>
  );
}
