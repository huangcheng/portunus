/**
 * Implicit-grant vendors (e.g. 百度网盘) land the operator on an address like
 * `…/login_success#expires_in=…&access_token=<TOKEN>&…`. Let them paste that whole
 * address and extract the token, so Connect does not depend on hand-trimming.
 * Anything without a non-empty `access_token=` parameter is returned trimmed as-is.
 */
const ACCESS_TOKEN_RE = /(?:^|[#?&])access_token=([^&\s#]+)/;

export function extractAccessToken(raw: string): string {
  const text = raw.trim();
  const match = ACCESS_TOKEN_RE.exec(text);
  if (!match) return text;
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

/**
 * Whether an `http` recipe is token-paste auth (Bearer / custom header). Credential-in-URL
 * and Basic recipes are not, so a `get_token_url` step makes no sense for them. Shared by
 * the Connect drawer and the catalog editor so the two cannot drift.
 */
export function httpUsesTokenFlow(spec: Record<string, unknown>): boolean {
  return (
    spec.url_is_credential !== true &&
    String(spec.auth_scheme ?? "").trim().toLowerCase() !== "basic"
  );
}
