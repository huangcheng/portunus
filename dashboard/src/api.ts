// Thin fetch wrapper: session cookie is sent automatically; CSRF token is kept in memory only.
// Step-up password re-prompt is only for account security (2FA / recovery / passkeys), via in-app modal.

import { confirmPassword } from "./ConfirmPassword";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public data: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let csrf = "";
export const setCsrf = (t: string) => {
  csrf = t;
};

type ErrorBody = {
  code?: unknown;
  message?: unknown;
  errors?: unknown;
};

async function stepUpHeaders(password?: string): Promise<Record<string, string>> {
  const pw =
    password ??
    (await confirmPassword({
      title: "Confirm password",
      message: "Re-enter your operator password to continue.",
    }));
  if (!pw) {
    throw new ApiError(401, "cancelled", "step-up cancelled");
  }
  const { step_up_token } = await call<{ step_up_token: string }>("POST", "/auth/step-up", {
    password: pw,
  });
  return { "X-Step-Up-Token": step_up_token };
}

async function call<T>(
  method: string,
  path: string,
  body?: unknown,
  extraHeaders?: Record<string, string>,
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method,
      credentials: "same-origin",
      signal: AbortSignal.timeout(60_000),
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(method !== "GET" && method !== "HEAD"
          ? { "X-CSRF-Token": csrf }
          : {}),
        ...extraHeaders,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    const aborted = err instanceof DOMException && err.name === "TimeoutError";
    throw new ApiError(
      0,
      aborted ? "timeout" : "network_error",
      aborted ? "request timed out" : "network request failed",
    );
  }
  if (res.status === 204) return undefined as T;
  const raw: unknown = await res.json().catch(() => ({}));
  const data: ErrorBody =
    raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as ErrorBody) : {};
  if (!res.ok) {
    const detail = Array.isArray(data.errors)
      ? data.errors
          .map((e) => {
            if (typeof e === "string") return e;
            if (e && typeof e === "object" && Array.isArray((e as { loc?: unknown }).loc)) {
              return ((e as { loc: unknown[] }).loc).map(String).join(".");
            }
            return "";
          })
          .filter(Boolean)
          .join(", ")
      : "";
    // Only an explicit JSON `code: "unauthorized"` means the operator cookie died.
    // Skip public auth forms and step-up (wrong password while the session is alive).
    const publicAuth = path.startsWith("/auth/");
    if (res.status === 401 && !publicAuth && data.code === "unauthorized") {
      csrf = "";
      window.dispatchEvent(new Event("portunus-unauthorized"));
    }
    throw new ApiError(
      res.status,
      typeof data.code === "string" ? data.code : "error",
      typeof data.message === "string"
        ? data.message
        : detail
          ? `invalid: ${detail}`
          : res.statusText,
      data as Record<string, unknown>,
    );
  }
  return raw as T;
}

export type OAuthConfig = {
  authorize_url: string;
  token_url: string;
  client_id: string;
  scopes: string[];
  resource?: string | null;
  issuer?: string | null;
  registration_endpoint?: string | null;
  token_endpoint_auth_method?: string | null;
  use_cimd?: boolean;
  allow_bearer_token?: boolean;
  configured: boolean;
};

export type CatalogAppRow = {
  id: string;
  slug: string;
  label: string;
  name: string;
  description: string;
  connection: "oauth" | "http" | "webdav" | "stdio" | "skill_bridge";
  popular: boolean;
  categories: string[];
  tags: string[];
  spec: Record<string, unknown>;
  sort_order: number;
};

export type OAuthBody = {
  authorize_url: string;
  token_url: string;
  client_id?: string;
  scopes: string[];
  resource?: string;
  /** RFC 9207 issuer identifier; enables exact `iss` validation on the callback. */
  issuer?: string;
  registration_endpoint?: string;
  token_endpoint_auth_method?: string;
  /** Prefer CIMD over DCR when the AS advertises client_id_metadata_document_supported. */
  use_cimd?: boolean;
};

