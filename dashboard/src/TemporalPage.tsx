import { useCallback, useEffect, useState } from "react";
import {
  api,
  type TemporalStatus,
  type TemporalWorkflowDetail,
  type TemporalWorkflowSummary,
} from "./api";
import { fmtTimestamp } from "./format";
import { Icon } from "./icons/ui";

function statusKind(status: string | null | undefined): "ok" | "warn" | "bad" | "" {
  if (!status) return "";
  const u = status.toUpperCase();
  if (u.includes("COMPLETED") || u.includes("RUNNING")) return u.includes("RUNNING") ? "ok" : "ok";
  if (u.includes("FAILED") || u.includes("TERMINATED") || u.includes("TIMED")) return "bad";
  if (u.includes("CANCELED") || u.includes("CONTINUED")) return "warn";
  return "";
}

function statusLabel(status: string | null | undefined): string {
  if (!status) return "Unknown";
  return status.replace(/^WORKFLOW_EXECUTION_STATUS_/, "").replace(/_/g, " ");
}

export function TemporalPage() {
  const [status, setStatus] = useState<TemporalStatus | null>(null);
  const [items, setItems] = useState<TemporalWorkflowSummary[]>([]);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<TemporalWorkflowDetail | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [syncBusy, setSyncBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [st, list] = await Promise.all([
        api.temporalStatus(),
        api.temporalWorkflows({ query: appliedQuery || undefined, page_size: 50 }),
      ]);
      setStatus(st);
      setItems(list.items ?? []);
      setListError(list.error ?? null);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load Temporal");
    }
  }, [appliedQuery]);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 15000);
    return () => window.clearInterval(id);
  }, [load]);

  async function openDetail(row: TemporalWorkflowSummary) {
    if (!row.workflow_id) return;
    setDetailBusy(true);
    setSelected(null);
    try {
      const d = await api.temporalWorkflow(row.workflow_id, {
        run_id: row.run_id ?? undefined,
      });
      setSelected(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load workflow");
    } finally {
      setDetailBusy(false);
    }
  }

  async function syncFleet() {
    setSyncBusy(true);
    setNotice(null);
    try {
      const r = await api.fleetSkillsSync();
      setNotice(`Started fleet sync · ${r.workflow_id}`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Fleet sync failed");
    } finally {
      setSyncBusy(false);
    }
  }

  const enabled = Boolean(status?.enabled) && !status?.error;
  const running = items.filter((i) => (i.status || "").toUpperCase().includes("RUNNING")).length;
  const failed = items.filter((i) => {
    const u = (i.status || "").toUpperCase();
    return u.includes("FAILED") || u.includes("TIMED") || u.includes("TERMINATED");
  }).length;

  return (
    <div className="temporal-page">
      {error && <div className="notice error">{error}</div>}
      {notice && <div className="notice success">{notice}</div>}

      <section className="fleet-strip temporal-strip" aria-label="Temporal control plane">
        <div>
          <span className="fleet-label">Control plane</span>
          <strong>
            {status == null
              ? "Checking…"
              : enabled
                ? "Connected"
                : status.error
                  ? "Unreachable"
                  : "Not configured"}
          </strong>
        </div>
        <div>
          <span className="fleet-label">Namespace</span>
          <span className="mono">{status?.namespace ?? "—"}</span>
        </div>
        <div>
          <span className="fleet-label">Task queue</span>
          <span className="mono">{status?.task_queue ?? "—"}</span>
        </div>
        <div>
          <span className="fleet-label">Host</span>
          <span className="mono" title={status?.host ?? undefined}>
            {status?.host ?? "—"}
          </span>
        </div>
      </section>

      {!enabled && status && (
        <div className="card temporal-empty">
          <div className="card-body">
            <p className="section-kicker">Temporal</p>
            <h2>Control plane offline</h2>
            <p className="lede muted">
              {status.error
                ? status.error
                : "Set TEMPORAL_HOST on the gateway and start the temporal compose profile to list workflows here."}
            </p>
          </div>
        </div>
      )}

      {enabled && (
        <>
          <div className="temporal-toolbar">
            <form
              className="temporal-search"
              onSubmit={(e) => {
                e.preventDefault();
                setAppliedQuery(query.trim());
              }}
            >
              <label className="sr-only" htmlFor="temporal-query">
                Visibility query
              </label>
              <input
                id="temporal-query"
                placeholder='Visibility query (default: TaskQueue = "portunus-control")'
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              <button type="submit" className="outline sm">
                Apply
              </button>
            </form>
            <div className="temporal-toolbar-actions">
              <button type="button" className="outline sm" onClick={() => void load()}>
                <Icon name="refresh" size={14} />
                Refresh
              </button>
              <button
                type="button"
                className="primary sm"
                disabled={syncBusy}
                onClick={() => void syncFleet()}
              >
                Sync fleet skills
              </button>
            </div>
          </div>

          <div className="temporal-counts" aria-label="Workflow counts">
            <div>
              <span className="fleet-label">Listed</span>
              <strong className="mono">{items.length}</strong>
            </div>
            <div>
              <span className="fleet-label">Running</span>
              <strong className="mono">{running}</strong>
            </div>
            <div>
              <span className="fleet-label">Failed</span>
              <strong className="mono">{failed}</strong>
            </div>
          </div>

          {listError && <div className="notice warn">{listError}</div>}

          <section className="card fleet-section">
            <div className="card-header">
              <div>
                <p className="section-kicker">Workflows</p>
                <h2>Durable control-plane runs</h2>
              </div>
            </div>
            <div className="card-body">
              <div className="table-wrap">
                <table className="flush">
                  <thead>
                    <tr>
                      <th>Workflow</th>
                      <th>Type</th>
                      <th>Status</th>
                      <th>Started</th>
                      <th>Closed</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {items.length === 0 ? (
                      <tr>
                        <td colSpan={6} className="muted">
                          No workflows on this queue yet
                        </td>
                      </tr>
                    ) : (
                      items.map((row) => (
                        <tr key={`${row.workflow_id}:${row.run_id}`}>
                          <td data-label="Workflow">
                            <strong className="mono" title={row.workflow_id ?? undefined}>
                              {row.workflow_id}
                            </strong>
                          </td>
                          <td data-label="Type" className="mono">
                            {row.workflow_type ?? "—"}
                          </td>
                          <td data-label="Status">
                            <span className={`health on-card ${statusKind(row.status)}`}>
                              <span className="dot" aria-hidden />
                              {statusLabel(row.status)}
                            </span>
                          </td>
                          <td data-label="Started">{fmtTimestamp(row.start_time)}</td>
                          <td data-label="Closed">{fmtTimestamp(row.close_time)}</td>
                          <td data-label="">
                            <button
                              type="button"
                              className="ghost sm"
                              onClick={() => void openDetail(row)}
                            >
                              Inspect
                            </button>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </section>
        </>
      )}

      {(detailBusy || selected) && (
        <div
          className="apps-drawer-backdrop"
          role="presentation"
          onClick={() => {
            if (!detailBusy) setSelected(null);
          }}
        >
          <aside
            className="apps-drawer temporal-drawer"
            role="dialog"
            aria-label="Workflow detail"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="apps-drawer-header">
                <div className="apps-drawer-heading">
                  <p className="drawer-kicker">Workflow</p>
                <div className="apps-drawer-title-row">
                  <h2 className="mono" title={selected?.workflow.workflow_id ?? undefined}>
                    {detailBusy ? "Loading…" : selected?.workflow.workflow_id}
                  </h2>
                </div>
                {!detailBusy && selected?.workflow.workflow_type ? (
                  <p className="desc mono">{selected.workflow.workflow_type}</p>
                ) : null}
              </div>
              <button
                type="button"
                className="ghost sm apps-drawer-close"
                aria-label="Close workflow details"
                onClick={() => setSelected(null)}
              >
                <Icon name="close" size={16} />
              </button>
            </div>
            <div className="stack drawer-section temporal-drawer-body">
              {detailBusy && <p className="muted">Loading history…</p>}
              {selected && !detailBusy && (
                <>
                  <dl className="temporal-meta">
                    <div>
                      <dt>Status</dt>
                      <dd>
                        <span className={`health ${statusKind(selected.workflow.status)}`}>
                          <span className="dot" aria-hidden />
                          {statusLabel(selected.workflow.status)}
                        </span>
                      </dd>
                    </div>
                    <div>
                      <dt>Query status</dt>
                      <dd className="mono">{selected.query_status ?? "—"}</dd>
                    </div>
                    <div>
                      <dt>Run ID</dt>
                      <dd className="mono" title={selected.workflow.run_id ?? undefined}>
                        {selected.workflow.run_id ?? "—"}
                      </dd>
                    </div>
                    <div>
                      <dt>Started</dt>
                      <dd>{fmtTimestamp(selected.workflow.start_time)}</dd>
                    </div>
                    <div>
                      <dt>Closed</dt>
                      <dd>{fmtTimestamp(selected.workflow.close_time)}</dd>
                    </div>
                  </dl>
                  {selected.failure && <div className="notice error">{selected.failure}</div>}
                  {selected.error && <div className="notice warn">{selected.error}</div>}
                  <p className="section-kicker">History</p>
                  <ol className="temporal-events">
                    {selected.events.length === 0 ? (
                      <li className="muted">No events</li>
                    ) : (
                      selected.events.map((ev) => (
                        <li key={String(ev.event_id)}>
                          <span className="mono temporal-event-id" title={`Event #${ev.event_id}`}>
                            {ev.event_id}
                          </span>
                          <span className="temporal-event-type">
                            {ev.event_type || "Unknown"}
                            {ev.summary ? <span className="muted temporal-event-summary"> · {ev.summary}</span> : null}
                          </span>
                          <span className="muted mono">{fmtTimestamp(ev.event_time)}</span>
                        </li>
                      ))
                    )}
                  </ol>
                </>
              )}
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
