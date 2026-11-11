import { useEffect, useState } from "react";
import { api, type FleetResponse } from "./api";
import { fmtTimestamp } from "./format";
import { Icon } from "./icons/ui";

export function FleetPage() {
  const [fleet, setFleet] = useState<FleetResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const f = await api.fleet();
        if (!cancelled) {
          setFleet(f);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load fleet");
      }
    };
    void tick();
    const id = window.setInterval(tick, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  if (!fleet) {
    return error ? <div className="notice error">{error}</div> : <div className="muted">Loading fleet…</div>;
  }

  const { gateway, runners, children } = fleet;

  return (
    <div className="fleet-page">
      {error && <div className="notice error">{error}</div>}
      <section className="fleet-strip" aria-label="Gateway">
        <div>
          <span className="fleet-label">Gateway</span>
          <strong>{gateway.version}</strong>
        </div>
        <div>
          <span className="fleet-label">Started</span>
          <span>{fmtTimestamp(gateway.started_at)}</span>
        </div>
        <div>
          <span className="fleet-label">Database</span>
          <span>{gateway.db_backend}</span>
        </div>
        <div>
          <span className="fleet-label">OTLP export</span>
          <span>{gateway.otel_export ? "on" : "off"}</span>
        </div>
      </section>

      <section className="card fleet-section">
        <div className="card-header">
          <div>
            <p className="section-kicker">Runners</p>
            <h2>Sites linked to this gateway</h2>
          </div>
        </div>
        <div className="card-body">
          <div className="table-wrap">
            <table className="flush">
              <thead>
                <tr>
                  <th>Site</th>
                  <th>Regions</th>
                  <th>Status</th>
                  <th>Heartbeat</th>
                  <th>Reconnects</th>
                  <th>Children</th>
                  <th>Drift</th>
                </tr>
              </thead>
              <tbody>
                {runners.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="muted">
                      No runners configured
                    </td>
                  </tr>
                ) : (
                  runners.map((r) => (
                    <tr key={r.site_id}>
                      <td data-label="Site">
                        <strong>{r.site_id}</strong>
                      </td>
                      <td data-label="Regions" className="mono">
                        {r.regions.join(", ") || "—"}
                      </td>
                      <td data-label="Status">
                        <span className={`health on-card ${r.connected ? "ok" : ""}`}>
                          <span className="dot" aria-hidden />
                          {r.connected
                            ? `Connected since ${fmtTimestamp(r.connected_since)}`
                            : `Disconnected ${fmtTimestamp(r.last_disconnected_at)}`}
                        </span>
                      </td>
                      <td data-label="Heartbeat" className="mono">
                        {fmtTimestamp(r.last_heartbeat_at)}
                      </td>
                      <td data-label="Reconnects" className="mono">
                        {r.reconnects}
                      </td>
                      <td data-label="Children" className="mono">
                        {r.children}
                      </td>
                      <td data-label="Drift">
                        {r.drift && r.drift.length > 0 ? (
                          <span className="app-pill warn" title={r.drift.join(", ")}>
                            <Icon name="shield" size={12} />
                            {r.drift.join(", ")}
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section className="card fleet-section">
        <div className="card-header">
          <div>
            <p className="section-kicker">Children</p>
            <h2>Live stdio slots</h2>
          </div>
        </div>
        <div className="card-body">
          <div className="table-wrap">
            <table className="flush">
              <thead>
                <tr>
                  <th>Server</th>
                  <th>Key</th>
                  <th>Site</th>
                  <th>State</th>
                  <th>Flags</th>
                </tr>
              </thead>
              <tbody>
                {children.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="muted">
                      No live children
                    </td>
                  </tr>
                ) : (
                  children.map((c) => (
                    <tr key={c.child_id}>
                      <td data-label="Server">{c.server.slug ?? c.server.id ?? "—"}</td>
                      <td data-label="Key" className="mono">
                        {c.key_id ?? "—"}
                      </td>
                      <td data-label="Site">{c.site_id}</td>
                      <td data-label="State">
                        <code>{c.state}</code>
                      </td>
                      <td data-label="Flags">
                        {[c.preferred_miss ? "preferred_miss" : null, c.pending_stop ? "pending_stop" : null]
                          .filter(Boolean)
                          .join(", ") || "—"}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  );
}
