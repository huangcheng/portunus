import { useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "./api";
import { AppsPage } from "./AppsPage";
import { BrandMark, Icon } from "./icons/ui";
import { KeysPage } from "./KeysPage";
import { Login } from "./Login";

type View = "apps" | "keys";

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
        <Icon name={copied ? "check" : "copy"} size={14} />
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
        <Icon name="terminal" size={14} />
        <span>API endpoints</span>
        <Icon name="chevron-down" size={13} className="chev" />
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
  keyCount,
  onLogout,
  children,
}: {
  view: View;
  setView: (v: View) => void;
  runners: number | null;
  keyCount: number | null;
  onLogout: () => void;
  children: ReactNode;
}) {
  const title = view === "apps" ? "Apps" : "Agent keys";
  const description =
    view === "apps"
      ? "One control plane for every tool your agents use."
      : "Create, distribute, and revoke agent credentials from one secure inventory.";

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="sidebar-header">
          <BrandMark />
          <div>
            <div className="sidebar-brand-title">Portunus</div>
            <div className="sidebar-brand-sub">MCP control plane</div>
          </div>
        </div>
        <div className="sidebar-content">
          <nav className="nav-group" aria-label="Operator navigation">
            <div className="nav-group-label">Workspace</div>
            <button
              type="button"
              className="nav-item"
              aria-current={view === "apps" ? "page" : undefined}
              onClick={() => setView("apps")}
            >
              <span className="nav-item-label">
                <Icon name="apps" size={16} />
                Apps
              </span>
              <Icon name="arrow-up-right" size={13} className="nav-arrow" />
            </button>
            <button
              type="button"
              className="nav-item"
              aria-current={view === "keys" ? "page" : undefined}
              onClick={() => setView("keys")}
            >
              <span className="nav-item-label">
                <Icon name="key" size={16} />
                Agent keys
              </span>
              <span className="count">{keyCount ?? "—"}</span>
            </button>
          </nav>
          <div className="sidebar-note">
            <Icon name="layers" size={16} />
            <p>One gateway.<br />Every tool in reach.</p>
            <span>Apps in. Agents out.</span>
          </div>
        </div>
        <div className="sidebar-footer">
          <div className="gateway-status">
            <span className="gateway-status-label">Gateway status</span>
            <span className={`health ${runners && runners > 0 ? "ok" : ""}`}>
              <span className="dot" aria-hidden />
              <span>{runners == null ? "Checking runners" : `${runners} runner${runners === 1 ? "" : "s"} live`}</span>
            </span>
          </div>
          <ApiEndpoints />
          <button type="button" className="ghost sidebar-signout" onClick={onLogout} aria-label="Sign out">
            <Icon name="logout" size={14} />
            <span>Sign out</span>
          </button>
        </div>
      </aside>
      <div className="inset">
        <header className="utility-bar">
          <div className="breadcrumb">
            <span>Portunus</span>
            <Icon name="chevron-right" size={11} />
            <strong>{title}</strong>
          </div>
          <div className="environment-badge">
            <span className="environment-dot" aria-hidden />
            Private gateway
          </div>
        </header>
        <main className="page-body" id="main-content" tabIndex={-1}>
          <div className={`page-intro ${view}`}>
            <div>
              <p className="page-eyebrow">{view === "apps" ? "Integration directory" : "Access control"}</p>
              <h1>{view === "apps" ? "Your toolstack, connected." : "Keys to your gateway."}</h1>
              <p className="lede">{description}</p>
            </div>
            <div className="page-intro-index" aria-hidden="true">
              {view === "apps" ? "01" : "02"}
            </div>
          </div>
          {children}
        </main>
      </div>
    </div>
  );
}

export function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [view, setView] = useState<View>("apps");
  const [runners, setRunners] = useState<number | null>(null);
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
        const [status, keys] = await Promise.all([
          api.status().catch(() => null),
          api.keys().catch(() => null),
        ]);
        if (cancelled) return;
        setRunners(typeof status?.runners_connected === "number" ? status.runners_connected : null);
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
      keyCount={keyCount}
      onLogout={() => api.logout().finally(() => setAuthed(false))}
    >
      {view === "apps" ? <AppsPage /> : <KeysPage />}
    </Shell>
  );
}
