import { useCallback, useState, type FormEvent } from "react";
import { api } from "./api";
import { JsonObjectEditor, parseJsonStringMap } from "./JsonObjectEditor";

/** Create a custom remote/stdio MCP endpoint (Apps “Add custom MCP”). */
export function CreateMcpForm({ onCreated, onCancel }: { onCreated: () => void; onCancel: () => void }) {
  const [kind, setKind] = useState<"stdio" | "remote">("remote");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [command, setCommand] = useState("npx");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [env, setEnv] = useState("");
  const [headersJson, setHeadersJson] = useState('{\n  "Authorization": "Bearer "\n}');
  const [headersOk, setHeadersOk] = useState(true);
  const [regions, setRegions] = useState("");
  const [authMode, setAuthMode] = useState<"none" | "token" | "oauth">("token");
  const [oauthAuthorize, setOauthAuthorize] = useState("");
  const [oauthToken, setOauthToken] = useState("");
  const [oauthClientId, setOauthClientId] = useState("");
  const [oauthScopes, setOauthScopes] = useState("");
  const [oauthSecret, setOauthSecret] = useState("");
  const [error, setError] = useState("");

  const onHeadersValidity = useCallback((ok: boolean) => setHeadersOk(ok), []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const envMap: Record<string, string> = {};
      if (kind === "stdio") {
        for (const line of env.split("\n").map((l) => l.trim()).filter(Boolean)) {
          const i = line.indexOf("=");
          if (i < 1) throw new Error(`bad secret line: ${line.split("=")[0]}`);
          envMap[line.slice(0, i)] = line.slice(i + 1);
        }
      } else if (authMode === "token") {
        Object.assign(envMap, parseJsonStringMap(headersJson));
      }
      const preferred_regions = regions
        .split(/[,\s]+/)
        .map((r) => r.trim())
        .filter(Boolean);
      if (kind === "stdio") {
        await api.createServer({
          name,
          slug,
          kind: "stdio",
          command,
          args: args.split(/\s+/).filter(Boolean),
          env: envMap,
          preferred_regions,
        });
      } else {
        const oauth =
          authMode === "oauth" && oauthAuthorize && oauthToken && oauthClientId
            ? {
                authorize_url: oauthAuthorize,
                token_url: oauthToken,
                client_id: oauthClientId,
                scopes: oauthScopes
                  .split(/[,\s]+/)
                  .map((s) => s.trim())
                  .filter(Boolean),
              }
            : null;
        await api.createServer({
          name,
          slug,
          kind: "remote",
          url,
          env: authMode === "none" ? {} : envMap,
          preferred_regions,
          oauth,
          oauth_client_secret: authMode === "oauth" && oauthSecret ? oauthSecret : null,
        });
      }
      onCreated();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <form className="stack custom-mcp-form" onSubmit={submit}>
      <section className="form-section" aria-labelledby="custom-mcp-connection">
        <div className="form-section-heading">
          <h3 id="custom-mcp-connection">Connection</h3>
          <p>Identify the MCP and choose its transport.</p>
        </div>
        <div className="segment" role="tablist" aria-label="MCP kind">
          <button type="button" aria-selected={kind === "remote"} onClick={() => setKind("remote")}>
            Remote HTTP
          </button>
          <button type="button" aria-selected={kind === "stdio"} onClick={() => setKind("stdio")}>
            Stdio
          </button>
        </div>
        <div className="form-grid">
          <label className="field">
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} required />
          </label>
          <label className="field">
            Slug
            <input value={slug} onChange={(e) => setSlug(e.target.value)} required />
          </label>
        </div>
        {kind === "stdio" ? (
          <div className="row">
            <label className="field command-field">
              Command
              <input value={command} onChange={(e) => setCommand(e.target.value)} required />
            </label>
            <label className="field grow">
              Args
              <input
                placeholder="--ignore-scripts pkg@1.2.3"
                value={args}
                onChange={(e) => setArgs(e.target.value)}
              />
            </label>
          </div>
        ) : (
          <label className="field">
            MCP URL
            <input
              placeholder="https://example.com/mcp"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
            />
          </label>
        )}
      </section>

      <section className="form-section" aria-labelledby="custom-mcp-auth">
        <div className="form-section-heading">
          <h3 id="custom-mcp-auth">Authentication</h3>
          <p>Credentials are stored in the gateway vault.</p>
        </div>
        {kind === "stdio" ? (
          <label className="field">
            Env secrets
            <textarea
              placeholder="API_KEY=… (one per line)"
              rows={2}
              value={env}
              onChange={(e) => setEnv(e.target.value)}
            />
          </label>
        ) : (
          <>
            <div className="segment" role="tablist" aria-label="Auth mode">
              <button type="button" aria-selected={authMode === "none"} onClick={() => setAuthMode("none")}>
                No auth
              </button>
              <button type="button" aria-selected={authMode === "token"} onClick={() => setAuthMode("token")}>
                API token
              </button>
              <button type="button" aria-selected={authMode === "oauth"} onClick={() => setAuthMode("oauth")}>
                OAuth
              </button>
            </div>
            {authMode === "token" && (
              <JsonObjectEditor
                label="Request headers (vault)"
                value={headersJson}
                onChange={setHeadersJson}
                onValidityChange={onHeadersValidity}
                placeholder={'{\n  "Authorization": "Bearer …",\n  "X-Api-Key": "…"\n}'}
              />
            )}
            {authMode === "oauth" && (
              <div className="stack oauth-box">
                <div className="form-grid">
                  <label className="field">
                    Authorize URL
                    <input
                      value={oauthAuthorize}
                      onChange={(e) => setOauthAuthorize(e.target.value)}
                      required
                    />
                  </label>
                  <label className="field">
                    Token URL
                    <input value={oauthToken} onChange={(e) => setOauthToken(e.target.value)} required />
                  </label>
                </div>
                <div className="form-grid">
                  <label className="field">
                    Client ID
                    <input
                      value={oauthClientId}
                      onChange={(e) => setOauthClientId(e.target.value)}
                      required
                    />
                  </label>
                  <label className="field">
                    Scopes
                    <input value={oauthScopes} onChange={(e) => setOauthScopes(e.target.value)} />
                  </label>
                </div>
                <label className="field">
                  Client secret (optional)
                  <input
                    type="password"
                    value={oauthSecret}
                    onChange={(e) => setOauthSecret(e.target.value)}
                    autoComplete="off"
                  />
                </label>
              </div>
            )}
          </>
        )}
      </section>

      <section className="form-section" aria-labelledby="custom-mcp-routing">
        <div className="form-section-heading">
          <h3 id="custom-mcp-routing">Routing</h3>
          <p>Optionally prefer runners in specific regions.</p>
        </div>
        <label className="field">
          Preferred regions
          <input
            placeholder="Optional soft affinity"
            value={regions}
            onChange={(e) => setRegions(e.target.value)}
          />
        </label>
        <p className="provider-hint">Starts paused. After creation, open it and Probe to go live.</p>
      </section>

      <div className="form-actions">
        <button type="button" className="ghost" onClick={onCancel}>
          Cancel
        </button>
        <button
          className="primary sm"
          type="submit"
          disabled={kind === "remote" && authMode === "token" && !headersOk}
        >
          Create MCP
        </button>
      </div>
      {error && <div className="notice error">{error}</div>}
    </form>
  );
}
