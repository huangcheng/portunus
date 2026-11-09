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

// Sensitive actions: ask for the password, trade it for a short-lived step-up token.
async function withStepUp<T>(fn: (h: Record<string, string>) => Promise<T>): Promise<T> {
  const password = window.prompt("Re-enter operator password to confirm");
  if (!password) throw new ApiError(0, "cancelled", "cancelled");
  const { token } = await call<{ token: string }>("POST", "/auth/step-up", { password });
  return fn({ "X-Step-Up-Token": token });
}

export type Server = {
  id: string;
  name: string;
  slug: string;
  kind: string;
  enabled: boolean;
  command: string | null;
  args: string[] | null;
  env_names: string[];
};
export type Tool = { id: string; name: string; description: string | null; enabled: boolean };
export type Key = {
  key_id: string;
  label: string | null;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  server_ids: string[];
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
  createServer: (b: { name: string; slug: string; command: string; args: string[]; env: Record<string, string> }) =>
    call<Server>("POST", "/servers", { ...b, kind: "stdio" }),
  probe: (id: string) => call<unknown>("POST", `/servers/${id}/probe`),
  tools: (id: string) => call<Tool[]>("GET", `/servers/${id}/tools`),
  setServerEnabled: (id: string, enabled: boolean) =>
    withStepUp((h) => call<Server>("POST", `/servers/${id}/enable`, { enabled }, h)),
  setToolEnabled: (id: string, enabled: boolean) =>
    withStepUp((h) => call<Tool>("POST", `/tools/${id}/enable`, { enabled }, h)),
  keys: () => call<Key[]>("GET", "/keys"),
  mintKey: (label: string) => call<Key & { token: string }>("POST", "/keys", { label: label || null }),
  revokeKey: (id: string) => withStepUp((h) => call<Key>("POST", `/keys/${id}/revoke`, undefined, h)),
  grant: (key: string, server: string) => call<Key>("PUT", `/keys/${key}/grants/${server}`),
  ungrant: (key: string, server: string) =>
    withStepUp((h) => call<Key>("DELETE", `/keys/${key}/grants/${server}`, undefined, h)),
};
