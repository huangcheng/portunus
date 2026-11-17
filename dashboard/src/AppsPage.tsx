import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode, type Ref } from "react";
import { paginate } from "./paginate";
import { api, type OAuthBody, type Server, type Tool } from "./api";
import { CatalogConfigPage } from "./CatalogConfigPage";
import { EnvFields } from "./EnvFields";
import { parseEnvRows } from "./envRows";
import { CreateMcpForm } from "./CreateMcpForm";
import { GatewayOverview } from "./GatewayOverview";
import { AppBrandMark } from "./icons/apps";
import { Icon } from "./icons/ui";
import {
  WEBDAV_MCP_ARGS,
  WEBDAV_MCP_PACKAGE,
  hasSkillBridgeSecrets,
  hasWebDavCredentials,
  isDeviceQrAuth,
  isHttpApp,
  isOAuthApp,
  isOauthLinkAuth,
  isPlatformSkillAuth,
  isSkillBridgeApp,
  isStdioApp,
  oauthLinkAuthOptions,
  isWebDavApp,
  specBool,
  specString,
  specStringList,
  type CatalogApp,
  type HttpApp,
  type OAuthApp,
  type SkillBridgeApp,
  type StdioApp,
  type WebDavApp,
} from "./providers";
import { SetupHints } from "./SetupHints";
import {
  ROUTING_CHOICES,
  choiceToRegions,
  regionsToChoice,
  type RoutingChoice,
} from "./routingPreference";
import {
  normalizeStdioArgs,
  prepareStdioArgv,
  stdioArgsHint,
  validateStdioArgs,
} from "./stdioArgv";

type Tab = "all" | "connected";

