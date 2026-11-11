// Thin fetch wrapper: session cookie is sent automatically; CSRF token is kept in memory only.

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

let csrf = "";
export const setCsrf = (t: string) => {
  csrf = t;
};

async function call<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(method !== "GET" ? { "X-CSRF-Token": csrf } : {}),
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.errors)
      ? data.errors.map((e: { loc: string[] }) => e.loc.join(".")).join(", ")
      : "";
    // Session gone / never logged in: bounce the shell to <Login/>. Skip /auth/login
    // so a wrong password stays on the form instead of remounting it.
    if (res.status === 401 && path !== "/auth/login") {
      csrf = "";
      window.dispatchEvent(new Event("portunus-unauthorized"));
    }
    throw new ApiError(res.status, data.code ?? "error", data.message ?? (detail ? `invalid: ${detail}` : res.statusText));
  }
  return data as T;
}

export type OAuthConfig = {
  authorize_url: string;
  token_url: string;
  client_id: string;
  scopes: string[];
  resource?: string | null;
  registration_endpoint?: string | null;
  token_endpoint_auth_method?: string | null;
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
  registration_endpoint?: string;
  token_endpoint_auth_method?: string;
};

export type UpdateServerBody = {
  preferred_regions?: string[];
  oauth?: OAuthBody | null;
  oauth_client_secret?: string | null;
  /** Stdio vault upsert (names only returned); omit keys to leave unchanged. */
  env?: Record<string, string>;
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

export const api = {
  apps: () => call<CatalogAppRow[]>("GET", "/apps"),
  createApp: (b: Omit<CatalogAppRow, "id">) => call<CatalogAppRow>("POST", "/apps", b),
  updateApp: (id: string, b: Partial<CatalogAppRow>) => call<CatalogAppRow>("PATCH", `/apps/${id}`, b),
  deleteApp: (id: string) => call<void>("DELETE", `/apps/${id}`),
  login: async (password: string) => {
    const r = await call<{ csrf_token: string }>("POST", "/auth/login", { password });
    setCsrf(r.csrf_token);
  },
  session: async () => {
    const r = await call<{ csrf_token: string }>("GET", "/auth/session");
    setCsrf(r.csrf_token);
  },
  status: () =>
    call<{ ok: boolean; runner_connected: boolean; runners_connected: number }>("GET", "/status"),
  logout: () => call<void>("POST", "/auth/logout"),
  servers: () => call<Server[]>("GET", "/servers"),
  createServer: (b: CreateServerBody) => call<Server>("POST", "/servers", b),
  updateServer: (id: string, b: UpdateServerBody) => call<Server>("PATCH", `/servers/${id}`, b),
  probe: (id: string) => call<unknown>("POST", `/servers/${id}/probe`),
  skillAuth: (id: string, action: string, opts?: { token?: string; job_id?: string }) =>
    call<SkillAuthResult>("POST", `/servers/${id}/skill-auth`, {
      action,
      ...(opts?.token ? { token: opts.token } : {}),
      ...(opts?.job_id ? { job_id: opts.job_id } : {}),
    }),
  tools: (id: string) => call<Tool[]>("GET", `/servers/${id}/tools`),
  setServerEnabled: (id: string, enabled: boolean) =>
    call<Server>("POST", `/servers/${id}/enable`, { enabled }),
  setToolEnabled: (id: string, enabled: boolean) =>
    call<Tool>("POST", `/tools/${id}/enable`, { enabled }),
  setBearerToken: (id: string, token: string) =>
    call<Server>("POST", `/servers/${id}/auth/token`, { token }),
  oauthStart: (id: string, redirect_uri: string) =>
    call<{ authorize_url: string; state: string }>("POST", `/servers/${id}/oauth/start`, { redirect_uri }),
  keys: () => call<Key[]>("GET", "/keys"),
  mintKey: (label: string) => call<Key & { token: string }>("POST", "/keys", { label: label || null }),
  revealKey: (id: string) => call<{ token: string }>("GET", `/keys/${id}/secret`),
  deleteKey: (id: string) => call<void>("DELETE", `/keys/${id}`),
  grant: (key: string, server: string) => call<Key>("PUT", `/keys/${key}/grants/${server}`),
  ungrant: (key: string, server: string) => call<Key>("DELETE", `/keys/${key}/grants/${server}`),
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