export type UpdateServerBody = {
  name?: string;
  /** Custom servers only (recipe_slug null). */
  slug?: string;
  command?: string;
  args?: string[];
  preferred_regions?: string[];
  oauth?: OAuthBody | null;
  oauth_client_secret?: string | null;
  /** Vault upsert: stdio env names, or remote request headers (e.g. WyHeader). */
  env?: Record<string, string>;
  /** Remote MCP endpoint URL (self-hosted apps like WeKnora). */
  url?: string;
};

export type ServerStatus = "probing" | "ok" | "auth_required" | "unreachable";

export type Server = {
  id: string;
  name: string;
  slug: string;
  kind: string;
  enabled: boolean;
  command: string | null;
  args: string[] | null;
  url: string | null;
  oauth: OAuthConfig | null;
  has_auth: boolean;
  has_skill_session?: boolean;
  preferred_regions: string[];
  /** Set when connected from the catalog; null for custom MCPs. */
  recipe_slug?: string | null;
  env_names: string[];
  status?: ServerStatus;
  tool_count?: number | null;
  last_probe_at?: string | null;
  last_ok_at?: string | null;
  last_error_code?: string | null;
  last_error_at?: string | null;
};

export type SkillAuthResult = {
  action: string;
  cli_action?: string;
  exit_code: number | null;
  result: Record<string, unknown> | null;
  stderr: string | null;
  qr_png_base64: string | null;
  auth_url?: string | null;
  job_id?: string | null;
  pending?: boolean;
  logged_in: boolean;
  has_skill_session: boolean;
};
export type Tool = {
  id: string;
  server_id?: string;
  name: string;
  description: string | null;
  schema?: Record<string, unknown> | null;
  schema_hash?: string;
  schema_overridden?: boolean;
  schema_override?: Record<string, unknown> | null;
  probed_schema?: Record<string, unknown> | null;
  enabled: boolean;
};
export type Key = {
  key_id: string;
  label: string | null;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  has_secret: boolean;
  server_ids: string[];
};

export type CreateServerBody = {
  name: string;
  slug: string;
  kind: "stdio" | "remote";
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  preferred_regions?: string[];
  oauth?: OAuthBody | null;
  oauth_client_secret?: string | null;
  /** Catalog Connect only — freezes slug/argv to the recipe. */
  recipe_slug?: string;
};

export type FleetResponse = {
  gateway: {
    version: string;
    started_at: string;
    db_backend: string;
    otel_export: boolean;
    fingerprints?: Record<string, string>;
    trace_url_template?: string | null;
  };
  runners: Array<{
    site_id: string;
    regions: string[];
    egress_class?: string | null;
    capacity?: number | null;
    connected: boolean;
    connected_since: string | null;
    last_heartbeat_at: string | null;
    last_disconnected_at: string | null;
    reconnects: number;
    children: number;
    fingerprints?: Record<string, string> | null;
    drift?: string[] | null;
  }>;
  children: Array<{
    child_id: string;
    server: { id: string | null; slug: string | null };
    key_id: string | null;
    site_id: string;
    state: string;
    preferred_miss: boolean;
    pending_stop: boolean;
  }>;
};

export type AuditEvent = {
  id: string;
  created_at: string | null;
  actor: string;
  action: string;
  target: string | null;
  detail: Record<string, unknown> | null;
};

export type ToolCallRow = {
  id: string;
  created_at: string | null;
  trace_id: string;
  key_id: string;
  server_slug: string;
  tool: string;
  outcome: string;
  site_id: string | null;
  cold_start: boolean | null;
  preferred_miss: boolean | null;
  gateway_ms: number | null;
  start_ms: number | null;
  queue_ms: number | null;
  init_ms: number | null;
  upstream_ms: number | null;
  result_bytes: number | null;
};

export type TemporalStatus = {
  enabled: boolean;
  host?: string;
  namespace?: string;
  task_queue?: string;
  error?: string;
};

export type TemporalWorkflowSummary = {
  workflow_id: string | null;
  run_id: string | null;
  workflow_type: string | null;
  task_queue: string | null;
  status: string | null;
  start_time: string | null;
  close_time: string | null;
  execution_time: string | null;
  history_length: number | null;
};

export type TemporalWorkflowEvent = {
  event_id: number | null;
  event_type: string | null;
  summary?: string | null;
  event_time: string | null;
};

