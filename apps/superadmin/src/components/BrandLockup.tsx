import Link from "next/link";
import { BRAND, BYLINE } from "@/lib/brand";

type Size = "sm" | "md" | "lg";

const PRODUCT_SIZE: Record<Size, string> = {
  sm: "text-base",
  md: "text-xl",
  lg: "text-3xl",
};

/**
 * The product lock-up.
 *
 * Three lines with a deliberate hierarchy: the product name leads, the company
 * sits underneath at a smaller size and lower contrast, and the site is a real
 * link rather than plain text.
 *
 * The by-line is a <p>, not a heading. It reads as secondary to a screen reader
 * for the same reason it looks secondary on screen - attribution is not the
 * message, and promoting it to an <h2> would announce it as a section title.
 */
export function BrandLockup({
  size = "md",
  showTagline = false,
}: {
  size?: Size;
  showTagline?: boolean;
}) {
  return (
    <div className="leading-tight">
      <p className={`${PRODUCT_SIZE[size]} font-semibold tracking-tight text-slate-900 dark:text-slate-50`}>
        {BRAND.productName}
      </p>
      <p className="mt-0.5 text-[13px] font-normal text-slate-500 dark:text-slate-400">
        {BYLINE}
      </p>
      <p className="mt-0.5 text-[13px]">
        <Link
          href={BRAND.websiteUrl}
          target="_blank"
          rel="noopener noreferrer"
          // Underlined rather than coloured: colour alone must never be the
          // only signal that something is a link (WCAG AA, PRD 16.5).
          className="text-slate-500 underline underline-offset-2 transition hover:text-slate-900 hover:decoration-2 dark:text-slate-400 dark:hover:text-slate-100"
        >
          {BRAND.websiteLabel}
        </Link>
      </p>
      {showTagline && (
        <p className="mt-2 max-w-md text-sm text-slate-600 dark:text-slate-400">
          {BRAND.tagline}
        </p>
      )}
    </div>
  );
}

/** One-line variant for a footer or an export. */
export function BrandFooter() {
  return (
    <p className="text-xs text-slate-500 dark:text-slate-400">
      {BRAND.productName} · {BYLINE} ·{" "}
      <Link
        href={BRAND.websiteUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="underline underline-offset-2 hover:text-slate-900 dark:hover:text-slate-100"
      >
        {BRAND.websiteLabel}
      </Link>
    </p>
  );
}
