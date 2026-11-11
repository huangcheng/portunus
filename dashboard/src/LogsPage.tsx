import { Fragment, useCallback, useEffect, useState } from "react";
import { api, type AuditEvent, type ToolCallRow } from "./api";
import { fmtTimestamp, traceHref } from "./format";

type Tab = "audit" | "calls";

export function LogsPage() {
  const [tab, setTab] = useState<Tab>("audit");
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [calls, setCalls] = useState<ToolCallRow[]>([]);
  const [auditNext, setAuditNext] = useState<string | null>(null);
  const [callsNext, setCallsNext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [traceTemplate, setTraceTemplate] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  useEffect(() => {
    api
      .fleet()
      .then((f) => setTraceTemplate(f.gateway.trace_url_template ?? null))
      .catch(() => setTraceTemplate(null));
  }, []);

  const load = useCallback(
    async (opts?: { append?: boolean }) => {
      const append = Boolean(opts?.append);
      try {
        if (tab === "audit") {
          const r = await api.audit({ limit: 50, before: append ? auditNext ?? undefined : undefined });
          setAudit((prev) => (append ? [...prev, ...r.items] : r.items));
          setAuditNext(r.next);
        } else {
          const r = await api.toolCalls({ limit: 50, before: append ? callsNext ?? undefined : undefined });
          setCalls((prev) => (append ? [...prev, ...r.items] : r.items));
          setCallsNext(r.next);
        }
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load logs");
      }
    },
    [tab, auditNext, callsNext],
  );

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refresh when tab changes; not when cursors change
  }, [tab]);

  const next = tab === "audit" ? auditNext : callsNext;

  async function copyTrace(id: string, traceId: string) {
    try {
      await navigator.clipboard.writeText(traceId);
      setCopiedId(id);
      window.setTimeout(() => setCopiedId(null), 1500);
    } catch {
      /* clipboard may be denied */
    }
  }

  return (
    <div className="logs-page">
      <div className="logs-toolbar">
        <div className="logs-tabs" role="tablist" aria-label="Log views">
          <button
            type="button"
            role="tab"
            id="logs-tab-audit"
            aria-controls="logs-panel-audit"
            aria-selected={tab === "audit"}
            className={tab === "audit" ? "active" : ""}
            onClick={() => setTab("audit")}
          >
            Audit
          </button>
          <button
            type="button"
            role="tab"
            id="logs-tab-calls"
            aria-controls="logs-panel-calls"
            aria-selected={tab === "calls"}
            className={tab === "calls" ? "active" : ""}
            onClick={() => setTab("calls")}
          >
            Tool calls
          </button>
        </div>
        <button type="button" className="outline sm" onClick={() => void load()}>
          Refresh
        </button>
      </div>
      {error && <div className="notice error">{error}</div>}

      {tab === "audit" ? (
        <div className="card" role="tabpanel" id="logs-panel-audit" aria-labelledby="logs-tab-audit">
          <div className="card-body">
            <div className="table-wrap">
              <table className="flush">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Actor</th>
                    <th>Action</th>
                    <th>Target</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="muted">
                        No audit events
                      </td>
                    </tr>
                  ) : (
                    audit.map((r) => (
                      <tr key={r.id}>
                        <td data-label="Time" className="mono">
                          {fmtTimestamp(r.created_at)}
                        </td>
                        <td data-label="Actor">{r.actor}</td>
                        <td data-label="Action">
                          <code>{r.action}</code>
                        </td>
                        <td data-label="Target" className="mono">
                          {r.target ?? "—"}
                        </td>
                        <td data-label="Detail" className="mono muted">
                          {r.detail ? JSON.stringify(r.detail).slice(0, 120) : "—"}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      ) : (
        <div className="card" role="tabpanel" id="logs-panel-calls" aria-labelledby="logs-tab-calls">
          <div className="card-body">
            <div className="table-wrap">
              <table className="flush">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Server</th>
                    <th>Tool</th>
                    <th>Outcome</th>
                    <th>Total</th>
                    <th>Trace</th>
                  </tr>
                </thead>
                <tbody>
                  {calls.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="muted">
                        No tool calls recorded yet
                      </td>
                    </tr>
                  ) : (
                    calls.map((r) => {
                      const open = expanded === r.id;
                      const link = traceTemplate ? traceHref(traceTemplate, r.trace_id) : null;
                      return (
                        <Fragment key={r.id}>
                          <tr
                            className="logs-row-toggle"
                            tabIndex={0}
                            role="button"
                            aria-expanded={open}
                            onClick={() => setExpanded(open ? null : r.id)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault();
                                setExpanded(open ? null : r.id);
                              }
                            }}
                          >
                            <td data-label="Time" className="mono">
                              {fmtTimestamp(r.created_at)}
                            </td>
                            <td data-label="Server">{r.server_slug}</td>
                            <td data-label="Tool">
                              <code>{r.tool}</code>
                            </td>
                            <td data-label="Outcome">
                              <span className={`app-pill ${r.outcome === "ok" ? "ok" : "warn"}`}>{r.outcome}</span>
                            </td>
                            <td data-label="Total" className="mono">
                              {r.gateway_ms != null ? `${r.gateway_ms} ms` : "—"}
                            </td>
                            <td data-label="Trace" className="mono">
                              {link ? (
                                <a href={link} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
                                  {r.trace_id.slice(0, 12)}…
                                </a>
                              ) : (
                                <button
                                  type="button"
                                  className="ghost sm"
                                  title="Copy trace id"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    void copyTrace(r.id, r.trace_id);
                                  }}
                                >
                                  {copiedId === r.id ? "Copied" : `${r.trace_id.slice(0, 12)}…`}
                                </button>
                              )}
                            </td>
                          </tr>
                          {open && (
                            <tr>
                              <td colSpan={6}>
                                <div className="logs-timing">
                                  <span>gateway {r.gateway_ms ?? "—"} ms</span>
                                  <span>start {r.start_ms ?? "—"} ms</span>
                                  <span>queue {r.queue_ms ?? "—"} ms</span>
                                  <span>init {r.init_ms ?? "—"} ms</span>
                                  <span>upstream {r.upstream_ms ?? "—"} ms</span>
                                  <span>site {r.site_id ?? "—"}</span>
                                  <span>cold_start {String(r.cold_start ?? "—")}</span>
                                  <span>preferred_miss {String(r.preferred_miss ?? "—")}</span>
                                  <span>result_bytes {r.result_bytes ?? "—"}</span>
                                </div>
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
      {next && (
        <button type="button" className="outline sm" onClick={() => void load({ append: true })}>
          Load more
        </button>
      )}
    </div>
  );
}
