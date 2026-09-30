/**
 * Product and company identity.
 *
 * Mirrors apps/agent-runtime/app/branding.py. The two exist because the API and
 * the UI are different runtimes, but they are checked against each other: the
 * web app fetches /api/brand on the server and a mismatch is a build-visible
 * error rather than a silent divergence between what the dashboard says and
 * what a PDF footer says.
 *
 * The lock-up is deliberate:
 *
 *     ad-vit
 *     by Broadmate Global      <- smaller, secondary
 *     broadmate.org            <- linked
 *
 * The product name leads because that is what the user is looking at. The
 * company sits underneath, quieter, because attribution is not the message.
 */

export const BRAND = {
  productName: "ad-vit",
  companyName: "Broadmate Global",
  websiteUrl: "https://broadmate.org",
  websiteLabel: "broadmate.org",
  tagline: "Agentic Meta advertising, closed on business truth.",
} as const;

export const BYLINE = `by ${BRAND.companyName}` as const;

export const FOOTER =
  `${BRAND.productName} · ${BYLINE} · ${BRAND.websiteUrl}` as const;

/** Shape returned by GET /api/brand on the agent runtime. */
export type BrandPayload = {
  product_name: string;
  company_name: string;
  byline: string;
  website_url: string;
  website_label: string;
  tagline: string;
};

/**
 * Assert the runtime agrees with this file.
 *
 * Called from the layout at request time. A drift here means a report footer
 * and the dashboard header are claiming different companies, which is exactly
 * the kind of thing a client notices in a meeting.
 */
export function assertBrandMatches(payload: BrandPayload): string | null {
  if (payload.product_name !== BRAND.productName) {
    return `product name drift: API says "${payload.product_name}", UI says "${BRAND.productName}"`;
  }
  if (payload.company_name !== BRAND.companyName) {
    return `company name drift: API says "${payload.company_name}", UI says "${BRAND.companyName}"`;
  }
  if (payload.website_url.replace(/\/$/, "") !== BRAND.websiteUrl) {
    return `website drift: API says "${payload.website_url}", UI says "${BRAND.websiteUrl}"`;
  }
  return null;
}
