import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { api, type Server } from "./api";
import { WELL_KNOWN_APPS, type WellKnownApp } from "./providers";

function findLinkedServer(app: WellKnownApp, servers: Server[]): Server | null {
  return servers.find((s) => s.slug === app.slug || s.url === app.url) ?? null;
}

export function AppsPage() {
  const [servers, setServers] = useState<Server[]>([]);
  const [toolCounts, setToolCounts] = useState<Record<string, number>>({});
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<WellKnownApp | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const list = await api.servers();
      setServers(list);
      const linked = WELL_KNOWN_APPS.map((a) => findLinkedServer(a, list)).filter(Boolean) as Server[];
      const entries = await Promise.all(
        linked.map(async (s) => {
          try {
            const tools = await api.tools(s.id);
            return [s.id, tools.length] as const;
          } catch {
            return [s.id, 0] as const;
          }
        }),
      );
      setToolCounts(Object.fromEntries(entries));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const onRefresh = () => void refresh();
    window.addEventListener("portunus-refresh", onRefresh);
    return () => window.removeEventListener("portunus-refresh", onRefresh);
  }, [refresh]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return WELL_KNOWN_APPS;
    return WELL_KNOWN_APPS.filter(
      (a) => a.label.toLowerCase().includes(q) || a.description.toLowerCase().includes(q),
    );
  }, [query]);

  const useSections = WELL_KNOWN_APPS.length >= 6 && !query.trim();
  const popular = useSections ? filtered.filter((a) => a.popular) : [];
  const rest = useSections ? filtered.filter((a) => !a.popular) : filtered;

  return (
    <>
      {error && <p className="error">{error}</p>}
      <div className="apps-toolbar">
        <label className="field apps-search">
          <span className="sr-only">Search apps</span>
          <input
            placeholder="Search apps"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoComplete="off"
          />
        </label>
        <p className="apps-toolbar-hint">{WELL_KNOWN_APPS.length} available</p>
      </div>

      {useSections && popular.length > 0 && (
        <AppSection title="Popular">
          {popular.map((app) => {
            const server = findLinkedServer(app, servers);
            return (
              <AppCard
                key={app.id}
                app={app}
                server={server}
                toolCount={server ? toolCounts[server.id] : undefined}
                onOpen={() => setSelected(app)}
              />
            );
          })}
        </AppSection>
      )}

      <AppSection title={useSections ? "All apps" : "Catalog"}>
        {rest.length === 0 ? (
          <p className="apps-empty">No apps match “{query}”.</p>
        ) : (
          rest.map((app) => {
            const server = findLinkedServer(app, servers);
            return (
              <AppCard
                key={app.id}
                app={app}
                server={server}
                toolCount={server ? toolCounts[server.id] : undefined}
                onOpen={() => setSelected(app)}
              />
            );
          })
        )}
      </AppSection>

      {selected && (
        <ManagePanel
          app={selected}
          server={findLinkedServer(selected, servers)}
          onClose={() => setSelected(null)}
          onChanged={async () => {
            await refresh();
            window.dispatchEvent(new Event("portunus-refresh"));
          }}
        />
      )}
    </>
  );
}

function AppSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="apps-section">
      <h2 className="apps-section-title">{title}</h2>
      <div className="apps-grid">{children}</div>
    </section>
  );
}

function AppCard({
  app,
  server,
  toolCount,
  onOpen,
}: {
  app: WellKnownApp;
  server: Server | null;
  toolCount?: number;
  onOpen: () => void;
}) {
  const active = !!server?.has_auth;
  const configured = !!server?.oauth?.configured;
  const toolsLabel =
    typeof toolCount === "number" && toolCount > 0
      ? `${toolCount} tool${toolCount === 1 ? "" : "s"}`
      : null;

  return (
    <article
      className={`app-card${active ? " is-active" : ""}`}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
      role="button"
      tabIndex={0}
    >
      <AppIcon app={app} />
      <div className="app-card-body">
        <div className="app-card-title-row">
          <span className="app-card-title">{app.label}</span>
          {active ? (
            <span className="app-pill ok">Active</span>
          ) : configured ? (
            <span className="app-pill warn">Needs auth</span>
          ) : null}
        </div>
        <p className="app-card-desc">{app.description}</p>
        <div className="app-card-meta">
          {toolsLabel ? (
            <span className="app-meta-item">
              <MetaKeyIcon />
              {toolsLabel}
            </span>
          ) : (
            <span className="app-meta-item muted">
              {active ? "Authorized" : configured ? "Credentials saved" : "Not configured"}
            </span>
          )}
        </div>
      </div>
      <span className={`app-card-cta${active || configured ? " outline" : " primary"} sm`} aria-hidden>
        {active || configured ? "Manage" : "Connect"}
      </span>
    </article>
  );
}

function AppIcon({ app }: { app: WellKnownApp }) {
  if (app.id === "dropbox") {
    return (
      <div className="app-card-mark dropbox" aria-hidden>
        <svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor">
          <path d="M6 2 0 6l6 4 6-4-6-4zm12 0-6 4 6 4 6-4-6-4zM0 14l6 4 6-4-6-4-6 4zm18 0-6 4 6 4 6-4-6-4zm-6 5.2L6.5 15.5 6 15.8 12 20l6-4.2-.5-.3L12 19.2z" />
        </svg>
      </div>
    );
  }
  return (
    <div className="app-card-mark" aria-hidden>
      {app.label.slice(0, 1)}
    </div>
  );
}

