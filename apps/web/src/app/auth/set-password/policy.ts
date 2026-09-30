/**
 * The password rule, in one place, so the form's `minLength` and the Server
 * Action's check cannot drift apart. Its own module because a `"use server"`
 * file may export only async functions.
 *
 * Twelve, not GoTrue's default six. The account this password protects can
 * approve ad spend on a connected Meta account; six characters is what the
 * auth server will accept, not what an owner should be allowed to choose, and
 * the check lives here because GoTrue's minimum is a cluster-wide setting
 * shared with the other products on the same auth pool.
 */
export const MIN_PASSWORD_LENGTH = 12;

/**
 * What `?error=` on the page may say, keyed by a short code. The Server
 * Action redirects with the code; the page looks the sentence up here and
 * renders nothing else. Free text in the URL would have let anyone compose
 * a link whose "error" says what they like, in our voice, on our page - and
 * would have put GoTrue's sentence, whatever it turns out to be, on screen
 * verbatim. A code that is not in this map renders no message at all.
 */
export const PASSWORD_ERRORS = {
  short: `The password must be at least ${MIN_PASSWORD_LENGTH} characters.`,
  mismatch: "The two passwords do not match.",
  refused:
    "The sign-in service did not accept that password. Choose a different one - longer, or less common - and try again.",
} as const;

export type PasswordErrorCode = keyof typeof PASSWORD_ERRORS;

export function passwordErrorText(code: string | undefined): string | null {
  if (!code) return null;
  return Object.prototype.hasOwnProperty.call(PASSWORD_ERRORS, code)
    ? PASSWORD_ERRORS[code as PasswordErrorCode]
    : null;
}
