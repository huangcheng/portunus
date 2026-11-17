import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, type AuthStatus } from "./api";
import { AccountPage } from "./AccountPage";
import { AppsPage } from "./AppsPage";
import { ConfirmPasswordHost } from "./ConfirmPassword";
import { FleetPage } from "./FleetPage";
import { BrandMark, Icon } from "./icons/ui";
import { KeysPage } from "./KeysPage";
import { Login } from "./Login";
import { LogsPage } from "./LogsPage";
import { Setup } from "./Setup";
import { TemporalPage } from "./TemporalPage";

type View = "apps" | "keys" | "fleet" | "logs" | "workflows" | "account";

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

const VIEW_META: Record<
  View,
  { title: string; eyebrow: string; headline: string; description: string; index: string }
> = {
  apps: {
    title: "Apps",
    eyebrow: "Integration directory",
    headline: "Your toolstack, connected.",
    description: "One control plane for every tool your agents use.",
    index: "01",
  },
  keys: {
    title: "Agent keys",
    eyebrow: "Access control",
    headline: "Keys to your gateway.",
    description: "Create, distribute, and revoke agent credentials from one secure inventory.",
    index: "02",
  },
  fleet: {
    title: "Fleet",
    eyebrow: "Runtime inventory",
    headline: "Runners under this gateway.",
    description: "Sites, live children, and package drift — read-only operational truth.",
    index: "03",
  },
  workflows: {
    title: "Workflows",
    eyebrow: "Control plane",
    headline: "Durable work, visible.",
    description: "Temporal runs for OAuth refresh, probes, Connect, and fleet sync (tools / skills / CLIs).",
    index: "04",
  },
  logs: {
    title: "Logs",
    eyebrow: "Event history",
    headline: "What happened, when.",
    description: "Operator audit trail and tool-call timing without arguments or results.",
    index: "05",
  },
  account: {
    title: "Account",
    eyebrow: "Operator",
    headline: "Sign-in for this console.",
    description: "Password, two-factor, recovery keys, and passkeys for the single operator.",
    index: "06",
  },
};

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
  const meta = VIEW_META[view];

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
            <button
              type="button"
              className="nav-item"
              aria-current={view === "fleet" ? "page" : undefined}
              onClick={() => setView("fleet")}
            >
              <span className="nav-item-label">
                <Icon name="layers" size={16} />
                Fleet
              </span>
              <Icon name="arrow-up-right" size={13} className="nav-arrow" />
            </button>
            <button
              type="button"
              className="nav-item"
              aria-current={view === "workflows" ? "page" : undefined}
              onClick={() => setView("workflows")}
            >
              <span className="nav-item-label">
                <Icon name="bolt" size={16} />
                Workflows
              </span>
              <Icon name="arrow-up-right" size={13} className="nav-arrow" />
            </button>
            <button
              type="button"
              className="nav-item"
              aria-current={view === "logs" ? "page" : undefined}
              onClick={() => setView("logs")}
            >
              <span className="nav-item-label">
                <Icon name="terminal" size={16} />
                Logs
              </span>
              <Icon name="arrow-up-right" size={13} className="nav-arrow" />
            </button>
            <button
              type="button"
              className="nav-item"
              aria-current={view === "account" ? "page" : undefined}
              onClick={() => setView("account")}
            >
              <span className="nav-item-label">
                <Icon name="shield" size={16} />
                Account
              </span>
              <Icon name="arrow-up-right" size={13} className="nav-arrow" />
            </button>
          </nav>
          <div className="sidebar-note">
            <Icon name="layers" size={16} />
            <p>
              One gateway.
              <br />
              Every tool in reach.
            </p>
            <span>Apps in. Agents out.</span>
          </div>
        </div>
        <div className="sidebar-footer">
          <button type="button" className="gateway-status linkish" onClick={() => setView("fleet")}>
            <span className="gateway-status-label">Gateway status</span>
            <span className={`health ${runners && runners > 0 ? "ok" : ""}`}>
              <span className="dot" aria-hidden />
              <span>{runners == null ? "Checking runners" : `${runners} runner${runners === 1 ? "" : "s"} live`}</span>
            </span>
          </button>
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
            <strong>{meta.title}</strong>
          </div>
          <div className="environment-badge">
            <span className="environment-dot" aria-hidden />
            Private gateway
          </div>
        </header>
        <main className="page-body" id="main-content" tabIndex={-1}>
          <div className={`page-intro ${view}`}>
            <div>
              <p className="page-eyebrow">{meta.eyebrow}</p>
              <h1>{meta.headline}</h1>
              <p className="lede">{meta.description}</p>
            </div>
            <div className="page-intro-index" aria-hidden="true">
              {meta.index}
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
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [view, setView] = useState<View>("apps");
  const [runners, setRunners] = useState<number | null>(null);
  const [keyCount, setKeyCount] = useState<number | null>(null);

  useEffect(() => {
    api.session().then(
      () => setAuthed(true),
      async () => {
        try {
          setAuthStatus(await api.authStatus());
        } catch {
          setAuthStatus({
            configured: true,
            has_password: true,
            has_totp: false,
            has_passkey: false,
          });
        }
        setAuthed(false);
      },
    );
  }, []);

  function showSignedOut() {
    setAuthed(false);
    void api.authStatus().then(setAuthStatus, () =>
      setAuthStatus({ configured: true, has_password: true, has_totp: false, has_passkey: false }),
    );
  }

  useEffect(() => {
    const onUnauth = () => showSignedOut();
    window.addEventListener("portunus-unauthorized", onUnauth);
    return () => window.removeEventListener("portunus-unauthorized", onUnauth);
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

  if (authed === null || (authed === false && authStatus === null)) return <div className="boot">Loading console</div>;
  if (!authed && authStatus && !authStatus.configured) {
    return <Setup status={authStatus} onSetup={() => setAuthed(true)} />;
  }
  if (!authed && authStatus) return <Login status={authStatus} onLogin={() => setAuthed(true)} />;

  return (
    <>
      <Shell
        view={view}
        setView={setView}
        runners={runners}
        keyCount={keyCount}
        onLogout={() => api.logout().finally(() => showSignedOut())}
      >
        {view === "apps" ? <AppsPage /> : null}
        {view === "keys" ? <KeysPage /> : null}
        {view === "fleet" ? <FleetPage /> : null}
        {view === "logs" ? <LogsPage /> : null}
        {view === "workflows" ? <TemporalPage /> : null}
        {view === "account" ? <AccountPage /> : null}
      </Shell>
      <ConfirmPasswordHost />
    </>
  );
}