function MetaKeyIcon() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.4">
      <circle cx="5.5" cy="8" r="2.5" />
      <path d="M8 8h5.5v2.2M11.2 8v2.2" strokeLinecap="round" />
    </svg>
  );
}

function ManagePanel({
  app,
  server,
  onClose,
  onChanged,
}: {
  app: WellKnownApp;
  server: Server | null;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [clientId, setClientId] = useState(server?.oauth?.client_id ?? "");
  const [clientSecret, setClientSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [current, setCurrent] = useState<Server | null>(server);

  useEffect(() => {
    setCurrent(server);
    setClientId(server?.oauth?.client_id ?? "");
    setClientSecret("");
    setError("");
    setNotice("");
  }, [server, app.id]);

  const active = !!current?.has_auth;
  const configured = !!current?.oauth?.configured;

  async function ensureServer(): Promise<Server> {
    const oauth = {
      authorize_url: app.authorize_url,
      token_url: app.token_url,
      client_id: clientId.trim(),
      scopes: app.scopes,
    };
    const secret = clientSecret.trim() ? clientSecret.trim() : null;
    if (current) {
      const updated = await api.updateServer(current.id, {
        oauth,
        oauth_client_secret: secret,
      });
      setCurrent(updated);
      return updated;
    }
    const created = await api.createServer({
      name: app.name,
      slug: app.slug,
      kind: "remote",
      url: app.url,
      oauth,
      oauth_client_secret: secret,
    });
    setCurrent(created);
    return created;
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    setError("");
    setNotice("");
    setBusy(true);
    try {
      await ensureServer();
      setClientSecret("");
      setNotice("Credentials saved. Use Authorize to complete OAuth.");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function authorize() {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const target = await ensureServer();
      const redirect = `${window.location.origin}/api/oauth/callback`;
      const { authorize_url } = await api.oauthStart(target.id, redirect);
      window.open(authorize_url, "portunus-oauth", "width=560,height=720");
      setNotice("OAuth window opened — finish consent, then this card will show Active.");
      setClientSecret("");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function probe() {
    if (!current) return;
    setError("");
    setNotice("");
    setBusy(true);
    try {
      await api.probe(current.id);
      setNotice("Probe finished — tool catalog refreshed.");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleEnabled() {
    if (!current) return;
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const updated = await api.setServerEnabled(current.id, !current.enabled);
      setCurrent(updated);
      setNotice(updated.enabled ? "Enabled for agents." : "Disabled.");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="apps-drawer-backdrop" role="presentation" onClick={onClose}>
      <aside
        className="apps-drawer"
        role="dialog"
        aria-label={`Manage ${app.label}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="apps-drawer-header">
          <AppIcon app={app} />
          <div>
            <h2>{app.label}</h2>
            <p className="desc">{app.description}</p>
          </div>
          <button type="button" className="ghost sm" onClick={onClose}>
            Close
          </button>
        </div>

        <div className="apps-status-row">
          <span className={`app-pill ${active ? "ok" : configured ? "warn" : ""}`}>
            {active ? "Active" : configured ? "Needs auth" : "Not configured"}
          </span>
          {current && (
            <span className={`app-pill ${current.enabled ? "ok" : ""}`}>
              {current.enabled ? "Enabled" : "Disabled"}
            </span>
          )}
          {current?.oauth?.client_id && (
            <span className="apps-status-id" title={current.oauth.client_id}>
              Client ID · {current.oauth.client_id.slice(0, 8)}…
            </span>
          )}
        </div>

        <div className="stack oauth-box">
          <p className="provider-hint">
            Update Client ID / secret anytime. Redirect URI must be{" "}
            <code>{`${window.location.origin}/api/oauth/callback`}</code>
          </p>
          <div className="provider-meta">
            <div>
              <span className="muted">MCP URL</span>
              <code>{app.url}</code>
            </div>
            <div>
              <span className="muted">Authorize</span>
              <code>{app.authorize_url}</code>
            </div>
            <div>
              <span className="muted">Token</span>
              <code>{app.token_url}</code>
            </div>
            <div>
              <span className="muted">Scopes</span>
              <code>{app.scopes.join(" ")}</code>
            </div>
          </div>
        </div>

        <form className="stack" onSubmit={save}>
          <label className="field">
            Client ID
            <input value={clientId} onChange={(e) => setClientId(e.target.value)} required autoComplete="off" />
          </label>
          <label className="field">
            Client secret {configured ? "(leave blank to keep)" : "(optional)"}
            <input
              type="password"
              value={clientSecret}
              onChange={(e) => setClientSecret(e.target.value)}
              autoComplete="off"
              placeholder={configured ? "••••••••" : undefined}
            />
          </label>

          <div className="apps-actions">
            <button className="outline sm" type="submit" disabled={busy}>
              Save credentials
            </button>
            <button className="primary sm" type="button" disabled={busy || !clientId.trim()} onClick={() => void authorize()}>
              {active ? "Reauthorize" : "Authorize"}
            </button>
          </div>

          {current && (
            <div className="apps-actions">
              <button type="button" className="outline sm" disabled={busy} onClick={() => void probe()}>
                Probe tools
              </button>
              <button type="button" className="outline sm" disabled={busy} onClick={() => void toggleEnabled()}>
                {current.enabled ? "Disable" : "Enable"}
              </button>
            </div>
          )}

          {notice && <p className="apps-notice">{notice}</p>}
          {error && <p className="error">{error}</p>}
        </form>
      </aside>
    </div>
  );
}
