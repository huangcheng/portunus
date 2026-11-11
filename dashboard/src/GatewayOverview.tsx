import { BrandMark, Icon } from "./icons/ui";

export function GatewayOverview({
  appCount,
  connectedCount,
  toolCount,
}: {
  appCount: number;
  connectedCount: number;
  toolCount: number;
}) {
  return (
    <section className="gateway-overview" aria-labelledby="gateway-overview-title">
      <div className="overview-copy">
        <p className="overview-kicker"><span />Gateway / overview</p>
        <h2 id="gateway-overview-title">One connection.<br />Every agent.</h2>
        <p className="overview-description">Your tools stay behind one secure MCP endpoint, ready wherever your agents work.</p>
        <dl className="overview-stats">
          <div><dt>Available apps</dt><dd>{String(appCount).padStart(2, "0")}</dd></div>
          <div><dt>Connected</dt><dd>{String(connectedCount).padStart(2, "0")}</dd></div>
          <div><dt>Tools discovered</dt><dd>{String(toolCount).padStart(2, "0")}</dd></div>
        </dl>
      </div>
      <figure className="gateway-map" aria-label="Apps connect through Portunus to agents">
        <div className="map-coordinate map-coordinate-top">Y / 04</div>
        <div className="map-coordinate map-coordinate-bottom">X / 16</div>
        <svg className="gateway-map-lines" viewBox="0 0 480 260" fill="none" aria-hidden="true">
          <path d="M74 58C146 58 142 130 218 130" />
          <path d="M74 130H218" />
          <path d="M74 202C146 202 142 130 218 130" />
          <path d="M262 130H408" />
          <circle cx="148" cy="130" r="3" />
          <circle cx="334" cy="130" r="3" />
        </svg>
        <div className="map-app-nodes" aria-hidden="true">
          <span><Icon name="layers" size={16} /></span>
          <span><Icon name="apps" size={16} /></span>
          <span><Icon name="link" size={16} /></span>
        </div>
        <div className="map-core">
          <span className="map-core-orbit" aria-hidden="true" />
          <BrandMark />
          <strong>Portunus</strong>
          <span>Secure gateway</span>
        </div>
        <div className="map-agent-node">
          <Icon name="agents" size={22} />
          <strong>Agents</strong>
          <span>MCP ready</span>
        </div>
        <figcaption><span className="environment-dot" />One secure data plane</figcaption>
      </figure>
    </section>
  );
}
