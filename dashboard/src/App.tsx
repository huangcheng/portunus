import { useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "./api";
import { AppsPage } from "./AppsPage";
import { KeysPage } from "./KeysPage";
import { Login } from "./Login";
import { ServersPage } from "./ServersPage";

type View = "apps" | "servers" | "keys";

async function copyText(text: string) {
  await navigator.clipboard.writeText(text);
}

function EndpointRow({ label, url }: { label: string; url: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await copyText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard may be denied */
    }
  }
  return (
    <div className="endpoint-row">
      <div className="endpoint-row-text">
        <span className="endpoint-label">{label}</span>
        <span className="endpoint-url" title={url}>
          {url}
        </span>
      </div>
      <button type="button" className="outline sm endpoint-copy" onClick={() => void copy()} title={`Copy ${url}`}>
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

function ApiEndpoints() {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const mcpUrl = `${window.location.origin}/mcp`;

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="api-endpoints" ref={root}>
      <button
        type="button"
        className={`outline sm api-endpoints-trigger${open ? " open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span>API endpoints</span>
        <span className="chev" aria-hidden>
          {open ? "▴" : "▾"}
        </span>
      </button>
      {open && (
        <div className="api-endpoints-pop" role="dialog" aria-label="API endpoints">
          <div className="api-endpoints-heading">Data plane</div>
          <EndpointRow label="MCP" url={mcpUrl} />
        </div>
      )}
    </div>
  );
}

function Shell({
  view,
  setView,
  runners,
  serverCount,
  keyCount,
  onLogout,
  children,
}: {
  view: View;
  setView: (v: View) => void;
  runners: number | null;
  serverCount: number | null;
  keyCount: number | null;
  onLogout: () => void;
  children: ReactNode;
}) {
  const title = view === "apps" ? "Apps" : view === "servers" ? "Servers" : "Agent keys";
  const description =
    view === "apps"
      ? "Connect well-known MCP apps. Custom and stdio servers live under Servers."
      : view === "servers"
        ? "Register remote HTTP/SSE or local stdio MCP servers, probe tools, then enable for agents."
        : "Bearer keys for /mcp — every key can use every server.";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="sidebar-mark" aria-hidden>
            P
          </div>
          <div>
            <div className="sidebar-brand-title">Portunus</div>
            <div className="sidebar-brand-sub">Gateway console</div>
          </div>
        </div>
        <div className="sidebar-content">
          <div className="nav-group">
            <div className="nav-group-label">Operator</div>
            <button
              type="button"
              className="nav-item"
              aria-current={view === "apps" ? "page" : undefined}
              onClick={() => setView("apps")}
            >
              Apps
            </button>
            <button
              type="button"
              className="nav-item"
              aria-current={view === "servers" ? "page" : undefined}
              onClick={() => setView("servers")}
            >
              Servers
              <span className="count">{serverCount ?? "—"}</span>
            </button>
            <button
              type="button"
              className="nav-item"
              aria-current={view === "keys" ? "page" : undefined}
              onClick={() => setView("keys")}
            >
              Agent keys
              <span className="count">{keyCount ?? "—"}</span>
            </button>
          </div>
        </div>
        <div className="sidebar-footer">
          <span className={`health ${runners && runners > 0 ? "ok" : ""}`}>
            <span className="dot" aria-hidden />
            {runners == null ? "checking…" : `${runners} runner${runners === 1 ? "" : "s"} live`}
          </span>
          <ApiEndpoints />
          <button type="button" className="ghost" onClick={onLogout}>
            Sign out
          </button>
        </div>
      </aside>
      <div className="inset">
        <header className="page-header">
          <div className="page-header-text">
            <h1>{title}</h1>
            <p className="lede">{description}</p>
          </div>
        </header>
        <div className="page-body">{children}</div>
      </div>
    </div>
  );
}

export function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [view, setView] = useState<View>("apps");
  const [runners, setRunners] = useState<number | null>(null);
  const [serverCount, setServerCount] = useState<number | null>(null);
  const [keyCount, setKeyCount] = useState<number | null>(null);

  useEffect(() => {
    api.session().then(
      () => setAuthed(true),
      () => setAuthed(false),
    );
  }, []);

  useEffect(() => {
    if (!authed) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const [h, servers, keys] = await Promise.all([
          fetch("/healthz", { credentials: "same-origin" }).then((r) => r.json()),
          api.servers().catch(() => null),
          api.keys().catch(() => null),
        ]);
        if (cancelled) return;
        setRunners(typeof h.runners_connected === "number" ? h.runners_connected : null);
        if (servers) setServerCount(servers.length);
        if (keys) setKeyCount(keys.length);
      } catch {
        if (!cancelled) setRunners(null);
      }
    };
    void tick();
    const id = window.setInterval(tick, 15000);
    const onRefresh = () => void tick();
    window.addEventListener("portunus-refresh", onRefresh);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      window.removeEventListener("portunus-refresh", onRefresh);
    };
  }, [authed]);

  useEffect(() => {
    const onMsg = (ev: MessageEvent) => {
      if (ev.data?.portunusOauth === "ok") {
        window.dispatchEvent(new Event("portunus-refresh"));
      }
    };
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, []);

  if (authed === null) return <div className="boot">Loading console</div>;
  if (!authed) return <Login onLogin={() => setAuthed(true)} />;

  return (
    <Shell
      view={view}
      setView={setView}
      runners={runners}
      serverCount={serverCount}
      keyCount={keyCount}
      onLogout={() => api.logout().finally(() => setAuthed(false))}
    >
      {view === "apps" ? <AppsPage /> : view === "servers" ? <ServersPage /> : <KeysPage />}
    </Shell>
  );
}