/** Popup for oauth_link authorize URLs (no QR — vendor扫一扫 misreads the URL as a code). */
function openAuthorizeWindow(url: string) {
  const popup = window.open(url, "portunus-oauth", "popup=yes,width=560,height=720");
  if (!popup) {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

function appUrl(app: CatalogApp): string | null {
  const url = specString(app.spec, "url");
  return url || null;
}

function findLinkedServer(app: CatalogApp, servers: Server[]): Server | null {
  const url = appUrl(app);
  if (url) {
    return servers.find((s) => s.slug === app.slug || s.url === url) ?? null;
  }
  return servers.find((s) => s.slug === app.slug) ?? null;
}

function catalogAppForServer(server: Server, catalog: CatalogApp[]): CatalogApp | undefined {
  return catalog.find(
    (a) => a.slug === server.slug || (appUrl(a) !== null && appUrl(a) === server.url),
  );
}

function appStatus(app: CatalogApp, server: Server | null): {
  active: boolean;
  configured: boolean;
} {
  if (isWebDavApp(app)) {
    const active = hasWebDavCredentials(server);
    return { active, configured: active };
  }
  if (isSkillBridgeApp(app)) {
    const active = hasSkillBridgeSecrets(app, server);
    // Server row exists ≠ authorized (platform auth needs SKILL_SESSION).
    return { active, configured: !!server };
  }
  if (isHttpApp(app)) {
    const needsUrl = specBool(app.spec, "url_from_operator");
    const active = needsUrl ? !!(server?.url && server.has_auth) : !!server?.has_auth;
    return { active, configured: active };
  }
  if (isStdioApp(app)) {
    // Non-secret env (optional URLs / host defaults) must not block "Live".
    const required = parseEnvRows(app.spec.env)
      .filter((r) => r.secret)
      .map((r) => r.name);
    if (required.length) {
      const names = new Set(server?.env_names ?? []);
      const active = required.every((n) => names.has(n));
      return { active, configured: !!server };
    }
    return { active: !!server?.enabled, configured: !!server };
  }
  // Catalog CIMD/DCR flags alone must not mark an app "Needs auth" before Connect.
  const oauthConfigured =
    !!server &&
    (!!server.oauth?.configured ||
      !!server.oauth?.client_id ||
      !!server.oauth?.registration_endpoint ||
      !!server.oauth?.use_cimd ||
      !!server.oauth?.allow_bearer_token ||
      specBool(app.spec, "allow_bearer_token"));
  const needsUrl = specBool(app.spec, "url_from_operator");
  return {
    active: !!server?.has_auth,
    configured: oauthConfigured && (!needsUrl || !!server?.url),
  };
}

function serverConnected(server: Server, catalog: CatalogApp[]): boolean {
  const app = catalogAppForServer(server, catalog);
  if (app) {
    const { active, configured } = appStatus(app, server);
    return active || configured || server.enabled;
  }
  return true;
}

function oauthBodyFromApp(
  app: OAuthApp,
  clientId: string,
  existing?: Server | null,
): OAuthBody {
  const body: OAuthBody = {
    authorize_url: specString(app.spec, "authorize_url"),
    token_url: specString(app.spec, "token_url"),
    scopes: specStringList(app.spec, "scopes"),
  };
  // Prefer the live server's DCR'd / CIMD client_id so re-authorize does not wipe it.
  const id =
    clientId.trim() ||
    existing?.oauth?.client_id ||
    specString(app.spec, "client_id");
  if (id) body.client_id = id;
  const resource = specString(app.spec, "resource") || existing?.oauth?.resource || "";
  if (resource) body.resource = resource;
  const useCimd = specBool(app.spec, "use_cimd") || !!existing?.oauth?.use_cimd;
  if (useCimd) {
    body.use_cimd = true;
  } else {
    const reg =
      specString(app.spec, "registration_endpoint") ||
      existing?.oauth?.registration_endpoint ||
      "";
    if (reg) body.registration_endpoint = reg;
  }
  const method =
    specString(app.spec, "token_endpoint_auth_method") ||
    existing?.oauth?.token_endpoint_auth_method ||
    "";
  if (method) body.token_endpoint_auth_method = method;
  // RFC 9207 issuer (optional): app spec wins, else keep the live server's value so a
  // re-authorize from the UI never silently downgrades exact iss validation.
  const issuer =
    specString(app.spec, "issuer") || existing?.oauth?.issuer || "";
  if (issuer) body.issuer = issuer;
  return body;
}

function toolsLabel(n: number | undefined | null): string | null {
  if (typeof n !== "number" || n <= 0) return null;
  return `${n} tool${n === 1 ? "" : "s"}`;
}

/** Prefer GET /tools length; fall back to health tool_count when the fetch failed/yielded 0. */
function resolvedToolCount(
  toolCount: number | undefined,
  serverToolCount: number | null | undefined,
): number | null {
  if (typeof toolCount === "number" && toolCount > 0) return toolCount;
  if (typeof serverToolCount === "number") return serverToolCount;
  if (typeof toolCount === "number") return toolCount;
  return null;
}

function healthPill(status: Server["status"] | undefined): { label: string; kind: string } | null {
  if (status === "auth_required") return { label: "Needs auth", kind: "warn" };
  if (status === "unreachable") return { label: "Unreachable", kind: "warn" };
  if (status === "probing") return { label: "Probing", kind: "" };
  return null;
}

function hostLabel(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    return new URL(url).host;
  } catch {
    return url.replace(/^https?:\/\//, "").split("/")[0] || url;
  }
}

function CardChevron() {
  return (
    <span className="app-card-chevron" aria-hidden>
      <Icon name="arrow-up-right" size={16} />
    </span>
  );
}

function normalizeCatalogApp(app: CatalogApp): CatalogApp {
  return {
    ...app,
    categories: Array.isArray(app.categories) ? app.categories : [],
    tags: Array.isArray(app.tags) ? app.tags : [],
  };
}

const APPS_PAGE_SIZE = 12;

export function AppsPage() {
  const [tab, setTab] = useState<Tab>("all");
  const [catalog, setCatalog] = useState<CatalogApp[]>([]);
  const [servers, setServers] = useState<Server[]>([]);
  const [toolCounts, setToolCounts] = useState<Record<string, number>>({});
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<string>("all");
  const [galleryPage, setGalleryPage] = useState(1);
  const [selectedApp, setSelectedApp] = useState<CatalogApp | null>(null);
  const [selectedServer, setSelectedServer] = useState<Server | null>(null);
  const [adding, setAdding] = useState(false);
  const [catalogConfig, setCatalogConfig] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [apps, list] = await Promise.all([api.apps(), api.servers()]);
      setCatalog(apps.map(normalizeCatalogApp));
      setServers(list);
      const entries = await Promise.all(
        list.map(async (s) => {
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

  const categories = useMemo(() => {
    const seen = new Map<string, string>();
    for (const app of catalog) {
      for (const c of app.categories) {
        const key = c.toLowerCase();
        if (!seen.has(key)) seen.set(key, c);
      }
    }
    return [...seen.values()].sort((a, b) => a.localeCompare(b));
  }, [catalog]);

  const filteredCatalog = useMemo(() => {
    const q = query.trim().toLowerCase();
    return catalog.filter((a) => {
      if (category !== "all" && !a.categories.some((c) => c.toLowerCase() === category.toLowerCase())) {
        return false;
      }
      if (!q) return true;
      const hay = [
        a.label,
        a.name,
        a.description,
        a.slug,
        a.connection,
        ...a.categories,
        ...a.tags,
      ]
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
  }, [catalog, query, category]);

  const {
    pageCount: galleryPageCount,
    safePage: safeGalleryPage,
    pageItems: pagedCatalog,
    rangeStart: galleryRangeStart,
    rangeEnd: galleryRangeEnd,
  } = useMemo(
    () => paginate(filteredCatalog, galleryPage, APPS_PAGE_SIZE),
    [filteredCatalog, galleryPage],
  );
  const allAppsSectionRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    setGalleryPage((p) => Math.min(p, galleryPageCount));
  }, [galleryPageCount]);

  function resetGalleryPage() {
    setGalleryPage(1);
  }

  function goGalleryPage(next: number) {
    const page = Math.min(galleryPageCount, Math.max(1, next));
    if (page === safeGalleryPage) return;
    setGalleryPage(page);
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    allAppsSectionRef.current?.scrollIntoView({
      block: "start",
      behavior: reduceMotion ? "auto" : "smooth",
    });
  }

  const connected = useMemo(() => {
    const q = query.trim().toLowerCase();
    return servers.filter((s) => {
      if (!serverConnected(s, catalog)) return false;
      if (!q) return true;
      return (
        s.name.toLowerCase().includes(q) ||
        s.slug.toLowerCase().includes(q) ||
        (s.url ?? "").toLowerCase().includes(q)
      );
    });
  }, [servers, catalog, query]);

  const discoveredTools = Object.values(toolCounts).reduce((sum, count) => sum + count, 0);
  const showOverview = !catalogConfig && !query.trim() && category === "all" && tab === "all";

  // Popular is a spotlight strip; All apps still includes those same apps (Composio-style).
  const popular = filteredCatalog.filter((a) => a.popular);
  const showPopular = popular.length > 0 && showOverview;

  function openCatalog(app: CatalogApp) {
    setSelectedServer(null);
    setSelectedApp(app);
  }

  function openConnected(server: Server) {
    const app = catalogAppForServer(server, catalog);
    if (app) {
      setSelectedApp(app);
      setSelectedServer(null);
    } else {
      setSelectedApp(null);
      setSelectedServer(server);
    }
  }

  async function onChanged() {
    await refresh();
    window.dispatchEvent(new Event("portunus-refresh"));
  }

  return (
    <>
      {error && <div className="notice error">{error}</div>}

      {showOverview && (
        <GatewayOverview
          appCount={catalog.length}
          connectedCount={connected.length}
          toolCount={discoveredTools}
        />
      )}

      <div className="apps-toolbar">
        <div className="apps-tabs" role="tablist" aria-label="Apps views">
          <button
            type="button"
            role="tab"
            aria-selected={!catalogConfig && tab === "all"}
            className={!catalogConfig && tab === "all" ? "active" : undefined}
            onClick={() => {
              setCatalogConfig(false);
              setTab("all");
              resetGalleryPage();
            }}
          >
            All
            <span className="apps-tab-count">{catalog.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={!catalogConfig && tab === "connected"}
            className={!catalogConfig && tab === "connected" ? "active" : undefined}
            onClick={() => {
              setCatalogConfig(false);
              setTab("connected");
              resetGalleryPage();
            }}
          >
            Connected
            <span className="apps-tab-count">{connected.length}</span>
          </button>
        </div>
        {!catalogConfig && (
          <label className="field apps-search">
            <span className="sr-only">Search apps</span>
            <span className="search-control">
              <Icon name="search" size={14} />
              <input
                placeholder="Search apps"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  resetGalleryPage();
                }}
                autoComplete="off"
              />
            </span>
          </label>
        )}
        <div className="apps-toolbar-actions">
          <button
            type="button"
            className={catalogConfig ? "outline sm" : "ghost sm"}
            onClick={() => {
              setAdding(false);
              setSelectedApp(null);
              setSelectedServer(null);
              setCatalogConfig((v) => !v);
            }}
          >
            <Icon name="layers" size={14} />
            {catalogConfig ? "Back to Apps" : "Catalog config"}
          </button>
          {!catalogConfig && (
            <button
              type="button"
              className={adding ? "outline sm" : "primary sm"}
              onClick={() => setAdding((v) => !v)}
            >
              <Icon name={adding ? "close" : "plus"} size={14} />
              {adding ? "Close" : "Add custom MCP"}
            </button>
          )}
        </div>
      </div>

      {catalogConfig ? (
        <CatalogConfigPage catalog={catalog} onChanged={onChanged} />
      ) : (
        <>
      {tab === "all" && categories.length > 0 && (
        <div className="apps-categories" role="radiogroup" aria-label="App categories">
          <button
            type="button"
            role="radio"
            aria-checked={category === "all"}
            className={category === "all" ? "active" : undefined}
            onClick={() => {
              setCategory("all");
              resetGalleryPage();
            }}
          >
            All
          </button>
          {categories.map((c) => (
            <button
              key={c}
              type="button"
              role="radio"
              aria-checked={category.toLowerCase() === c.toLowerCase()}
              className={category.toLowerCase() === c.toLowerCase() ? "active" : undefined}
              onClick={() => {
                setCategory(c);
                resetGalleryPage();
              }}
            >
              {c}
            </button>
          ))}
        </div>
      )}

      {adding && (
        <div className="card create-card">
          <h2>Add custom MCP</h2>
          <p className="desc">Remote HTTP/SSE or stdio endpoint outside the catalog.</p>
          <CreateMcpForm
            onCreated={() => {
              setAdding(false);
              setTab("connected");
              void onChanged();
            }}
            onCancel={() => setAdding(false)}
          />
        </div>
      )}

      {tab === "all" ? (
        <>
          {showPopular && (
            <AppSection title="Popular">
              {popular.map((app) => {
                const server = findLinkedServer(app, servers);
                return (
                  <CatalogCard
                    key={app.id}
                    app={app}
                    server={server}
                    toolCount={server ? toolCounts[server.id] : undefined}
                    onOpen={() => openCatalog(app)}
                  />
                );
              })}
            </AppSection>
          )}
          <AppSection
            title="All apps"
            sectionRef={allAppsSectionRef}
            count={filteredCatalog.length || undefined}
            footer={
              galleryPageCount > 1 ? (
                <div className="apps-pagination" role="navigation" aria-label="App pagination">
                  <span className="apps-pagination-meta">
                    {galleryRangeStart}–{galleryRangeEnd} of {filteredCatalog.length}
                  </span>
                  <div className="apps-pagination-controls">
                    <button
                      type="button"
                      className="ghost sm"
                      aria-disabled={safeGalleryPage <= 1}
                      onClick={() => {
                        if (safeGalleryPage <= 1) return;
                        goGalleryPage(safeGalleryPage - 1);
                      }}
                    >
                      Previous
                    </button>
                    <span className="apps-pagination-page" aria-live="polite">
                      {safeGalleryPage} / {galleryPageCount}
                    </span>
                    <button
                      type="button"
                      className="outline sm"
                      aria-disabled={safeGalleryPage >= galleryPageCount}
                      onClick={() => {
                        if (safeGalleryPage >= galleryPageCount) return;
                        goGalleryPage(safeGalleryPage + 1);
                      }}
                    >
                      Next
                    </button>
                  </div>
                </div>
              ) : null
            }
          >
            {filteredCatalog.length === 0 ? (
              <p className="apps-empty">
                No apps match{query.trim() ? ` “${query.trim()}”` : " this category"}.
              </p>
            ) : (
              pagedCatalog.map((app) => {
                const server = findLinkedServer(app, servers);
                return (
                  <CatalogCard
                    key={app.id}
                    app={app}
                    server={server}
                    toolCount={server ? toolCounts[server.id] : undefined}
                    onOpen={() => openCatalog(app)}
                  />
                );
              })
            )}
          </AppSection>
        </>
      ) : connected.length === 0 ? (
        <div className="empty">
          <strong>No connected apps</strong>
          Connect from All, or add a custom MCP.
        </div>
      ) : (
        <AppSection title="Connected">
          {connected.map((server) => (
            <ConnectedCard
              key={server.id}
              server={server}
              catalog={catalogAppForServer(server, catalog)}
              toolCount={toolCounts[server.id]}
              onOpen={() => openConnected(server)}
            />
          ))}
        </AppSection>
      )}

      {selectedApp && (
        <ManagePanel
          app={selectedApp}
          server={findLinkedServer(selectedApp, servers)}
          onClose={() => setSelectedApp(null)}
          onChanged={onChanged}
        />
      )}
      {selectedServer && (
        <GenericManagePanel
          server={selectedServer}
          onClose={() => setSelectedServer(null)}
          onChanged={async () => {
            await onChanged();
            const list = await api.servers();
            const next = list.find((s) => s.id === selectedServer.id) ?? null;
            setSelectedServer(next);
          }}
        />
      )}
        </>
      )}
    </>
  );
}

function AppSection({
  title,
  children,
  count: countProp,
  footer,
  sectionRef,
}: {
  title: string;
  children: ReactNode;
  count?: number;
  footer?: ReactNode;
  sectionRef?: Ref<HTMLElement | null>;
}) {
  const featured = title === "Popular";
  const count = countProp ?? (Array.isArray(children) ? children.length : undefined);
  return (
    <section ref={sectionRef} className={`apps-section${featured ? " featured" : ""}`}>
      <div className="apps-section-heading">
        <div>
          <p className="section-kicker">{featured ? "Curated starting points" : title === "Connected" ? "Live integrations" : "Integration directory"}</p>
          <h2 className="apps-section-title">{title}</h2>
        </div>
        {typeof count === "number" && <span className="section-count">{String(count).padStart(2, "0")}</span>}
      </div>
      <div className="apps-grid">{children}</div>
      {footer}
    </section>
  );
}

function CatalogCard({
  app,
  server,
  toolCount,
  onOpen,
}: {
  app: CatalogApp;
  server: Server | null;
  toolCount?: number;
  onOpen: () => void;
}) {
  const { active, configured } = appStatus(app, server);
  const count = resolvedToolCount(toolCount, server?.tool_count);
  const label = toolsLabel(count);
  const paused = !!server && !server.enabled && (active || configured);
  const health = !paused ? healthPill(server?.status) : null;

  return (
    <article
      className={`app-card${active && !paused ? " is-live" : ""}${paused ? " is-paused" : ""}`}
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
      <AppBrandMark id={app.slug} label={app.label} />
      <div className="app-card-body">
        <div className="app-card-title-row">
          <span className="app-card-title">{app.label}</span>
          {paused ? (
            <span className="app-pill warn">Paused</span>
          ) : health ? (
            <span className={`app-pill ${health.kind}`}>{health.label}</span>
          ) : active ? (
            <span className="app-status-dot" title="Live for agents" />
          ) : configured ? (
            <span className="app-pill warn">Needs auth</span>
          ) : null}
        </div>
        <p className="app-card-desc">{app.description}</p>
        <div className="app-card-meta">
          <span
            className={`app-meta-item ${
              !paused && !health && active ? "is-connected" : "muted"
            }`}
          >
            {paused
              ? "Paused"
              : health
                ? health.label
                : active
                  ? "Connected"
                  : configured
                    ? "Needs authorization"
                    : "Not connected"}
          </span>
          {label && (
            <span className="app-meta-item">
              <Icon name="terminal" size={12} />
              {label}
            </span>
          )}
        </div>
        {(app.categories.length > 0 || app.tags.length > 0) && (
          <div className="app-card-tags">
            {app.categories.slice(0, 2).map((c) => (
              <span key={`c-${c}`} className="app-tag is-category">
                {c}
              </span>
            ))}
            {app.tags.slice(0, 3).map((t) => (
              <span key={`t-${t}`} className="app-tag">
                {t}
              </span>
            ))}
          </div>
        )}
      </div>
      <CardChevron />
    </article>
  );
}

function ConnectedCard({
  server,
  catalog,
  toolCount,
  onOpen,
}: {
  server: Server;
  catalog?: CatalogApp;
  toolCount?: number;
  onOpen: () => void;
}) {
  const count = resolvedToolCount(toolCount, server.tool_count);
  const label = toolsLabel(count);
  const title = catalog?.label ?? server.name;
  const desc =
    catalog?.description ??
    (server.kind === "stdio"
      ? [server.command, ...(server.args ?? [])].filter(Boolean).join(" ") || server.slug
      : hostLabel(server.url) ?? "Custom MCP");
  const health = server.enabled ? healthPill(server.status) : null;

  return (
    <article
      className={`app-card${server.enabled ? " is-live" : " is-paused"}`}
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
      <AppBrandMark id={catalog?.slug ?? server.slug} label={title} />
      <div className="app-card-body">
        <div className="app-card-title-row">
          <span className="app-card-title">{title}</span>
          {!server.enabled ? (
            <span className="app-pill warn">Paused</span>
          ) : health ? (
            <span className={`app-pill ${health.kind}`}>{health.label}</span>
          ) : (
            <span className="app-status-dot" title="Live for agents" />
          )}
        </div>
        <p className="app-card-desc">{desc}</p>
        <div className="app-card-meta">
          <span
            className={`app-meta-item ${
              server.enabled && !health ? "is-connected" : "muted"
            }`}
          >
            {!server.enabled ? "Paused" : health ? health.label : "Connected"}
          </span>
          {label ? (
            <span className="app-meta-item">
              <Icon name="terminal" size={12} />
              {label}
            </span>
          ) : (
            <span className="app-meta-item muted">{server.slug}</span>
          )}
        </div>
      </div>
      <CardChevron />
    </article>
  );
}

function ManagePanel({
  app,
  server,
  onClose,
  onChanged,
}: {
  app: CatalogApp;
  server: Server | null;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  if (isWebDavApp(app)) {
    return <WebDavManagePanel app={app} server={server} onClose={onClose} onChanged={onChanged} />;
  }
  if (isSkillBridgeApp(app)) {
    return <SkillBridgeManagePanel app={app} server={server} onClose={onClose} onChanged={onChanged} />;
  }
  if (isHttpApp(app)) {
    return <HttpManagePanel app={app} server={server} onClose={onClose} onChanged={onChanged} />;
  }
  if (isStdioApp(app)) {
    return <StdioManagePanel app={app} server={server} onClose={onClose} onChanged={onChanged} />;
  }
  if (isOAuthApp(app)) {
    return <OAuthManagePanel app={app} server={server} onClose={onClose} onChanged={onChanged} />;
  }
  return (
    <GenericManagePanel
      server={
        server ?? {
          id: "",
          name: app.name,
          slug: app.slug,
          kind: "stdio",
          enabled: false,
          command: null,
          args: null,
          url: null,
          oauth: null,
          has_auth: false,
          preferred_regions: [],
          env_names: [],
        }
      }
      onClose={onClose}
      onChanged={onChanged}
    />
  );
}

function OAuthManagePanel({
  app,
  server,
  onClose,
  onChanged,
}: {
  app: OAuthApp;
  server: Server | null;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const useCimd = specBool(app.spec, "use_cimd");
  const allowBearer = specBool(app.spec, "allow_bearer_token");
  const urlFromOperator = specBool(app.spec, "url_from_operator");
  const needsManualClientId =
    !specString(app.spec, "registration_endpoint") && !useCimd;
  const [clientId, setClientId] = useState(
    server?.oauth?.client_id || specString(app.spec, "client_id"),
  );
  const [clientSecret, setClientSecret] = useState("");
  const [bearerToken, setBearerToken] = useState("");
  const [mcpUrl, setMcpUrl] = useState(server?.url || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [current, setCurrent] = useState<Server | null>(server);

  useEffect(() => {
    setCurrent(server);
    setClientId(server?.oauth?.client_id || specString(app.spec, "client_id"));
    setClientSecret("");
    setBearerToken("");
    setMcpUrl(server?.url || "");
    setError("");
    setNotice("");
  }, [server, app]);

  const active = !!current?.has_auth;
  const configured = allowBearer
    ? !!current
    : !!current &&
      (!!current.oauth?.configured ||
        !!current.oauth?.client_id ||
        !!current.oauth?.use_cimd ||
        useCimd ||
        !!specString(app.spec, "registration_endpoint")) &&
      (!urlFromOperator || !!current.url);

  function resolveMcpUrl(): string {
    if (urlFromOperator) {
      const url = mcpUrl.trim();
      if (!url) throw new Error("MCP Streamable HTTP endpoint is required");
      return url;
    }
    return specString(app.spec, "url");
  }

  async function ensureServer(opts?: { forBearer?: boolean }): Promise<Server> {
    const oauth = oauthBodyFromApp(app, clientId, current);
    const forBearer = !!opts?.forBearer || allowBearer;
    const url = resolveMcpUrl();
    // Token paste does not need an OAuth client_id (Fastmail public redirects
    // are not DCR/CIMD-eligible — API token is the supported path).
    if (forBearer && !oauth.client_id) {
      if (current) {
        if (urlFromOperator && url !== current.url) {
          const updated = await api.updateServer(current.id, { url });
          setCurrent(updated);
          return updated;
        }
        return current;
      }
      const created = await api.createServer({
        name: app.name,
        slug: app.slug,
        kind: "remote",
        url,
        recipe_slug: app.slug,
      });
      setCurrent(created);
      return created;
    }
    if (needsManualClientId && !oauth.client_id) {
      throw new Error("Client ID is required");
    }
    const secret = clientSecret.trim() ? clientSecret.trim() : null;
    if (current) {
      const updated = await api.updateServer(current.id, {
        oauth,
        oauth_client_secret: secret,
        ...(urlFromOperator ? { url } : {}),
      });
      setCurrent(updated);
      return updated;
    }
    const created = await api.createServer({
      name: app.name,
      slug: app.slug,
      kind: "remote",
      url,
      oauth,
      oauth_client_secret: secret,
      recipe_slug: app.slug,
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
      if (allowBearer) {
        if (!bearerToken.trim()) {
          setError(active ? "Enter a new token to replace the current one." : "Paste an MCP API token.");
          return;
        }
        const target = await ensureServer({ forBearer: true });
        const updated = await api.setBearerToken(target.id, bearerToken.trim());
        setCurrent(updated);
        setBearerToken("");
        setClientSecret("");
        setNotice("API token saved. Probe tools to go live for agents.");
        await onChanged();
        return;
      }
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
      setBearerToken("");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <DrawerShell
      title={app.label}
      description={app.description}
      iconId={app.slug}
      onClose={onClose}
      current={current}
      active={active}
      configured={configured}
    >
      <form className="stack drawer-section" onSubmit={save}>
        {urlFromOperator && (
          <label className="field">
            MCP Streamable HTTP endpoint
            <input
              type="url"
              value={mcpUrl}
              onChange={(e) => setMcpUrl(e.target.value)}
              required
              autoComplete="off"
              placeholder="https://openapi-mcp.cn-hangzhou.aliyuncs.com/…"
            />
          </label>
        )}
        {allowBearer ? (
          <label className="field">
            MCP API token {active ? "(replace)" : ""}
            <input
              type="password"
              value={bearerToken}
              onChange={(e) => setBearerToken(e.target.value)}
              autoComplete="off"
              placeholder={active ? "••••••••" : "Paste MCP API token"}
            />
          </label>
        ) : needsManualClientId ? (
          <>
            <label className="field">
              Client ID
              <input
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
                required
                autoComplete="off"
              />
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
          </>
        ) : (
          <p className="provider-hint">
            {useCimd
              ? "Authorize uses Portunus client metadata (CIMD)"
              : "Uses dynamic client registration on Authorize"}
            {current?.oauth?.client_id ? (
              <>
                {" "}
                · client <code>{current.oauth.client_id.slice(0, 8)}…</code>
              </>
            ) : null}
            .
          </p>
        )}
        <SetupHints hints={specStringList(app.spec, "setup_hints")} />
        {!allowBearer && (
          <p className="provider-hint">
            Redirect URI: <code>{`${window.location.origin}/api/oauth/callback`}</code>
          </p>
        )}

        <div className="apps-actions">
          {(allowBearer || needsManualClientId || urlFromOperator) && (
            <button
              className={allowBearer ? "primary sm" : "outline sm"}
              type="submit"
              disabled={
                busy ||
                (allowBearer && !bearerToken.trim()) ||
                (urlFromOperator && !mcpUrl.trim())
              }
            >
              {allowBearer ? "Save token" : "Save"}
            </button>
          )}
          {!allowBearer && (
            <button
              className="primary sm"
              type="button"
              disabled={
                busy ||
                (needsManualClientId && !clientId.trim()) ||
                (urlFromOperator && !mcpUrl.trim())
              }
              onClick={() => void authorize()}
            >
              {active ? "Reauthorize" : "Authorize"}
            </button>
          )}
        </div>

        <details className="apps-tech">
          <summary>Endpoint details</summary>
          <div className="provider-meta">
            <div>
              <span className="muted">MCP</span>
              <code>{specString(app.spec, "url")}</code>
            </div>
            {!allowBearer && (
              <>
                <div>
                  <span className="muted">Authorize</span>
                  <code>{specString(app.spec, "authorize_url")}</code>
                </div>
                <div>
                  <span className="muted">Token</span>
                  <code>{specString(app.spec, "token_url")}</code>
                </div>
                <div>
                  <span className="muted">Scopes</span>
                  <code>{specStringList(app.spec, "scopes").join(" ")}</code>
                </div>
                {specString(app.spec, "resource") && (
                  <div>
                    <span className="muted">Resource</span>
                    <code>{specString(app.spec, "resource")}</code>
                  </div>
                )}
              </>
            )}
          </div>
        </details>

        <ServerLifecycle
          current={current}
          busy={busy}
          setBusy={setBusy}
          setError={setError}
          setNotice={setNotice}
          setCurrent={setCurrent}
          onChanged={onChanged}
        />

        {notice && <div className="notice success">{notice}</div>}
        {error && <div className="notice error">{error}</div>}
      </form>
    </DrawerShell>
  );
}


function skillBridgeSecretDefs(app: SkillBridgeApp) {
  // Shared with CatalogConfigForm via parseEnvRows (bare strings + {name,label,secret}).
  // Multiline is suppressed for skill_bridge until the gateway preserves it.
  return parseEnvRows(app.spec.secrets).map((row) => ({ ...row, multiline: false }));
}

function skillBridgeEnvDefaults(app: SkillBridgeApp): Record<string, string> {
  const raw = app.spec.env_defaults;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof k === "string" && k.trim() && typeof v === "string") out[k.trim()] = v;
  }
  return out;
}

function SkillBridgeManagePanel({
  app,
  server,
  onClose,
  onChanged,
}: {
  app: SkillBridgeApp;
  server: Server | null;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const secretDefs = skillBridgeSecretDefs(app);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [current, setCurrent] = useState<Server | null>(server);
  const [qrPng, setQrPng] = useState<string | null>(null);
  const [authUrl, setAuthUrl] = useState<string | null>(null);
  const [authCode, setAuthCode] = useState("");
  const [loginMessage, setLoginMessage] = useState("");

  useEffect(() => {
    setCurrent(server);
    setValues({});
    setError("");
    setNotice("");
    setQrPng(null);
    setAuthUrl(null);
    setAuthCode("");
    setLoginMessage("");
  }, [server, app]);

  const active = hasSkillBridgeSecrets(app, current);
  const configured = !!current;
  const command = specString(app.spec, "command", "python");
  const args = specStringList(app.spec, "args");
  const envDefaults = skillBridgeEnvDefaults(app);
  const backend =
    app.spec.manifest && typeof app.spec.manifest === "object"
      ? String((app.spec.manifest as { backend?: unknown }).backend || "")
      : "";
  const deviceQr = isDeviceQrAuth(app);
  const oauthLink = isOauthLinkAuth(app);
  const platformAuth = isPlatformSkillAuth(app);
  const oauthOpts = oauthLink ? oauthLinkAuthOptions(app) : null;

  async function ensureServer(): Promise<Server> {
    if (current) {
      // Backfill catalog env_defaults onto existing vaults.
      const missing = Object.fromEntries(
        Object.entries(envDefaults).filter(([k]) => !current.env_names.includes(k)),
      );
      if (Object.keys(missing).length) {
        const updated = await api.updateServer(current.id, { env: missing });
        setCurrent(updated);
        return updated;
      }
      return current;
    }
    const created = await api.createServer({
      name: app.name,
      slug: app.slug,
      kind: "stdio",
      command: command || "python",
      args: args.length
        ? args
        : ["-m", "portunus_skill_bridge", "--manifest-env", "PORTUNUS_MANIFEST"],
      env: { ...envDefaults },
      recipe_slug: app.slug,
    });
    setCurrent(created);
    return created;
  }

  async function refreshServer(id: string) {
    await onChanged();
    const refreshed = (await api.servers()).find((s) => s.id === id) || null;
    setCurrent(refreshed);
  }

  async function startDeviceQrLogin() {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const srv = await ensureServer();
      const res = await api.skillAuth(srv.id, "login");
      const data =
        res.result && typeof res.result === "object"
          ? (res.result.data as Record<string, unknown> | undefined)
          : undefined;
      setQrPng(res.qr_png_base64);
      setLoginMessage(
        typeof data?.message === "string"
          ? data.message
          : "Scan the QR code to authorize Portunus.",
      );
      if (res.logged_in) {
        setNotice("Logged in. Probe tools to go live for agents.");
        setQrPng(null);
        await refreshServer(srv.id);
        return;
      }
      setNotice("Waiting for scan…");
      for (let i = 0; i < 90; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const st = await api.skillAuth(srv.id, "login-status");
        if (st.qr_png_base64) setQrPng(st.qr_png_base64);
        if (st.logged_in) {
          setNotice("Logged in. Probe tools to go live for agents.");
          setQrPng(null);
          setLoginMessage("");
          await refreshServer(srv.id);
          return;
        }
        const code =
          st.result && typeof st.result === "object"
            ? String((st.result as { code?: unknown }).code || "")
            : "";
        if (code === "LOGIN_REQUIRED" || code === "LOGIN_DENIED" || code === "LOGIN_EXPIRED") {
          throw new Error(code === "LOGIN_DENIED" ? "Login cancelled" : "Login expired — try again");
        }
      }
      throw new Error("Login timed out — try again");
    } catch (err) {
      setError((err as Error).message);
      setQrPng(null);
    } finally {
      setBusy(false);
    }
  }

  async function startOauthLinkLogin() {
    setError("");
    setNotice("");
    setAuthCode("");
    setBusy(true);
    try {
      const srv = await ensureServer();
      const res = await api.skillAuth(srv.id, "login");
      if (res.logged_in) {
        setNotice("Already authorized. Probe tools to go live for agents.");
        setAuthUrl(null);
        await refreshServer(srv.id);
        return;
      }
      const url = res.auth_url || null;
      if (!url) {
        const msg =
          res.result && typeof res.result === "object" && typeof res.result.msg === "string"
            ? res.result.msg
            : "No authorize URL returned — try again";
        throw new Error(msg);
      }
      setAuthUrl(url);
      const opts = oauthLinkAuthOptions(app);
      setLoginMessage(opts.connectHint);
      openAuthorizeWindow(url);
      setNotice("Authorize window opened — finish in the popup or phone browser.");
      const jobId = res.job_id || "";
      if (!jobId || !opts.browserCapture) {
        setNotice(
          opts.tokenPaste
            ? "Finish in the authorize window, then paste the code below if needed."
            : "Finish authorization in the popup or phone browser.",
        );
        return;
      }
      const polls = Math.max(1, Math.ceil(opts.captureTimeoutS / opts.pollIntervalS));
      for (let i = 0; i < polls; i++) {
        await new Promise((r) => setTimeout(r, opts.pollIntervalS * 1000));
        const st = await api.skillAuth(srv.id, "status", { job_id: jobId });
        if (st.auth_url) setAuthUrl(st.auth_url);
        if (st.logged_in) {
          setAuthUrl(null);
          setLoginMessage("");
          setNotice("Logged in. Probe tools to go live for agents.");
          await refreshServer(srv.id);
          return;
        }
        if (st.pending === false && !st.logged_in) {
          const msg =
            st.result && typeof st.result === "object" && typeof st.result.msg === "string"
              ? st.result.msg
              : "Authorization expired — try Connect again";
          throw new Error(msg);
        }
      }
      throw new Error("Login timed out — try Connect again");
    } catch (err) {
      setError((err as Error).message);
      setAuthUrl(null);
    } finally {
      setBusy(false);
    }
  }

  async function submitOauthCode() {
    if (!current) return;
    const code = authCode.trim();
    if (!code) {
      setError("Paste the authorization code first");
      return;
    }
    setError("");
    setBusy(true);
    try {
      const res = await api.skillAuth(current.id, "login", { token: code });
      if (!res.logged_in) {
        const msg =
          res.result && typeof res.result === "object" && typeof res.result.msg === "string"
            ? res.result.msg
            : "Authorization failed — check the code and try again";
        throw new Error(msg);
      }
      setAuthUrl(null);
      setAuthCode("");
      setLoginMessage("");
      setNotice("Logged in. Probe tools to go live for agents.");
      await refreshServer(current.id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function startPlatformLogin() {
    if (oauthLink) {
      await startOauthLinkLogin();
      return;
    }
    await startDeviceQrLogin();
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    if (platformAuth && !secretDefs.length) {
      await startPlatformLogin();
      return;
    }
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const env: Record<string, string> = {};
      for (const def of secretDefs) {
        const v = (values[def.name] || "").trim();
        if (v) env[def.name] = v;
      }
      if (current) {
        if (!Object.keys(env).length) {
          setNotice("Already connected. Enter a new secret only when rotating.");
          return;
        }
        const updated = await api.updateServer(current.id, { env });
        setCurrent(updated);
      } else {
        for (const def of secretDefs) {
          if (def.secret && !env[def.name]) throw new Error(`${def.label} is required`);
        }
        const created = await api.createServer({
          name: app.name,
          slug: app.slug,
          kind: "stdio",
          command: command || "python",
          args: args.length
            ? args
            : ["-m", "portunus_skill_bridge", "--manifest-env", "PORTUNUS_MANIFEST"],
          env: { ...envDefaults, ...env },
          recipe_slug: app.slug,
        });
        setCurrent(created);
      }
      setValues({});
      setNotice("Saved. Probe tools to refresh and go live for agents.");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function logoutPlatform() {
    if (!current) return;
    setError("");
    setNotice("");
    setBusy(true);
    try {
      await api.skillAuth(current.id, "logout");
      setQrPng(null);
      setLoginMessage("");
      setNotice("Logged out. Connect again to authorize.");
      await onChanged();
      const refreshed = (await api.servers()).find((s) => s.id === current.id) || null;
      setCurrent(refreshed);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <DrawerShell
      title={app.label}
      description={app.description}
      iconId={app.slug}
      onClose={onClose}
      current={current}
      active={active}
      configured={configured}
    >
      <form className="stack drawer-section" onSubmit={save}>
        <p className="provider-hint">
          {deviceQr
            ? "Scan the QR to sign in. Login stays on the Portunus runner — agents only see app tools, never login or QR."
            : oauthLink
              ? `${oauthOpts?.connectHint || "Open the authorize link and confirm in the vendor app."} Login stays on the runner — agents never see codes.`
              : "API secrets stay in the Portunus vault. The tool manifest lives in the catalog and updates without rebuilding — connected servers respawn on save."}
        </p>
        <SetupHints hints={specStringList(app.spec, "setup_hints")} />
        <EnvFields
          defs={secretDefs}
          values={values}
          onChange={(name, value) => setValues((prev) => ({ ...prev, [name]: value }))}
          active={active}
        />

        {qrPng && (
          <div className="skill-auth-qr">
            <img
              src={`data:image/png;base64,${qrPng}`}
              alt="Login QR code"
              width={220}
              height={220}
            />
            {loginMessage && <p className="muted">{loginMessage}</p>}
          </div>
        )}

        {authUrl && (
          <div className="skill-auth-link">
            {loginMessage && <p className="muted">{loginMessage}</p>}
            <div className="apps-actions" style={{ justifyContent: "flex-start" }}>
              <button
                className="primary sm"
                type="button"
                onClick={() => openAuthorizeWindow(authUrl)}
              >
                Open authorize page
              </button>
              <button
                className="ghost sm"
                type="button"
                onClick={() => {
                  void navigator.clipboard?.writeText(authUrl).then(
                    () => setNotice("Authorize link copied — open it on your phone browser."),
                    () => setError("Could not copy link"),
                  );
                }}
              >
                Copy link
              </button>
            </div>
            {(oauthOpts?.tokenPaste ?? true) && (
              <>
                <label className="field">
                  Authorization code
                  <input
                    type="text"
                    value={authCode}
                    onChange={(e) => setAuthCode(e.target.value)}
                    autoComplete="off"
                    placeholder="Paste code from the authorize page if needed"
                  />
                </label>
                <button
                  className="primary sm"
                  type="button"
                  disabled={busy || !authCode.trim()}
                  onClick={() => void submitOauthCode()}
                >
                  Submit code
                </button>
              </>
            )}
          </div>
        )}

        <div className="apps-actions">
          {platformAuth && !secretDefs.length ? (
            <>
              <button className="primary sm" type="submit" disabled={busy}>
                {active ? "Re-authorize" : authUrl ? "Refresh link" : "Connect"}
              </button>
              {active && (
                <button className="ghost sm" type="button" disabled={busy} onClick={() => void logoutPlatform()}>
                  Log out
                </button>
              )}
            </>
          ) : (
            <button className="primary sm" type="submit" disabled={busy}>
              {current ? "Save" : "Connect"}
            </button>
          )}
        </div>

        <details className="apps-tech">
          <summary>Endpoint details</summary>
          <div className="provider-meta">
            <div>
              <span className="muted">Transport</span>
              <code>skill-bridge{backend ? ` · ${backend}` : ""}</code>
            </div>
            <div>
              <span className="muted">Manifest</span>
              <code>catalog DB (hot-reload)</code>
            </div>
            {current && (
              <div>
                <span className="muted">Slug</span>
                <code>{current.slug}</code>
              </div>
            )}
          </div>
        </details>

        <ServerLifecycle
          current={current}
          busy={busy}
          setBusy={setBusy}
          setError={setError}
          setNotice={setNotice}
          setCurrent={setCurrent}
          onChanged={onChanged}
        />

        {notice && <div className="notice success">{notice}</div>}
        {error && <div className="notice error">{error}</div>}
      </form>
    </DrawerShell>
  );
}

function WebDavManagePanel({
  app,
  server,
  onClose,
  onChanged,
}: {
  app: WebDavApp;
  server: Server | null;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const defaultRoot = specString(app.spec, "root_url");
  const defaultPath = specString(app.spec, "root_path", "/");
  const webdavArgs = specStringList(app.spec, "args");
  const [rootUrl, setRootUrl] = useState(defaultRoot);
  const [rootPath, setRootPath] = useState(defaultPath || "/");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [current, setCurrent] = useState<Server | null>(server);

  useEffect(() => {
    setCurrent(server);
    setRootUrl(specString(app.spec, "root_url"));
    setRootPath(specString(app.spec, "root_path", "/") || "/");
    setUsername("");
    setPassword("");
    setError("");
    setNotice("");
  }, [server, app]);

  const active = hasWebDavCredentials(current);
  const configured = active;

  function buildEnv(): Record<string, string> {
    const env: Record<string, string> = {
      WEBDAV_ROOT_URL: rootUrl.trim() || defaultRoot,
      WEBDAV_ROOT_PATH: rootPath.trim() || "/",
      WEBDAV_AUTH_ENABLED: "true",
    };
    if (username.trim()) env.WEBDAV_USERNAME = username.trim();
    if (password.trim()) env.WEBDAV_PASSWORD = password.trim();
    return env;
  }

  async function ensureServer(): Promise<Server> {
    const env = buildEnv();
    if (current) {
      const patch: Record<string, string> = {
        WEBDAV_ROOT_URL: env.WEBDAV_ROOT_URL,
        WEBDAV_ROOT_PATH: env.WEBDAV_ROOT_PATH,
        WEBDAV_AUTH_ENABLED: "true",
      };
      if (username.trim()) patch.WEBDAV_USERNAME = username.trim();
      if (password.trim()) patch.WEBDAV_PASSWORD = password.trim();
      const updated = await api.updateServer(current.id, { env: patch });
      setCurrent(updated);
      return updated;
    }
    if (!env.WEBDAV_USERNAME || !env.WEBDAV_PASSWORD) {
      throw new Error("Username and app password are required");
    }
    const created = await api.createServer({
      name: app.name,
      slug: app.slug,
      kind: "stdio",
      command: "npx",
      args: webdavArgs.length ? webdavArgs : [...WEBDAV_MCP_ARGS],
      env,
      recipe_slug: app.slug,
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
      setPassword("");
      setUsername("");
      setNotice("Saved. Probe tools to refresh and go live for agents.");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <DrawerShell
      title={app.label}
      description={app.description}
      iconId={app.slug}
      onClose={onClose}
      current={current}
      active={active}
      configured={configured}
    >
      <form className="stack drawer-section" onSubmit={save}>
        <p className="provider-hint">Use an app password, not your account login.</p>
        <SetupHints hints={specStringList(app.spec, "setup_hints")} />
        <label className="field">
          Root URL
          <input value={rootUrl} readOnly autoComplete="off" />
        </label>
        <label className="field">
          Root path
          <input value={rootPath} onChange={(e) => setRootPath(e.target.value)} autoComplete="off" />
        </label>
        <label className="field">
          Username {active ? "(leave blank to keep)" : ""}
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required={!active}
            autoComplete="username"
            placeholder={active ? "••••••••" : "email@example.com"}
          />
        </label>
        <label className="field">
          App password {active ? "(leave blank to keep)" : ""}
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required={!active}
            autoComplete="current-password"
            placeholder={active ? "••••••••" : undefined}
          />
        </label>

        <div className="apps-actions">
          <button className="primary sm" type="submit" disabled={busy}>
            {current ? "Save" : "Connect"}
          </button>
        </div>

        <details className="apps-tech">
          <summary>Endpoint details</summary>
          <div className="provider-meta">
            <div>
              <span className="muted">Transport</span>
              <code>WebDAV · npx {WEBDAV_MCP_PACKAGE}</code>
            </div>
            {current && (
              <div>
                <span className="muted">Slug</span>
                <code>{current.slug}</code>
              </div>
            )}
          </div>
        </details>

        <ServerLifecycle
          current={current}
          busy={busy}
          setBusy={setBusy}
          setError={setError}
          setNotice={setNotice}
          setCurrent={setCurrent}
          onChanged={onChanged}
        />

        {notice && <div className="notice success">{notice}</div>}
        {error && <div className="notice error">{error}</div>}
      </form>
    </DrawerShell>
  );
}

function stdioEnvDefs(app: StdioApp) {
  return parseEnvRows(app.spec.env);
}

function stdioEnvDefaults(app: StdioApp): Record<string, string> {
  const raw = app.spec.env_defaults;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof k === "string" && k.trim() && typeof v === "string") out[k.trim()] = v;
  }
  return out;
}

function StdioManagePanel({
  app,
  server,
  onClose,
  onChanged,
}: {
  app: StdioApp;
  server: Server | null;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const secretDefs = stdioEnvDefs(app);
  const envDefaults = stdioEnvDefaults(app);
  const [values, setValues] = useState<Record<string, string>>({});
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [current, setCurrent] = useState<Server | null>(server);

  useEffect(() => {
    setCurrent(server);
    // Prefill non-secret fields that have catalog env_defaults (e.g. Infisical Host URL).
    const initial: Record<string, string> = {};
    for (const def of secretDefs) {
      if (!def.secret && envDefaults[def.name]) initial[def.name] = envDefaults[def.name];
    }
    setValues(initial);
    setRevealed({});
    setError("");
    setNotice("");
  }, [server, app]);

  const { active, configured } = appStatus(app, current);
  const command = specString(app.spec, "command") || "pikpak";
  const args = specStringList(app.spec, "args");

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const env: Record<string, string> = { ...envDefaults };
      for (const def of secretDefs) {
        const v = (values[def.name] || "").trim();
        if (v) env[def.name] = v;
      }
      if (current) {
        const missing = Object.fromEntries(
          Object.entries(envDefaults).filter(([k]) => !current.env_names.includes(k)),
        );
        const payload = { ...missing };
        for (const def of secretDefs) {
          if (env[def.name]) payload[def.name] = env[def.name];
        }
        if (!Object.keys(payload).length) {
          setNotice("Already connected. Enter a new value only when rotating.");
          return;
        }
        for (const def of secretDefs) {
          // Non-secret optional fields (e.g. Aliyun MCP Server URL) may stay empty.
          if (!def.secret) continue;
          if (!current.env_names.includes(def.name) && !payload[def.name]) {
            throw new Error(`${def.label} is required`);
          }
        }
        const updated = await api.updateServer(current.id, { env: payload });
        setCurrent(updated);
      } else {
        for (const def of secretDefs) {
          if (def.secret && !env[def.name]) throw new Error(`${def.label} is required`);
        }
        const created = await api.createServer({
          name: app.name,
          slug: app.slug,
          kind: "stdio",
          command,
          args,
          env,
          recipe_slug: app.slug,
        });
        setCurrent(created);
      }
      setValues({});
      setNotice("Saved. Probe to refresh tools and go live for agents.");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <DrawerShell
      title={app.label}
      description={app.description}
      iconId={app.slug}
      onClose={onClose}
      current={current}
      active={active}
      configured={configured}
    >
      <form className="stack drawer-section" onSubmit={save}>
        <p className="provider-hint">
          Stdio MCP: <code>{[command, ...args].join(" ")}</code>
        </p>
        <SetupHints hints={specStringList(app.spec, "setup_hints")} />
        <EnvFields
          defs={secretDefs}
          values={values}
          onChange={(name, value) => setValues((prev) => ({ ...prev, [name]: value }))}
          active={active}
          revealed={revealed}
          onReveal={(name) => setRevealed((prev) => ({ ...prev, [name]: true }))}
        />
        <div className="apps-actions">
          <button className="primary sm" type="submit" disabled={busy}>
            {current ? "Save" : "Connect"}
          </button>
        </div>
        <ServerLifecycle
          current={current}
          busy={busy}
          setBusy={setBusy}
          setError={setError}
          setNotice={setNotice}
          setCurrent={setCurrent}
          onChanged={onChanged}
        />
        {notice && <div className="notice success">{notice}</div>}
        {error && <div className="notice error">{error}</div>}
      </form>
    </DrawerShell>
  );
}

function HttpManagePanel({
  app,
  server,
  onClose,
  onChanged,
}: {
  app: HttpApp;
  server: Server | null;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [current, setCurrent] = useState<Server | null>(server);
  const [token, setToken] = useState("");
  const urlFromOperator = specBool(app.spec, "url_from_operator");
  const [mcpUrl, setMcpUrl] = useState(server?.url || "");

  useEffect(() => {
    setCurrent(server);
    setToken("");
    setMcpUrl(server?.url || "");
    setError("");
    setNotice("");
  }, [server, app]);

  const authHeader = specString(app.spec, "auth_header");
  const tokenPrefix = specString(app.spec, "token_prefix");
  const customHeader = !!authHeader;
  const active = customHeader
    ? !!current?.env_names.some((n) => n.toLowerCase() === authHeader.toLowerCase()) ||
      !!current?.has_auth
    : !!current?.has_auth;
  const configured = urlFromOperator ? !!(current?.url && active) : active;

  async function ensureServer(url: string): Promise<Server> {
    if (current) {
      if (urlFromOperator && url && url !== current.url) {
        const updated = await api.updateServer(current.id, { url });
        setCurrent(updated);
        return updated;
      }
      return current;
    }
    const created = await api.createServer({
      name: app.name,
      slug: app.slug,
      kind: "remote",
      url,
      recipe_slug: app.slug,
    });
    setCurrent(created);
    return created;
  }

  async function saveToken(e: FormEvent) {
    e.preventDefault();
    const url = urlFromOperator ? mcpUrl.trim() : specString(app.spec, "url");
    if (urlFromOperator && !url) {
      setError("MCP endpoint URL is required.");
      return;
    }
    if (!token.trim() && !active) return;
    if (!token.trim() && active && !(urlFromOperator && url && url !== current?.url)) {
      setNotice("Already connected. Enter a new token only when rotating.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const target = await ensureServer(url);
      let updated: Server = target;
      if (token.trim()) {
        const raw = token.trim();
        if (customHeader) {
          const value =
            tokenPrefix && !raw.toLowerCase().startsWith(tokenPrefix.toLowerCase())
              ? `${tokenPrefix}${raw}`
              : raw;
          updated = await api.updateServer(target.id, { env: { [authHeader]: value } });
        } else {
          updated = await api.setBearerToken(target.id, raw);
        }
      }
      setCurrent(updated);
      setToken("");
      setNotice(token.trim() ? "Saved. Probe tools to go live for agents." : "URL updated.");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const tokenLabel = customHeader
    ? `${authHeader} token${active ? " (replace)" : ""}`
    : `Bearer token${active ? " (replace)" : ""}`;
  const canSubmit = urlFromOperator
    ? !!mcpUrl.trim() && (!!token.trim() || active)
    : !!token.trim();

  return (
    <DrawerShell
      title={app.label}
      description={app.description}
      iconId={app.slug}
      onClose={onClose}
      current={current}
      active={active}
      configured={configured}
    >
      <form className="stack drawer-section" onSubmit={saveToken}>
        {urlFromOperator ? (
          <p className="provider-hint">
            Self-hosted MCP — paste the endpoint URL from your deployment (path looks like{" "}
            <code>/mcp/&lt;endpoint_id&gt;</code>).
          </p>
        ) : (
          <p className="provider-hint">
            MCP: <code>{specString(app.spec, "url")}</code>
            {customHeader ? (
              <>
                <br />
                Auth header: <code>{authHeader}</code>
                {tokenPrefix ? (
                  <>
                    {" "}
                    (<code>{tokenPrefix}…</code>)
                  </>
                ) : null}
              </>
            ) : null}
          </p>
        )}
        <SetupHints hints={specStringList(app.spec, "setup_hints")} />
        {urlFromOperator ? (
          <label className="field">
            MCP endpoint URL
            <input
              type="url"
              value={mcpUrl}
              onChange={(e) => setMcpUrl(e.target.value)}
              autoComplete="off"
              placeholder="https://your-host/mcp/<endpoint_id>"
              required
            />
          </label>
        ) : null}
        <label className="field">
          {tokenLabel}
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            autoComplete="off"
            placeholder={active ? "••••••••" : "Paste token"}
            required={!active}
          />
        </label>
        <div className="apps-actions">
          <button className="primary sm" type="submit" disabled={busy || !canSubmit}>
            {current ? "Save" : "Connect"}
          </button>
        </div>
        <ServerLifecycle
          current={current}
          busy={busy}
          setBusy={setBusy}
          setError={setError}
          setNotice={setNotice}
          setCurrent={setCurrent}
          onChanged={onChanged}
        />
        {notice && <div className="notice success">{notice}</div>}
        {error && <div className="notice error">{error}</div>}
      </form>
    </DrawerShell>
  );
}

function GenericManagePanel({
  server,
  onClose,
  onChanged,
}: {
  server: Server;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [current, setCurrent] = useState<Server | null>(server);
  const [name, setName] = useState(server.name);
  const [slug, setSlug] = useState(server.slug);
  const [command, setCommand] = useState(server.command || "npx");
  const [args, setArgs] = useState((server.args ?? []).join(" "));
  const [argsDirty, setArgsDirty] = useState(false);
  const [url, setUrl] = useState(server.url || "");
  const [token, setToken] = useState("");
  const custom = !server.recipe_slug;
  const argsHint = server.kind === "stdio" ? stdioArgsHint(command) : null;

  useEffect(() => {
    setCurrent(server);
    setName(server.name);
    setSlug(server.slug);
    setCommand(server.command || "npx");
    setArgs((server.args ?? []).join(" "));
    setArgsDirty(false);
    setUrl(server.url || "");
    setToken("");
    setError("");
    setNotice("");
  }, [server]);

  if (!current) return null;

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!current) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const patch: Parameters<typeof api.updateServer>[1] = {
        name: name.trim(),
      };
      if (custom) {
        patch.slug = slug.trim();
      }
      if (current.kind === "stdio" && custom) {
        const cmd = command.trim();
        const cmdChanged = cmd !== (current.command || "");
        // Only re-parse argv text when the operator edited it — join/split is lossy
        // for args that contain whitespace.
        if (argsDirty) {
          const { argv, error: argvError } = prepareStdioArgv(cmd, args);
          if (argvError) throw new Error(argvError);
          setArgs(argv.join(" "));
          patch.command = cmd;
          patch.args = argv;
        } else if (cmdChanged) {
          const argv = normalizeStdioArgs(cmd, current.args ?? []);
          const argvError = validateStdioArgs(cmd, argv);
          if (argvError) throw new Error(argvError);
          patch.command = cmd;
          patch.args = argv;
        }
      }
      if (current.kind === "remote" && custom) {
        const nextUrl = url.trim();
        if (nextUrl && nextUrl !== (current.url || "")) {
          patch.url = nextUrl;
        }
      }
      const updated = await api.updateServer(current.id, patch);
      setCurrent(updated);
      setName(updated.name);
      setSlug(updated.slug);
      setCommand(updated.command || "npx");
      setArgs((updated.args ?? []).join(" "));
      setArgsDirty(false);
      setUrl(updated.url || "");
      setNotice("Saved.");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function pasteToken(e: FormEvent) {
    e.preventDefault();
    if (!token.trim() || !current) return;
    setBusy(true);
    setError("");
    try {
      const updated = await api.setBearerToken(current.id, token.trim());
      setCurrent(updated);
      setToken("");
      setNotice("Token saved.");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const subtitle =
    current.kind === "remote" ? hostLabel(current.url) ?? "Remote MCP" : "Stdio MCP";

  return (
    <DrawerShell
      title={current.name}
      description={subtitle}
      iconId={current.slug}
      onClose={onClose}
      current={current}
      active={current.has_auth || current.env_names.length > 0}
      configured={current.has_auth || current.env_names.length > 0}
    >
      <form className="stack drawer-section" onSubmit={save}>
        <p className="provider-hint">
          {custom
            ? "Custom MCP — edit identity and endpoint, then Probe to refresh tools."
            : "Catalog MCP — name is editable; slug and argv stay bound to the recipe."}
        </p>
        <div className="form-grid">
          <label className="field">
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} required />
          </label>
          <label className="field">
            Slug
            <input
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              required
              readOnly={!custom}
              disabled={!custom}
            />
          </label>
        </div>
        {current.kind === "stdio" ? (
          <>
            <div className="row">
              <label className="field command-field">
                Command
                <input
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  required
                  readOnly={!custom}
                  disabled={!custom}
                />
              </label>
              <label className="field grow">
                Args
                <input
                  value={args}
                  onChange={(e) => {
                    setArgsDirty(true);
                    setArgs(e.target.value);
                  }}
                  placeholder="-y --ignore-scripts pkg@1.2.3"
                  readOnly={!custom}
                  disabled={!custom}
                />
              </label>
            </div>
            {custom && argsHint ? <p className="provider-hint">{argsHint}</p> : null}
          </>
        ) : (
          <label className="field">
            MCP URL
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
              readOnly={!custom}
              disabled={!custom}
              autoComplete="off"
            />
          </label>
        )}
        <div className="apps-actions">
          <button className="primary sm" type="submit" disabled={busy}>
            Save
          </button>
        </div>
      </form>

      {current.kind === "remote" && (
        <form className="stack drawer-section" onSubmit={pasteToken}>
          <label className="field">
            Bearer token {current.has_auth ? "(replace)" : ""}
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="off"
              placeholder={current.has_auth ? "••••••••" : "Paste token"}
            />
          </label>
          <div className="apps-actions">
            <button className="outline sm" type="submit" disabled={busy || !token.trim()}>
              Save token
            </button>
          </div>
        </form>
      )}

      {current.env_names.length > 0 && (
        <details className="apps-tech drawer-section">
          <summary>Vault</summary>
          <div className="provider-meta">
            <div>
              <span className="muted">Names</span>
              <code>{current.env_names.join(", ")}</code>
            </div>
          </div>
        </details>
      )}

      <div className="drawer-section">
        <ServerLifecycle
          current={current}
          busy={busy}
          setBusy={setBusy}
          setError={setError}
          setNotice={setNotice}
          setCurrent={setCurrent}
          onChanged={onChanged}
        />
        {notice && <div className="notice success">{notice}</div>}
        {error && <div className="notice error">{error}</div>}
      </div>
    </DrawerShell>
  );
}

function DrawerShell({
  title,
  description,
  iconId,
  onClose,
  current,
  active,
  configured,
  children,
}: {
  title: string;
  description: string;
  iconId: string;
  onClose: () => void;
  current: Server | null;
  active: boolean;
  configured: boolean;
  children: ReactNode;
}) {
  const paused = !!current && !current.enabled;
  let statusLabel = "Not connected";
  let statusKind: "" | "ok" | "warn" = "";
  if (paused && (active || configured)) {
    statusLabel = "Paused";
    statusKind = "warn";
  } else if (active) {
    statusLabel = "Live";
    statusKind = "ok";
  } else if (configured) {
    statusLabel = "Needs auth";
    statusKind = "warn";
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="apps-drawer-backdrop" role="presentation" onClick={onClose}>
      <aside
        className="apps-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="app-drawer-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="apps-drawer-header">
          <AppBrandMark id={iconId} label={title} />
          <div className="apps-drawer-heading">
            <p className="drawer-kicker">App connection</p>
            <div className="apps-drawer-title-row">
              <h2 id="app-drawer-title">{title}</h2>
              <span className={`app-pill ${statusKind}`}>{statusLabel}</span>
            </div>
            <p className="desc">{description}</p>
          </div>
          <button type="button" className="ghost sm apps-drawer-close" onClick={onClose} aria-label="Close app details">
            <Icon name="close" size={15} />
          </button>
        </div>

        {children}

        {current ? <ServerToolsPanel serverId={current.id} /> : null}
      </aside>
    </div>
  );
}

function ServerToolsPanel({ serverId }: { serverId: string }) {
  const [tools, setTools] = useState<Tool[] | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [busyIds, setBusyIds] = useState<Record<string, boolean>>({});
  const [overrideDraft, setOverrideDraft] = useState<Record<string, string>>({});
  const [rowError, setRowError] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const list = await api.tools(serverId);
      setTools(list);
      setError("");
    } catch (e) {
      setTools([]);
      setError((e as Error).message);
    }
  }, [serverId]);

  useEffect(() => {
    setTools(null);
    setExpanded({});
    setOverrideDraft({});
    setBusyIds({});
    setRowError({});
    void load();
  }, [load]);

  function setBusy(id: string, on: boolean) {
    setBusyIds((m) => {
      if (on) return { ...m, [id]: true };
      const next = { ...m };
      delete next[id];
      return next;
    });
  }

  function clearDraft(id: string) {
    setOverrideDraft((m) => {
      const next = { ...m };
      delete next[id];
      return next;
    });
  }

  function clearRowError(id: string) {
    setRowError((m) => {
      const next = { ...m };
      delete next[id];
      return next;
    });
  }

  function patchTool(updated: Tool) {
    setTools((prev) =>
      prev
        ? prev.map((t) =>
            t.id === updated.id
              ? {
                  ...t,
                  ...updated,
                  // tool_view omits these when no override — clear stale client state.
                  schema_override: updated.schema_override ?? null,
                  probed_schema: updated.probed_schema ?? null,
                }
              : t,
          )
        : prev,
    );
  }

  async function toggleTool(tool: Tool) {
    setBusy(tool.id, true);
    setError("");
    clearRowError(tool.id);
    try {
      const updated = await api.setToolEnabled(tool.id, !tool.enabled);
      patchTool(updated);
      window.dispatchEvent(new Event("portunus-refresh"));
    } catch (e) {
      setRowError((m) => ({ ...m, [tool.id]: (e as Error).message }));
    } finally {
      setBusy(tool.id, false);
    }
  }

  async function saveOverride(tool: Tool) {
    const raw = overrideDraft[tool.id];
    if (raw === undefined) {
      setRowError((m) => ({
        ...m,
        [tool.id]: "Edit the schema before saving an override",
      }));
      return;
    }
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(raw) as Record<string, unknown>;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("schema must be a JSON object");
      }
    } catch (e) {
      const msg = (e as Error).message || "invalid JSON";
      setRowError((m) => ({ ...m, [tool.id]: `Invalid JSON schema — ${msg}` }));
      return;
    }
    setBusy(tool.id, true);
    setError("");
    clearRowError(tool.id);
    try {
      const updated = await api.setToolSchemaOverride(tool.id, parsed);
      patchTool(updated);
      clearDraft(tool.id);
    } catch (e) {
      setRowError((m) => ({ ...m, [tool.id]: (e as Error).message }));
    } finally {
      setBusy(tool.id, false);
    }
  }

  async function clearOverride(tool: Tool) {
    setBusy(tool.id, true);
    setError("");
    clearRowError(tool.id);
    try {
      const updated = await api.clearToolSchemaOverride(tool.id);
      patchTool(updated);
      clearDraft(tool.id);
    } catch (e) {
      setRowError((m) => ({ ...m, [tool.id]: (e as Error).message }));
    } finally {
      setBusy(tool.id, false);
    }
  }

  const count = tools?.length ?? 0;

  return (
    <section className="drawer-section server-tools">
      <div className="server-tools-head">
        <h3 className="apps-section-title">
          Tools
          <span className="apps-tab-count">{tools === null ? "…" : count}</span>
        </h3>
        <button type="button" className="ghost sm" onClick={() => void load()} disabled={tools === null}>
          <Icon name="refresh" size={13} />
          Refresh
        </button>
      </div>
      {error && <div className="notice error">{error}</div>}
      {tools === null ? (
        <p className="provider-hint">Loading tools…</p>
      ) : count === 0 ? (
        <p className="provider-hint">No tools yet — Probe to discover the catalog.</p>
      ) : (
        <ul className="tools-list drawer-tools-list">
          {tools.map((t) => {
            const open = !!expanded[t.id];
            const baseline = open
              ? JSON.stringify(t.schema_override ?? t.schema ?? {}, null, 2)
              : "";
            const draft = open ? (overrideDraft[t.id] ?? baseline) : "";
            const dirty =
              open &&
              overrideDraft[t.id] !== undefined &&
              overrideDraft[t.id] !== baseline;
            const busy = !!busyIds[t.id];
            return (
              <li key={t.id} className={!t.enabled ? "disabled" : undefined}>
                <button
                  type="button"
                  className={`tools-row-toggle${open ? " open" : ""}`}
                  aria-expanded={open}
                  onClick={() => setExpanded((m) => ({ ...m, [t.id]: !open }))}
                >
                  <code className="tools-list-name">{t.name}</code>
                  <span className="tools-list-desc">
                    {t.description?.trim() || "No description"}
                  </span>
                  <span className="tools-row-trailing">
                    {t.schema_overridden ? (
                      <span className="muted mono-chip" title="Operator schema override">
                        Overridden
                      </span>
                    ) : null}
                    <Icon name="chevron-down" size={13} className="tools-toggle-chev" />
                  </span>
                </button>
                {open && (
                  <div className="tools-row-detail">
                    <div className="tools-row-actions">
                      <button
                        type="button"
                        className="outline sm"
                        disabled={busy}
                        onClick={() => void toggleTool(t)}
                      >
                        {t.enabled ? "Disable for agents" : "Enable for agents"}
                      </button>
                      {t.schema_hash ? (
                        <span className="muted mono-chip" title={t.schema_hash}>
                          schema {t.schema_hash.slice(0, 10)}…
                        </span>
                      ) : null}
                    </div>
                    <p className="provider-hint">
                      Effective schema (agents validate against this). Paste a JSON Schema
                      object to override a drifted upstream contract; probe will not clear it.
                    </p>
                    <textarea
                      className="json-editor-input tools-schema-edit"
                      rows={10}
                      spellCheck={false}
                      aria-label={`${t.name} schema override`}
                      aria-invalid={rowError[t.id] ? true : undefined}
                      value={draft}
                      disabled={busy}
                      onChange={(e) =>
                        setOverrideDraft((m) => ({ ...m, [t.id]: e.target.value }))
                      }
                    />
                    {rowError[t.id] ? (
                      <p className="json-editor-error">{rowError[t.id]}</p>
                    ) : null}
                    <div className="tools-row-actions">
                      <button
                        type="button"
                        className="outline sm"
                        disabled={busy || !dirty}
                        onClick={() => void saveOverride(t)}
                      >
                        Save schema override
                      </button>
                      {t.schema_overridden ? (
                        <button
                          type="button"
                          className="ghost sm"
                          disabled={busy}
                          onClick={() => void clearOverride(t)}
                        >
                          Clear override
                        </button>
                      ) : null}
                    </div>
                    {t.schema_overridden && t.probed_schema ? (
                      <>
                        <p className="provider-hint">Last probed upstream schema</p>
                        <pre className="tools-schema">
                          {JSON.stringify(t.probed_schema, null, 2)}
                        </pre>
                      </>
                    ) : null}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function ServerLifecycle({
  current,
  busy,
  setBusy,
  setError,
  setNotice,
  setCurrent,
  onChanged,
}: {
  current: Server | null;
  busy: boolean;
  setBusy: (v: boolean) => void;
  setError: (v: string) => void;
  setNotice: (v: string) => void;
  setCurrent: (s: Server | null) => void;
  onChanged: () => Promise<void>;
}) {
  if (!current) return null;

  const routing = regionsToChoice(current.preferred_regions);
  const customRegions = routing === null;

  async function probe() {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      await api.probe(current!.id);
      // Successful probe is the readiness check: enable so agents can see the server.
      // Without this, a probed+granted but still-disabled server looks identical to
      // "not granted" on the agent path (review #2).
      let enabled = current!.enabled;
      if (!enabled) {
        const updated = await api.setServerEnabled(current!.id, true);
        setCurrent(updated);
        enabled = updated.enabled;
      }
      setNotice(enabled ? "Catalog refreshed · live for agents." : "Catalog refreshed.");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleEnabled() {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const updated = await api.setServerEnabled(current!.id, !current!.enabled);
      setCurrent(updated);
      setNotice(updated.enabled ? "Live for agents." : "Paused — hidden from agents.");
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function setRouting(choice: RoutingChoice) {
    if (routing !== null && choice === routing) return;
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const updated = await api.updateServer(current!.id, {
        preferred_regions: choiceToRegions(choice),
      });
      setCurrent(updated);
      setNotice(
        choice === "any"
          ? "Routing: any healthy runner."
          : `Routing: prefer ${choice} runners.`,
      );
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-lifecycle">
      {!current.enabled && (
        <p className="provider-hint">Paused apps are invisible to agents. Probe to go live, or resume.</p>
      )}
      <div className="stack">
        <p className="provider-hint">Runner preference</p>
        <div className="segment" role="tablist" aria-label="Runner preference">
          {ROUTING_CHOICES.map((c) => (
            <button
              key={c.id}
              type="button"
              aria-selected={routing === c.id}
              disabled={busy}
              onClick={() => void setRouting(c.id)}
            >
              {c.label}
            </button>
          ))}
        </div>
        <p className="provider-hint">
          {customRegions ? (
            <>
              Custom region tags (<code>{(current.preferred_regions ?? []).join(", ")}</code>
              ). Pick a choice above to replace them.
            </>
          ) : (
            <>
              Soft affinity among healthy sites. Tag runners with{" "}
              <code>PORTUNUS_REGIONS</code> including <code>domestic</code> or{" "}
              <code>overseas</code>.
            </>
          )}
        </p>
      </div>
      <div className="apps-actions">
        <button type="button" className="outline sm" disabled={busy} onClick={() => void probe()}>
          Probe
        </button>
        <button
          type="button"
          className="outline sm"
          disabled={busy}
          onClick={() => void toggleEnabled()}
        >
          {current.enabled ? "Pause" : "Resume"}
        </button>
      </div>
    </div>
  );
}
