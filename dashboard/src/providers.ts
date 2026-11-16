/** Apps catalog types + helpers. Catalog rows live in the gateway DB (`GET /api/apps`). */

import { parseEnvRows } from "./envRows";

export const WEBDAV_MCP_PACKAGE = "webdav-mcp-server@1.0.4";
export const WEBDAV_MCP_ARGS = ["-y", "--ignore-scripts", WEBDAV_MCP_PACKAGE] as const;

export type CatalogConnection = "oauth" | "http" | "webdav" | "stdio" | "skill_bridge";

export type CatalogApp = {
  id: string;
  slug: string;
  label: string;
  name: string;
  description: string;
  connection: CatalogConnection;
  popular: boolean;
  categories: string[];
  tags: string[];
  spec: Record<string, unknown>;
  sort_order: number;
};

/** @deprecated use CatalogApp — kept as alias while panels migrate */
export type WellKnownApp = CatalogApp;
export type OAuthApp = CatalogApp & { connection: "oauth" };
export type WebDavApp = CatalogApp & { connection: "webdav" };
export type HttpApp = CatalogApp & { connection: "http" };
export type StdioApp = CatalogApp & { connection: "stdio" };
export type SkillBridgeApp = CatalogApp & { connection: "skill_bridge" };

export function isWebDavApp(app: CatalogApp): app is WebDavApp {
  return app.connection === "webdav";
}

export function isOAuthApp(app: CatalogApp): app is OAuthApp {
  return app.connection === "oauth";
}

export function isHttpApp(app: CatalogApp): app is HttpApp {
  return app.connection === "http";
}

export function isStdioApp(app: CatalogApp): app is StdioApp {
  return app.connection === "stdio";
}

export function isSkillBridgeApp(app: CatalogApp): app is SkillBridgeApp {
  return app.connection === "skill_bridge";
}

export function hasWebDavCredentials(server: { env_names: string[] } | null | undefined): boolean {
  return server?.env_names?.includes("WEBDAV_PASSWORD") ?? false;
}

/** Manifest auth.type for platform Connect (external_session is a deprecated device_qr alias). */
export function skillBridgeAuthType(app: SkillBridgeApp): string {
  const manifest = app.spec.manifest;
  if (!manifest || typeof manifest !== "object") return "";
  return String(((manifest as { auth?: { type?: unknown } }).auth || {}).type || "");
}

export function isDeviceQrAuth(app: SkillBridgeApp): boolean {
  const t = skillBridgeAuthType(app);
  return t === "device_qr" || t === "external_session";
}

export function isOauthLinkAuth(app: SkillBridgeApp): boolean {
  return skillBridgeAuthType(app) === "oauth_link";
}

export function isPlatformSkillAuth(app: SkillBridgeApp): boolean {
  return isDeviceQrAuth(app) || isOauthLinkAuth(app);
}

/** Manifest auth object for skill_bridge platform Connect. */
export function skillBridgeAuth(app: SkillBridgeApp): Record<string, unknown> {
  const manifest = app.spec.manifest;
  if (!manifest || typeof manifest !== "object") return {};
  const auth = (manifest as { auth?: unknown }).auth;
  return auth && typeof auth === "object" ? (auth as Record<string, unknown>) : {};
}

export type OauthLinkAuthOptions = {
  browserCapture: boolean;
  captureTimeoutS: number;
  pollIntervalS: number;
  tokenPaste: boolean;
  connectHint: string;
};

/** oauth_link Connect knobs from the catalog manifest (with safe defaults). */
export function oauthLinkAuthOptions(app: SkillBridgeApp): OauthLinkAuthOptions {
  const auth = skillBridgeAuth(app);
  const captureTimeout =
    typeof auth.capture_timeout_s === "number" && auth.capture_timeout_s > 0
      ? auth.capture_timeout_s
      : 90;
  const pollInterval =
    typeof auth.poll_interval_s === "number" && auth.poll_interval_s > 0
      ? auth.poll_interval_s
      : 2;
  return {
    browserCapture: auth.browser_capture === true,
    captureTimeoutS: Math.min(300, Math.max(15, captureTimeout)),
    pollIntervalS: Math.min(15, Math.max(1, pollInterval)),
    tokenPaste: auth.token_paste !== false,
    connectHint:
      typeof auth.connect_hint === "string" && auth.connect_hint.trim()
        ? auth.connect_hint.trim()
        : "Open the authorize link, confirm in the vendor app, then wait for Connect to finish.",
  };
}

/** True when every catalog secret name is present in the server vault. */
export function hasSkillBridgeSecrets(
  app: SkillBridgeApp,
  server: { env_names: string[]; has_skill_session?: boolean } | null | undefined,
): boolean {
  if (!server) return false;
  const names = parseEnvRows(app.spec.secrets).map((r) => r.name);
  const have = new Set(server.env_names ?? []);
  if (names.length && !names.every((n) => have.has(n))) return false;
  // Platform login (device_qr / oauth_link) requires SKILL_SESSION.
  if (isPlatformSkillAuth(app)) {
    return !!server.has_skill_session || have.has("SKILL_SESSION");
  }
  // Secrets all present (or none required). Server non-null already guarded above.
  return true;
}

export function specString(spec: Record<string, unknown>, key: string, fallback = ""): string {
  const v = spec[key];
  return typeof v === "string" ? v : fallback;
}

export function specBool(spec: Record<string, unknown>, key: string): boolean {
  return spec[key] === true;
}

export function specStringList(spec: Record<string, unknown>, key: string): string[] {
  const v = spec[key];
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}