export type TemporalWorkflowTask = {
  scheduled_event_id: number | null;
  activity_id: string | null;
  name: string;
  status: string;
  attempt: number | null;
  input: string | null;
  error: string | null;
  scheduled_at: string | null;
  started_at: string | null;
  completed_at: string | null;
};

export type TemporalWorkflowDetail = {
  enabled: boolean;
  workflow: TemporalWorkflowSummary;
  query_status: string | null;
  /** Humanized current phase from the workflow status query. */
  phase?: string | null;
  stats?: Record<string, unknown> | null;
  tasks?: TemporalWorkflowTask[];
  events: TemporalWorkflowEvent[];
  history_truncated?: boolean;
  failure?: string | null;
  error?: string;
};

export type TemporalWorkflowList = {
  enabled: boolean;
  items: TemporalWorkflowSummary[];
  next_page_token: string | null;
  query?: string;
  error?: string;
};

export type AuthStatus = {
  configured: boolean;
  username?: string;
  has_password: boolean;
  has_totp: boolean;
  has_passkey: boolean;
  turnstile_sitekey?: string;
  bootstrap_required?: boolean;
};

export type RecoveryStatus = { total: number; remaining: number };

export type PasskeyInfo = {
  credential_id: string;
  name: string;
  transports: string[];
  created_at: string | null;
  last_used_at: string | null;
  sign_count: number;
};

export type PasskeyCreationOptions = {
  challenge: string;
  rp: { name: string; id: string };
  user: { id: string; name: string; displayName: string };
  pubKeyCredParams: { type: string; alg: number }[];
  timeout: number;
  attestation?: string;
  authenticatorSelection?: AuthenticatorSelectionCriteria;
};

export type PasskeyAssertionOptions = {
  challenge: string;
  rpId: string;
  allowCredentials: { id: string; type?: string }[];
  timeout: number;
  userVerification?: string;
};

