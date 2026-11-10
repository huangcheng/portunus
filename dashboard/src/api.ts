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
    throw new ApiError(res.status, data.code ?? "error", data.message ?? (detail ? `invalid: ${detail}` : res.statusText));
  }
  return data as T;
}

export type OAuthConfig = {
  authorize_url: string;
  token_url: string;
  client_id: string;
  scopes: string[];
  configured: boolean;
};

export type UpdateServerBody = {
  preferred_regions?: string[];
  oauth?: {
    authorize_url: string;
    token_url: string;
    client_id: string;
    scopes: string[];
  } | null;
  oauth_client_secret?: string | null;
};

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
  preferred_regions: string[];
  env_names: string[];
};
export type Tool = { id: string; name: string; description: string | null; enabled: boolean };
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
  oauth?: {
    authorize_url: string;
    token_url: string;
    client_id: string;
    scopes: string[];
  } | null;
  oauth_client_secret?: string | null;
};

export const api = {
  login: async (password: string) => {
    const r = await call<{ csrf_token: string }>("POST", "/auth/login", { password });
    setCsrf(r.csrf_token);
  },
  session: async () => {
    const r = await call<{ csrf_token: string }>("GET", "/auth/session");
    setCsrf(r.csrf_token);
  },
  logout: () => call<void>("POST", "/auth/logout"),
  servers: () => call<Server[]>("GET", "/servers"),
  createServer: (b: CreateServerBody) => call<Server>("POST", "/servers", b),
  updateServer: (id: string, b: UpdateServerBody) => call<Server>("PATCH", `/servers/${id}`, b),
  probe: (id: string) => call<unknown>("POST", `/servers/${id}/probe`),
  tools: (id: string) => call<Tool[]>("GET", `/servers/${id}/tools`),
  setServerEnabled: (id: string, enabled: boolean) =>
    call<Server>("POST", `/servers/${id}/enable`, { enabled }),
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
};
