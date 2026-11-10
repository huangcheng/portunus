import { Fragment, useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api, type Server, type Tool } from "./api";

export function ServersPage() {
  const [servers, setServers] = useState<Server[]>([]);
  const [tools, setTools] = useState<Record<string, Tool[]>>({});
  const [openTools, setOpenTools] = useState<Record<string, boolean>>({});
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const list = await api.servers();
      setServers(list);
      const entries = await Promise.all(list.map(async (s) => [s.id, await api.tools(s.id)] as const));
      setTools(Object.fromEntries(entries));
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => void refresh(), [refresh]);
  useEffect(() => {
    const onRefresh = () => void refresh();
    window.addEventListener("portunus-refresh", onRefresh);
    return () => window.removeEventListener("portunus-refresh", onRefresh);
  }, [refresh]);

  const act = (fn: () => Promise<unknown>) => async () => {
    setError("");
    try {
      await fn();
      await refresh();
      window.dispatchEvent(new Event("portunus-refresh"));
    } catch (e) {
      if ((e as { code?: string }).code !== "cancelled") setError((e as Error).message);
    }
  };

  return (
    <>
      {error && <p className="error">{error}</p>}

      {creating && (
        <div className="card create-card">
          <h2>New server</h2>
          <CreateServer
            onCreated={() => {
              setCreating(false);
              void refresh();
              window.dispatchEvent(new Event("portunus-refresh"));
            }}
            onCancel={() => setCreating(false)}
          />
        </div>
      )}

      <div className="card">
        <div className="card-header">
          <h2>Inventory</h2>
          <p className="desc">
            Probe refreshes tools from upstream. Expand a tools count to see names and descriptions. Enable before agents can use a
            server.
          </p>
          <div className="action">
            <button type="button" className="primary sm" onClick={() => setCreating((v) => !v)}>
              {creating ? "Close" : "Add server"}
            </button>
          </div>
        </div>
        <div className="card-body">
          {servers.length === 0 && !creating ? (
            <div className="empty">
              <strong>No servers yet</strong>
              Add a remote MCP URL or a stdio command to expose tools through this gateway.
            </div>
          ) : (
            <div className="table-wrap">
              <table className="flush">
                <thead>
                  <tr>
                    <th>Server</th>
                    <th>Endpoint</th>
                    <th>Status</th>
                    <th>Tools</th>
                    <th className="text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {servers.map((s) => {
                    const list = tools[s.id] ?? [];
                    const expanded = !!openTools[s.id];
                    const endpoint =
                      s.kind === "stdio" ? [s.command, ...(s.args ?? [])].join(" ") : (s.url ?? "");
                    return (
                      <Fragment key={s.id}>
                        <tr>
                          <td>
                            <div className="name">{s.name}</div>
                            <div className="slug">{s.slug}</div>
                            <div style={{ marginTop: 6 }}>
                              <span className="badge">{s.kind}</span>
                            </div>
                          </td>
                          <td>
                            <div className="endpoint" title={endpoint}>
                              {endpoint}
                            </div>
                            {s.env_names.length > 0 && (
                              <div className="mono" style={{ marginTop: 4 }}>
                                vault · {s.env_names.join(", ")}
                              </div>
                            )}
                          </td>
                          <td>
                            <div className="stack" style={{ gap: 4 }}>
                              <span className={`status ${s.enabled ? "on" : ""}`}>
                                {s.enabled ? "enabled" : "disabled"}
                              </span>
                              {s.kind === "remote" && (
                                <span className={`status ${s.has_auth ? "on" : "warn"}`}>
                                  {s.has_auth ? "authed" : "needs token"}
                                </span>
                              )}
                            </div>
                          </td>
                          <td>
                            <button
                              type="button"
                              className={`tools-toggle${expanded ? " open" : ""}`}
                              aria-expanded={expanded}
                              disabled={list.length === 0}
                              title={list.length === 0 ? "No tools — Probe first" : expanded ? "Hide tools" : "Show tools"}
                              onClick={() => setOpenTools((m) => ({ ...m, [s.id]: !expanded }))}
                            >
                              <span className="tools-toggle-count">{list.length}</span>
                              <span className="tools-toggle-label">tools</span>
                              <span className="tools-toggle-chev" aria-hidden>
                                {expanded ? "▴" : "▾"}
                              </span>
                            </button>
                          </td>
                          <td>
                            <div className="actions">
                              <IconBtn label="Probe" onClick={act(() => api.probe(s.id))}>
                                <IconProbe />
                              </IconBtn>
                              <IconBtn
                                label={s.enabled ? "Disable" : "Enable"}
                                onClick={act(() => api.setServerEnabled(s.id, !s.enabled))}
                              >
                                {s.enabled ? <IconPowerOff /> : <IconPower />}
                              </IconBtn>
                              {s.kind === "remote" && (
                                <>
                                  <IconBtn
                                    label="Paste token"
                                    onClick={act(async () => {
                                      const token = window.prompt("Paste bearer token (or full Authorization value)");
                                      if (!token) throw Object.assign(new Error("cancelled"), { code: "cancelled" });
                                      await api.setBearerToken(s.id, token);
                                    })}
                                  >
                                    <IconClipboard />
                                  </IconBtn>
                                  {s.oauth?.configured && (
                                    <IconBtn
                                      label="OAuth"
                                      onClick={act(async () => {
                                        const redirect = `${window.location.origin}/api/oauth/callback`;
                                        const { authorize_url } = await api.oauthStart(s.id, redirect);
                                        window.open(authorize_url, "portunus-oauth", "width=560,height=720");
                                      })}
                                    >
                                      <IconKey />
                                    </IconBtn>
                                  )}
                                </>
                              )}
                            </div>
                          </td>
                        </tr>
                        {expanded && list.length > 0 && (
                          <tr className="tools-expand-row">
                            <td colSpan={5}>
                              <ul className="tools-list">
                                {list.map((t: Tool) => (
                                  <li key={t.id} className={!t.enabled ? "disabled" : undefined}>
                                    <code className="tools-list-name">{t.name}</code>
                                    <span className="tools-list-desc">
                                      {t.description?.trim() || "No description"}
                                    </span>
                                  </li>
                                ))}
                              </ul>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function IconBtn({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button type="button" className="outline icon-btn" title={label} aria-label={label} onClick={onClick}>
      {children}
    </button>
  );
}

function IconProbe() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3.2 8a4.8 4.8 0 0 1 8.2-3.4M12.8 8a4.8 4.8 0 0 1-8.2 3.4" strokeLinecap="round" />
      <path d="M11.2 2.8v2.6H8.6M4.8 13.2V10.6h2.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function IconPower() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M8 2.5v5.5" strokeLinecap="round" />
      <path d="M5.1 4.4a4.5 4.5 0 1 0 5.8 0" strokeLinecap="round" />
    </svg>
  );
}

function IconPowerOff() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M8 2.5v5.5" strokeLinecap="round" opacity="0.35" />
      <path d="M5.1 4.4a4.5 4.5 0 1 0 5.8 0" strokeLinecap="round" />
      <path d="M3.2 12.8 12.8 3.2" strokeLinecap="round" />
    </svg>
  );
}

function IconClipboard() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="4" y="3.5" width="8" height="10" rx="1.5" />
      <path d="M6 3.5V2.8a1.3 1.3 0 0 1 1.3-1.3h1.4A1.3 1.3 0 0 1 10 2.8v.7" strokeLinecap="round" />
      <path d="M6.5 7.5h3M6.5 10h3" strokeLinecap="round" />
    </svg>
  );
}