export const api = {
  apps: () => call<CatalogAppRow[]>("GET", "/apps"),
  createApp: (b: Omit<CatalogAppRow, "id">) => call<CatalogAppRow>("POST", "/apps", b),
  updateApp: (id: string, b: Partial<CatalogAppRow>) =>
    call<CatalogAppRow>("PATCH", `/apps/${encodeURIComponent(id)}`, b),
  deleteApp: (id: string) => call<void>("DELETE", `/apps/${encodeURIComponent(id)}`),
  authStatus: () => call<AuthStatus>("GET", "/auth/status"),
  setup: async (body: { username: string; password: string; bootstrap_password?: string }) => {
    const r = await call<{ csrf_token: string; username: string }>("POST", "/auth/setup", body);
    setCsrf(r.csrf_token);
    return r;
  },
  login: async (body: { username?: string; password: string; turnstile_token?: string }) => {
    const r = await call<{ csrf_token: string; username?: string }>("POST", "/auth/login", body);
    setCsrf(r.csrf_token);
    return r;
  },
  loginTotp: async (challenge_token: string, code: string) => {
    const r = await call<{ csrf_token: string; username?: string; recovery_remaining?: number }>(
      "POST",
      "/auth/login/totp",
      { challenge_token, code },
    );
    setCsrf(r.csrf_token);
    return r;
  },
  passkeyAssertionOptions: () =>
    call<PasskeyAssertionOptions>("POST", "/auth/login/passkey/assertion/options", {}),
  passkeyLogin: async (body: {
    id: string;
    challenge: string;
    response: { clientDataJSON: string; authenticatorData: string; signature: string };
  }) => {
    const r = await call<{ csrf_token: string; username?: string }>("POST", "/auth/login/passkey/verify", body);
    setCsrf(r.csrf_token);
    return r;
  },
  session: async () => {
    const r = await call<{ csrf_token: string; username?: string }>("GET", "/auth/session");
    setCsrf(r.csrf_token);
    return r;
  },
  changePassword: (current: string, next: string) =>
    call<{ ok: boolean }>("POST", "/auth/password", { current, next }),
  stepUp: (password: string) =>
    call<{ step_up_token: string }>("POST", "/auth/step-up", { password }),
  totpEnable: async (password?: string) => {
    const headers = await stepUpHeaders(password);
    return call<{ secret: string; provisioning_uri: string }>(
      "POST",
      "/auth/totp/enable",
      {},
      headers,
    );
  },
  totpConfirm: async (secret: string, code: string, password?: string) => {
    const headers = await stepUpHeaders(password);
    return call<{ ok: boolean; codes: string[] }>(
      "POST",
      "/auth/totp/confirm",
      { secret, code },
      headers,
    );
  },
  totpQr: async (password?: string) => {
    const headers = await stepUpHeaders(password);
    return call<{ secret: string; provisioning_uri: string }>("POST", "/auth/totp/qr", {}, headers);
  },
  totpDisable: async (password?: string) => {
    const pw =
      password ??
      (await confirmPassword({
        title: "Disable two-factor",
        message: "Re-enter your operator password to turn off two-factor authentication.",
      }));
    if (!pw) {
      throw new ApiError(401, "cancelled", "step-up cancelled");
    }
    return call<{ ok: boolean }>("POST", "/auth/totp/disable", { password: pw });
  },
  recovery: () => call<RecoveryStatus>("GET", "/auth/recovery"),
  regenerateRecovery: async (password?: string) => {
    const headers = await stepUpHeaders(password);
    return call<{ ok: boolean; codes: string[] }>("POST", "/auth/recovery/regenerate", {}, headers);
  },
  passkeyRegisterOptions: async (password?: string) => {
    const headers = await stepUpHeaders(password);
    return call<PasskeyCreationOptions>("POST", "/auth/passkey/register/options", {}, headers);
  },
  passkeyRegister: async (
    body: {
      name: string;
      challenge: string;
      response: { clientDataJSON: string; attestationObject: string };
      transports?: string[];
    },
    password?: string,
  ) => {
    const headers = await stepUpHeaders(password);
    return call<{ ok: boolean; credential_id: string }>(
      "POST",
      "/auth/passkey/register/verify",
      body,
      headers,
    );
  },
  passkeys: () => call<{ passkeys: PasskeyInfo[] }>("GET", "/auth/passkeys"),
  deletePasskey: async (id: string, password?: string) => {
    const headers = await stepUpHeaders(password);
    return call<{ ok: boolean; deleted: number }>(
      "DELETE",
      `/auth/passkeys/${encodeURIComponent(id)}`,
      undefined,
      headers,
    );
  },
  status: () =>
    call<{
      ok: boolean;
      runner_connected: boolean;
      runners_connected: number;
      temporal?: TemporalStatus;
    }>("GET", "/status"),
  temporalStatus: () => call<TemporalStatus>("GET", "/temporal/status"),
  temporalWorkflows: (q?: { query?: string; page_size?: number; page_token?: string }) => {
    const p = new URLSearchParams();
    if (q?.query) p.set("query", q.query);
    if (q?.page_size != null) p.set("page_size", String(q.page_size));
    if (q?.page_token) p.set("page_token", q.page_token);
    const qs = p.toString();
    return call<TemporalWorkflowList>("GET", `/temporal/workflows${qs ? `?${qs}` : ""}`);
  },
  temporalWorkflow: (workflowId: string, q?: { run_id?: string; history_limit?: number }) => {
    const p = new URLSearchParams();
    if (q?.run_id) p.set("run_id", q.run_id);
    if (q?.history_limit != null) p.set("history_limit", String(q.history_limit));
    const qs = p.toString();
    return call<TemporalWorkflowDetail>(
      "GET",
      `/temporal/workflows/${encodeURIComponent(workflowId)}${qs ? `?${qs}` : ""}`,
    );
  },
  fleetSkillsSync: (hosts?: string[]) =>
    call<{ workflow_id: string }>("POST", "/fleet/skills-sync", hosts ? { hosts } : {}),
  fleetProvidersUpdate: (opts?: { hosts?: string[]; components?: string[] }) =>
    call<{ workflow_id: string }>("POST", "/fleet/providers-update", opts ?? {}),
  logout: async () => {
    try {
      await call<void>("POST", "/auth/logout");
    } finally {
      setCsrf("");
    }
  },
  servers: () => call<Server[]>("GET", "/servers"),
  createServer: (b: CreateServerBody) => call<Server>("POST", "/servers", b),
  updateServer: (id: string, b: UpdateServerBody) =>
    call<Server>("PATCH", `/servers/${encodeURIComponent(id)}`, b),
  probe: (id: string) => call<unknown>("POST", `/servers/${encodeURIComponent(id)}/probe`),
  skillAuth: (id: string, action: string, opts?: { token?: string; job_id?: string }) =>
    call<SkillAuthResult>("POST", `/servers/${encodeURIComponent(id)}/skill-auth`, {
      action,
      ...(opts?.token ? { token: opts.token } : {}),
      ...(opts?.job_id ? { job_id: opts.job_id } : {}),
    }),
  tools: (id: string) => call<Tool[]>("GET", `/servers/${encodeURIComponent(id)}/tools`),
  setServerEnabled: (id: string, enabled: boolean) =>
    call<Server>("POST", `/servers/${encodeURIComponent(id)}/enable`, { enabled }),
  setToolEnabled: (id: string, enabled: boolean) =>
    call<Tool>("POST", `/tools/${encodeURIComponent(id)}/enable`, { enabled }),
  setToolSchemaOverride: (id: string, schema: Record<string, unknown>) =>
    call<Tool>("PUT", `/tools/${encodeURIComponent(id)}/schema-override`, { schema }),
  clearToolSchemaOverride: (id: string) =>
    call<Tool>("DELETE", `/tools/${encodeURIComponent(id)}/schema-override`),
  setBearerToken: (id: string, token: string) =>
    call<Server>("POST", `/servers/${encodeURIComponent(id)}/auth/token`, { token }),
  oauthStart: (id: string, redirect_uri: string) =>
    call<{ authorize_url: string; state: string }>(
      "POST",
      `/servers/${encodeURIComponent(id)}/oauth/start`,
      { redirect_uri },
    ),
  keys: () => call<Key[]>("GET", "/keys"),
  mintKey: (label: string) =>
    call<Key & { token: string }>("POST", "/keys", { label: label || null }),
  revealKey: (id: string) =>
    call<{ token: string }>("GET", `/keys/${encodeURIComponent(id)}/secret`),
  deleteKey: (id: string) => call<void>("DELETE", `/keys/${encodeURIComponent(id)}`),
  grant: (key: string, server: string) =>
    call<Key>("PUT", `/keys/${encodeURIComponent(key)}/grants/${encodeURIComponent(server)}`),
  ungrant: (key: string, server: string) =>
    call<Key>("DELETE", `/keys/${encodeURIComponent(key)}/grants/${encodeURIComponent(server)}`),
  fleet: () => call<FleetResponse>("GET", "/fleet"),
  audit: (q?: { limit?: number; before?: string; action?: string; target?: string }) => {
    const p = new URLSearchParams();
    if (q?.limit != null) p.set("limit", String(q.limit));
    if (q?.before) p.set("before", q.before);
    if (q?.action) p.set("action", q.action);
    if (q?.target) p.set("target", q.target);
    const qs = p.toString();
    return call<{ items: AuditEvent[]; next: string | null }>("GET", `/audit${qs ? `?${qs}` : ""}`);
  },
  toolCalls: (q?: {
    limit?: number;
    before?: string;
    server?: string;
    outcome?: string;
    key_id?: string;
    site_id?: string;
    trace_id?: string;
  }) => {
    const p = new URLSearchParams();
    if (q?.limit != null) p.set("limit", String(q.limit));
    if (q?.before) p.set("before", q.before);
    if (q?.server) p.set("server", q.server);
    if (q?.outcome) p.set("outcome", q.outcome);
    if (q?.key_id) p.set("key_id", q.key_id);
    if (q?.site_id) p.set("site_id", q.site_id);
    if (q?.trace_id) p.set("trace_id", q.trace_id);
    const qs = p.toString();
    return call<{ items: ToolCallRow[]; next: string | null }>("GET", `/tool-calls${qs ? `?${qs}` : ""}`);
  },
};
