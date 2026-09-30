import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { BrandFooter, BrandLockup } from "@/components/BrandLockup";
import { BRAND, BYLINE } from "@/lib/brand";
import { signOutAction } from "@/app/login/page";
import { currentUser } from "@/lib/session";

export const metadata: Metadata = {
  title: {
    default: BRAND.productName,
    template: `%s · ${BRAND.productName}`,
  },
  description: `${BRAND.tagline} ${BYLINE}.`,
  applicationName: BRAND.productName,
  authors: [{ name: BRAND.companyName, url: BRAND.websiteUrl }],
  creator: BRAND.companyName,
  publisher: BRAND.companyName,
};

export default async function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // Read here so the header can say who is signed in. `currentUser` is wrapped
  // in React's cache(), so the page underneath reading it again is free.
  const user = await currentUser();

  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased dark:bg-slate-950 dark:text-slate-100">
        <div className="mx-auto flex min-h-screen max-w-6xl flex-col px-5 py-6 sm:px-8">
          <header className="mb-8 flex items-start justify-between gap-6 border-b border-slate-200 pb-6 dark:border-slate-800">
            <BrandLockup size="lg" />
            <nav className="flex items-center gap-4 pt-1 text-sm text-slate-600 dark:text-slate-400">
              <Link href="/" className="hover:text-slate-900 dark:hover:text-slate-100">
                Dashboard
              </Link>
              <Link href="/chat" className="hover:text-slate-900 dark:hover:text-slate-100">
                Chat
              </Link>
              <Link href="/reports" className="hover:text-slate-900 dark:hover:text-slate-100">
                Reports
              </Link>
              <Link href="/creatives" className="hover:text-slate-900 dark:hover:text-slate-100">
                Creatives
              </Link>
              <Link href="/billing" className="hover:text-slate-900 dark:hover:text-slate-100">
                Billing
              </Link>
              <Link href="/settings" className="hover:text-slate-900 dark:hover:text-slate-100">
                Settings
              </Link>
              {user && (
                <>
                  <span className="text-slate-400 dark:text-slate-600">{user.email}</span>
                  {/* A Server Action, so Next checks Origin against Host. Sign-out
                      as a GET link would be triggerable from any page on the
                      internet with an <img> tag. */}
                  <form action={signOutAction}>
                    <button
                      type="submit"
                      className="hover:text-slate-900 dark:hover:text-slate-100"
                    >
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