function IconKey() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="6" cy="8" r="2.6" />
      <path d="M8.4 8h5.2v2.2M11.5 8v2.2" strokeLinecap="round" />
    </svg>
  );
}

function CreateServer({ onCreated, onCancel }: { onCreated: () => void; onCancel: () => void }) {
  const [kind, setKind] = useState<"stdio" | "remote">("remote");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [command, setCommand] = useState("npx");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [env, setEnv] = useState("");
  const [regions, setRegions] = useState("");
  const [authMode, setAuthMode] = useState<"none" | "token" | "oauth">("token");
  const [oauthAuthorize, setOauthAuthorize] = useState("");
  const [oauthToken, setOauthToken] = useState("");
  const [oauthClientId, setOauthClientId] = useState("");
  const [oauthScopes, setOauthScopes] = useState("");
  const [oauthSecret, setOauthSecret] = useState("");
  const [error, setError] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const envMap: Record<string, string> = {};
      for (const line of env.split("\n").map((l) => l.trim()).filter(Boolean)) {
        const i = line.indexOf("=");
        if (i < 1) throw new Error(`bad secret line: ${line.split("=")[0]}`);
        envMap[line.slice(0, i)] = line.slice(i + 1);
      }
      const preferred_regions = regions.split(/[,\s]+/).map((r) => r.trim()).filter(Boolean);
      if (kind === "stdio") {
        await api.createServer({
          name,
          slug,
          kind: "stdio",
          command,
          args: args.split(/\s+/).filter(Boolean),
          env: envMap,
          preferred_regions,
        });
      } else {
        const oauth =
          authMode === "oauth" && oauthAuthorize && oauthToken && oauthClientId
            ? {
                authorize_url: oauthAuthorize,
                token_url: oauthToken,
                client_id: oauthClientId,
                scopes: oauthScopes.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean),
              }
            : null;
        await api.createServer({
          name,
          slug,
          kind: "remote",
          url,
          env: authMode === "none" ? {} : envMap,
          preferred_regions,
          oauth,
          oauth_client_secret: authMode === "oauth" && oauthSecret ? oauthSecret : null,
        });
      }
      onCreated();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <form className="stack" onSubmit={submit}>
      <div className="segment" role="tablist" aria-label="Server kind">
        <button type="button" aria-selected={kind === "remote"} onClick={() => setKind("remote")}>
          Remote HTTP
        </button>
        <button type="button" aria-selected={kind === "stdio"} onClick={() => setKind("stdio")}>
          Stdio
        </button>
      </div>
      <div className="row">
        <label className="field grow">
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} required />
        </label>
        <label className="field grow">
          Slug
          <input value={slug} onChange={(e) => setSlug(e.target.value)} required />
        </label>
      </div>
      {kind === "stdio" ? (
        <div className="row">
          <label className="field" style={{ width: 120 }}>
            Command
            <input value={command} onChange={(e) => setCommand(e.target.value)} required />
          </label>
          <label className="field grow">
            Args
            <input placeholder="--ignore-scripts pkg@1.2.3" value={args} onChange={(e) => setArgs(e.target.value)} />
          </label>
        </div>
      ) : (
        <label className="field">
          MCP URL
          <input placeholder="https://example.com/mcp" value={url} onChange={(e) => setUrl(e.target.value)} required />
        </label>
      )}
      {kind === "stdio" ? (
        <label className="field">
          Env secrets
          <textarea placeholder="API_KEY=… (one per line)" rows={2} value={env} onChange={(e) => setEnv(e.target.value)} />
        </label>
      ) : (
        <>
          <div className="segment" role="tablist" aria-label="Auth mode">
            <button type="button" aria-selected={authMode === "none"} onClick={() => setAuthMode("none")}>
              No auth
            </button>
            <button type="button" aria-selected={authMode === "token"} onClick={() => setAuthMode("token")}>
              API token
            </button>
            <button type="button" aria-selected={authMode === "oauth"} onClick={() => setAuthMode("oauth")}>
              OAuth
            </button>
          </div>
          {authMode === "token" && (
            <label className="field">
              Request headers (vault)
              <textarea
                placeholder={"Authorization=Bearer …\nX-Api-Key=…"}
                rows={2}
                value={env}
                onChange={(e) => setEnv(e.target.value)}
              />
            </label>
          )}
          {authMode === "oauth" && (
            <div className="stack oauth-box">
              <div className="row">
                <label className="field grow">
                  Authorize URL
                  <input value={oauthAuthorize} onChange={(e) => setOauthAuthorize(e.target.value)} required />
                </label>
                <label className="field grow">
                  Token URL
                  <input value={oauthToken} onChange={(e) => setOauthToken(e.target.value)} required />
                </label>
              </div>
              <div className="row">
                <label className="field grow">
                  Client ID
                  <input value={oauthClientId} onChange={(e) => setOauthClientId(e.target.value)} required />
                </label>
                <label className="field grow">
                  Scopes
                  <input value={oauthScopes} onChange={(e) => setOauthScopes(e.target.value)} />
                </label>
              </div>
              <label className="field">
                Client secret (optional)
                <input type="password" value={oauthSecret} onChange={(e) => setOauthSecret(e.target.value)} autoComplete="off" />
              </label>
            </div>
          )}
        </>
      )}
      <label className="field">
        Preferred regions
        <input placeholder="optional soft affinity" value={regions} onChange={(e) => setRegions(e.target.value)} />
      </label>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button type="button" className="ghost" onClick={onCancel}>
          Cancel
        </button>
        <button className="primary sm" type="submit">
          Create
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </form>
  );
}
