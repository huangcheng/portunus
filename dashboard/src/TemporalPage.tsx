import { useCallback, useEffect, useState } from "react";
import {
  api,
  type TemporalStatus,
  type TemporalWorkflowDetail,
  type TemporalWorkflowSummary,
  type TemporalWorkflowTask,
} from "./api";
import { fmtTimestamp } from "./format";
import { Icon } from "./icons/ui";

type StatusFilter = "running" | "failed" | "all";

const PAGE_SIZE = 50;

function statusKind(status: string | null | undefined): "ok" | "warn" | "bad" | "" {
  if (!status) return "";
  const u = status.toUpperCase();
  if (u.includes("COMPLETED") || u.includes("RUNNING")) return "ok";
  if (u.includes("FAILED") || u.includes("TERMINATED") || u.includes("TIMED")) return "bad";
  if (u.includes("CANCELED") || u.includes("CONTINUED")) return "warn";
  return "";
}

function statusLabel(status: string | null | undefined): string {
  if (!status) return "Unknown";
  return status.replace(/^WORKFLOW_EXECUTION_STATUS_/, "").replace(/_/g, " ");
}

/** Operator-facing names for always-on control-plane workflows. */
function workflowLabel(id: string | null | undefined): string {
  if (!id) return "—";
  if (id === "fleet-providers-update") return "fleet sync";
  if (id === "server-probe") return "server probe";
  if (id === "oauth-refresh") return "oauth refresh";
  if (id.startsWith("fleet-skills-sync:")) return "fleet skills (one-shot)";
  return id;
}

function taskStatusKind(status: string | null | undefined): "ok" | "warn" | "bad" | "" {
  if (!status) return "";
  const u = status.toLowerCase();
  if (u === "completed" || u === "running") return "ok";
  if (u === "failed" || u === "timed_out") return "bad";
  if (u === "canceled" || u === "scheduled") return "warn";
  return "";
}

