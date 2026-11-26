/**
 * Implicit-grant vendors (e.g. 百度网盘) land the operator on an address like
 * `…/login_success#expires_in=…&access_token=<TOKEN>&…`. Let them paste that whole
 * address and take the token from it, so Connect does not depend on hand-trimming.
 */
const ACCESS_TOKEN_RE = /(?:^|[#?&])access_token=([^&\s#]+)/;

/**
 * The (percent-decoded) `access_token` value in a pasted address, or null when there is no
 * non-empty `access_token=` parameter. A bare token is NOT an address, so it also yields
 * null — callers keep the trimmed paste as the token in that case.
 */
export function parseAccessToken(raw: string): string | null {
  const match = ACCESS_TOKEN_RE.exec(raw.trim());
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

/** True when the paste contains a URL (scheme:// anywhere, or a protocol-relative `//host`). */
export function looksLikeAddress(raw: string): boolean {
  const text = raw.trim();
  return /[a-z][a-z0-9+.-]*:\/\//i.test(text) || text.startsWith("//");
}

/** `auth_scheme: basic` (case/whitespace-insensitive). The one definition for every caller. */
export function isBasicAuth(spec: Record<string, unknown>): boolean {
  return String(spec.auth_scheme ?? "").trim().toLowerCase() === "basic";
}

/**
 * Whether an `http` recipe is token-paste auth (Bearer / custom header). Credential-in-URL
 * and Basic recipes are not, so a `get_token_url` step makes no sense for them. Shared by
 * the Connect drawer and the catalog editor so the two cannot drift.
 */
export function httpUsesTokenFlow(spec: Record<string, unknown>): boolean {
  return spec.url_is_credential !== true && !isBasicAuth(spec);
}