function taskStatusLabel(status: string | null | undefined): string {
  if (!status) return "Unknown";
  return status
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function taskWhen(task: TemporalWorkflowTask): string | null {
  return task.completed_at || task.started_at || task.scheduled_at;
}

function statsLine(stats: Record<string, unknown> | null | undefined): string | null {
  if (!stats) return null;
  const bits: string[] = [];
  if (typeof stats.last_due === "number") bits.push(`${stats.last_due} due`);
  if (typeof stats.last_failed === "number") bits.push(`${stats.last_failed} failed`);
  if (typeof stats.paused === "boolean") bits.push(stats.paused ? "paused" : "active");
  if (Array.isArray(stats.priority) && stats.priority.length) {
    bits.push(`${stats.priority.length} priority`);
  }
  return bits.length ? bits.join(" · ") : null;
}

function buildQuery(filter: StatusFilter, custom: string): string {
  const trimmed = custom.trim();
  if (trimmed) return trimmed;
  const queue = 'TaskQueue = "portunus-control"';
  if (filter === "running") return `${queue} AND ExecutionStatus = "Running"`;
  if (filter === "failed") {
    return (
      `${queue} AND (ExecutionStatus = "Failed" OR ExecutionStatus = "TimedOut" ` +
      `OR ExecutionStatus = "Terminated")`
    );
  }
  return queue;
}

export function TemporalPage() {
  const [status, setStatus] = useState<TemporalStatus | null>(null);
  const [items, setItems] = useState<TemporalWorkflowSummary[]>([]);
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [filter, setFilter] = useState<StatusFilter>("running");
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<TemporalWorkflowDetail | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [syncBusy, setSyncBusy] = useState(false);
  const [loadMoreBusy, setLoadMoreBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const visibilityQuery = buildQuery(filter, appliedQuery);

  const load = useCallback(
    async (opts?: { append?: boolean; pageToken?: string | null }) => {
      const append = Boolean(opts?.append);
      try {
        const [st, list] = await Promise.all([
          api.temporalStatus(),
          api.temporalWorkflows({
            query: visibilityQuery,
            page_size: PAGE_SIZE,
            page_token: opts?.pageToken || undefined,
          }),
        ]);
        setStatus(st);
        setItems((prev) => (append ? [...prev, ...(list.items ?? [])] : (list.items ?? [])));
        setNextToken(list.next_page_token ?? null);
        setListError(list.error ?? null);
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load Temporal");
      }
    },
    [visibilityQuery],
  );

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

  async function syncSkillsOnly() {
    setSyncBusy(true);
    setNotice(null);
    try {
      const r = await api.fleetSkillsSync();
      setNotice(`Started fleet skills sync · ${r.workflow_id}`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Fleet skills sync failed");
    } finally {
      setSyncBusy(false);
    }
  }

  async function syncFleet() {
    setSyncBusy(true);
    setNotice(null);
    try {
      const r = await api.fleetProvidersUpdate();
      setNotice(`Kicked fleet sync · always-on: ${r.workflow_id}`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Fleet sync failed");
    } finally {
      setSyncBusy(false);
    }
  }

  async function loadMore() {
    if (!nextToken) return;
    setLoadMoreBusy(true);
    try {
      await load({ append: true, pageToken: nextToken });
    } finally {
      setLoadMoreBusy(false);
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
            <div className="temporal-filters" role="tablist" aria-label="Workflow status filter">
              {(
                [
                  ["running", "Running"],
                  ["failed", "Failed"],
                  ["all", "All"],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={filter === id}
                  className={filter === id ? "active" : ""}
                  onClick={() => {
                    setFilter(id);
                    setAppliedQuery("");
                    setQuery("");
                    setNextToken(null);
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
            <form
              className="temporal-search"
              onSubmit={(e) => {
                e.preventDefault();
                setAppliedQuery(query.trim());
                setNextToken(null);
              }}
            >
              <label className="sr-only" htmlFor="temporal-query">
                Visibility query
              </label>
              <input
                id="temporal-query"
                placeholder="Custom Visibility query (overrides filter)"
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
                className="outline sm"
                disabled={syncBusy}
                onClick={() => void syncSkillsOnly()}
                title="Rsync skill trees only (legacy one-shot)"
              >
                Sync skills
              </button>
              <button
                type="button"
                className="primary sm"
                disabled={syncBusy}
                onClick={() => void syncFleet()}
                title="Full fleet sync: rclone, skill trees, vendor CLIs from manifests, skill_bridge"
              >
                Sync fleet
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
          {filter === "running" && !appliedQuery && (
            <p className="muted temporal-filter-hint">
              Showing open runs only. Switch to Failed or All for closed history from earlier deploys.
            </p>
          )}

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
                          No workflows match this filter
                        </td>
                      </tr>
                    ) : (
                      items.map((row) => {
                        const label = workflowLabel(row.workflow_id);
                        return (
                          <tr key={`${row.workflow_id}:${row.run_id}`}>
                            <td data-label="Workflow">
                              <div className="temporal-workflow-cell">
                                <strong className="mono" title={row.workflow_id ?? undefined}>
                                  {label}
                                </strong>
                                {row.workflow_id && label !== row.workflow_id ? (
                                  <div className="muted mono temporal-workflow-id">
                                    {row.workflow_id}
                                  </div>
                                ) : null}
                              </div>
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
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>
              {nextToken ? (
                <button
                  type="button"
                  className="outline sm temporal-load-more"
                  disabled={loadMoreBusy}
                  onClick={() => void loadMore()}
                >
                  {loadMoreBusy ? "Loading…" : "Load more"}
                </button>
              ) : null}
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
              {detailBusy && <p className="muted">Loading tasks…</p>}
              {selected && !detailBusy && (
                <>
                  <div className="temporal-phase">
                    <p className="section-kicker">Now</p>
                    <p className="temporal-phase-text">
                      {selected.phase || selected.query_status || "No live phase reported"}
                    </p>
                    {(() => {
                      const phaseStats = statsLine(selected.stats);
                      return phaseStats ? (
                        <p className="muted temporal-phase-stats">{phaseStats}</p>
                      ) : null;
                    })()}
                    {selected.query_status && selected.phase !== selected.query_status ? (
                      <p className="muted mono temporal-phase-raw" title="Raw workflow status query">
                        {selected.query_status}
                      </p>
                    ) : null}
                  </div>
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
                  <div className="temporal-tasks-block">
                    <p className="section-kicker">Tasks</p>
                    <p className="muted temporal-tasks-hint">
                      Recent activities in this run (latest {selected.tasks?.length ?? 0}
                      {selected.history_truncated ? ", history window truncated" : ""}).
                    </p>
                    {(selected.tasks?.length ?? 0) === 0 ? (
                      <p className="muted">No activity tasks in the loaded history window.</p>
                    ) : (
                      <div className="table-wrap temporal-tasks-table">
                        <table className="flush">
                          <thead>
                            <tr>
                              <th>Activity</th>
                              <th>Input</th>
                              <th>Status</th>
                              <th>When</th>
                            </tr>
                          </thead>
                          <tbody>
                            {[...(selected.tasks ?? [])].reverse().map((task) => (
                              <tr key={`${task.scheduled_event_id}:${task.activity_id}`}>
                                <td data-label="Activity">
                                  <div className="temporal-task-activity">
                                    <span>
                                      <strong className="mono">{task.name}</strong>
                                      {task.attempt && task.attempt > 1 ? (
                                        <span className="muted"> · attempt {task.attempt}</span>
                                      ) : null}
                                    </span>
                                    {task.error ? (
                                      <div className="temporal-task-error">{task.error}</div>
                                    ) : null}
                                  </div>
                                </td>
                                <td data-label="Input" className="mono">
                                  {task.input ?? "—"}
                                </td>
                                <td data-label="Status">
                                  <span className={`health on-card ${taskStatusKind(task.status)}`}>
                                    <span className="dot" aria-hidden />
                                    {taskStatusLabel(task.status)}
                                  </span>
                                </td>
                                <td data-label="When">{fmtTimestamp(taskWhen(task))}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                  <details className="temporal-history-details">
                    <summary>Raw history ({selected.events.length} events)</summary>
                    <ol className="temporal-events">
                      {selected.events.length === 0 ? (
                        <li className="muted">No events</li>
                      ) : (
                        selected.events.map((ev) => (
                          <li key={String(ev.event_id)}>
                            <span
                              className="mono temporal-event-id"
                              title={`Event #${ev.event_id}`}
                            >
                              {ev.event_id}
                            </span>
                            <span className="temporal-event-type">
                              {ev.event_type || "Unknown"}
                              {ev.summary ? (
                                <span className="muted temporal-event-summary"> · {ev.summary}</span>
                              ) : null}
                            </span>
                            <span className="muted mono">{fmtTimestamp(ev.event_time)}</span>
                          </li>
                        ))
                      )}
                    </ol>
                  </details>
                </>
              )}
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
