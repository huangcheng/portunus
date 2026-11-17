import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "./api";
import { parseEnvRows, type EnvRow } from "./envRows";
import { JsonObjectEditor } from "./JsonObjectEditor";
import {
  WEBDAV_MCP_ARGS,
  WEBDAV_MCP_PACKAGE,
  type CatalogApp,
  type CatalogConnection,
} from "./providers";

const CONNECTIONS: CatalogConnection[] = ["oauth", "http", "webdav", "stdio", "skill_bridge"];

type Props = {
  initial?: CatalogApp | null;
  onSaved: (app: CatalogApp) => void;
  onCancel: () => void;
};

function splitLabels(raw: string): string[] {
  return raw
    .split(/[,]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 16);
}

function joinLabels(list: string[] | undefined): string {
  return (list ?? []).join(", ");
}


function asStringMap(raw: unknown): Record<string, string> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof k === "string" && k.trim() && typeof v === "string") out[k.trim()] = v;
  }
  return out;
}

function envDefaultsText(map: Record<string, string>): string {
  return Object.entries(map)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

function parseEnvDefaults(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of raw.split("\n").map((l) => l.trim()).filter(Boolean)) {
    const i = line.indexOf("=");
    if (i < 1) throw new Error(`bad env_defaults line: ${line.split("=")[0] || line}`);
    out[line.slice(0, i).trim()] = line.slice(i + 1);
  }
  return out;
}

function emptySpec(connection: CatalogConnection): Record<string, unknown> {
  switch (connection) {
    case "oauth":
      return { url: "", authorize_url: "", token_url: "", scopes: [] as string[] };
    case "http":
      return { url: "" };
    case "webdav":
      return {
        root_url: "",
        root_path: "/",
        package: WEBDAV_MCP_PACKAGE,
        args: [...WEBDAV_MCP_ARGS],
      };
    case "stdio":
      return { command: "npx", args: [] as string[], env: [] as EnvRow[] };
    case "skill_bridge":
      return {
        manifest: {},
        secrets: [] as EnvRow[],
        env_defaults: {},
        command: "python",
        args: ["-m", "portunus_skill_bridge"],
      };
  }
}

/** Create / edit an Apps catalog recipe (not a live Server). */
export function CatalogConfigForm({ initial, onSaved, onCancel }: Props) {
  const editing = !!initial;
  const [slug, setSlug] = useState(initial?.slug ?? "");
  const [label, setLabel] = useState(initial?.label ?? "");
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [connection, setConnection] = useState<CatalogConnection>(initial?.connection ?? "http");
  const [popular, setPopular] = useState(initial?.popular ?? false);
  const [categories, setCategories] = useState(joinLabels(initial?.categories));
  const [tags, setTags] = useState(joinLabels(initial?.tags));
  const [sortOrder, setSortOrder] = useState(String(initial?.sort_order ?? 100));
  const [spec, setSpec] = useState<Record<string, unknown>>(
    initial?.spec ? { ...initial.spec } : emptySpec("http"),
  );
  const [manifestJson, setManifestJson] = useState(
    JSON.stringify((initial?.spec?.manifest as object) ?? {}, null, 2) + "\n",
  );
  const [manifestOk, setManifestOk] = useState(true);
  const [envDefaults, setEnvDefaults] = useState(
    envDefaultsText(asStringMap(initial?.spec?.env_defaults)),
  );
  const [setupHintsText, setSetupHintsText] = useState(
    Array.isArray(initial?.spec?.setup_hints)
      ? (initial!.spec.setup_hints as unknown[]).map(String).join("\n")
      : "",
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!initial) return;
    setSlug(initial.slug);
    setLabel(initial.label);
    setName(initial.name);
    setDescription(initial.description);
    setConnection(initial.connection);
    setPopular(initial.popular);
    setCategories(joinLabels(initial.categories));
    setTags(joinLabels(initial.tags));
    setSortOrder(String(initial.sort_order));
    setSpec({ ...initial.spec });
    setManifestJson(JSON.stringify((initial.spec.manifest as object) ?? {}, null, 2) + "\n");
    setEnvDefaults(envDefaultsText(asStringMap(initial.spec.env_defaults)));
    setSetupHintsText(
      Array.isArray(initial.spec.setup_hints)
        ? (initial.spec.setup_hints as unknown[]).map(String).join("\n")
        : "",
    );
    setError("");
  }, [initial]);

  const onManifestValidity = useCallback((ok: boolean) => setManifestOk(ok), []);

  function setConnectionAndReset(next: CatalogConnection) {
    if (editing) return;
    setConnection(next);
    setSpec(emptySpec(next));
    setSetupHintsText("");
    if (next === "skill_bridge") {
      setManifestJson("{\n}\n");
      setEnvDefaults("");
    }
  }

  function patchSpec(partial: Record<string, unknown>) {
    setSpec((prev) => ({ ...prev, ...partial }));
  }

  function setupHintsFromText(): string[] {
    return setupHintsText
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function withSetupHints(out: Record<string, unknown>): Record<string, unknown> {
    // Only persist the key when there are hints or the entry already had them, so a
    // dashboard save does not inject setup_hints: [] (which blocks gateway seed backfills).
    const hints = setupHintsFromText();
    if (hints.length > 0 || Array.isArray(initial?.spec?.setup_hints)) {
      out.setup_hints = hints;
    }
    return out;
  }

  function buildSpec(): Record<string, unknown> {
    if (connection === "oauth") {
      const scopeList = Array.isArray(spec.scopes)
        ? (spec.scopes as unknown[]).map(String).filter(Boolean)
        : String(spec.scopes ?? "")
            .split(/[,\s]+/)
            .map((s) => s.trim())
            .filter(Boolean);
      const out: Record<string, unknown> = {
        url: String(spec.url ?? "").trim(),
        authorize_url: String(spec.authorize_url ?? "").trim(),
        token_url: String(spec.token_url ?? "").trim(),
        scopes: scopeList,
      };
      const clientId = String(spec.client_id ?? "").trim();
      if (clientId) out.client_id = clientId;
      const resource = String(spec.resource ?? "").trim();
      if (resource) out.resource = resource;
      const reg = String(spec.registration_endpoint ?? "").trim();
      if (reg) out.registration_endpoint = reg;
      const issuer = String(spec.issuer ?? "").trim();
      if (issuer) out.issuer = issuer;
      const method = String(spec.token_endpoint_auth_method ?? "").trim();
      if (method) out.token_endpoint_auth_method = method;
      if (spec.use_cimd === true) out.use_cimd = true;
      if (spec.allow_bearer_token === true) out.allow_bearer_token = true;
      if (spec.url_from_operator === true) {
        out.url_from_operator = true;
        // Operator-pasted MCP URL at Connect; seed may omit a fixed url.
        const url = String(spec.url ?? "").trim();
        if (url) out.url = url;
        else delete out.url;
      }
      return withSetupHints(out);
    }
    if (connection === "http") {
      const out: Record<string, unknown> = {};
      if (spec.url_from_operator === true) {
        out.url_from_operator = true;
        const url = String(spec.url ?? "").trim();
        if (url) out.url = url;
      } else {
        out.url = String(spec.url ?? "").trim();
      }
      const authHeader = String(spec.auth_header ?? "").trim();
      if (authHeader) {
        out.auth_header = authHeader;
        out.token_prefix = String(spec.token_prefix ?? "");
      }
      return withSetupHints(out);
    }
    if (connection === "webdav") {
      const argsRaw = spec.args;
      const args = Array.isArray(argsRaw)
        ? argsRaw.map(String)
        : String(argsRaw ?? "")
            .split(/\s+/)
            .filter(Boolean);
      return withSetupHints({
        root_url: String(spec.root_url ?? "").trim(),
        root_path: String(spec.root_path ?? "/").trim() || "/",
        package: String(spec.package ?? WEBDAV_MCP_PACKAGE).trim() || WEBDAV_MCP_PACKAGE,
        args: args.length ? args : [...WEBDAV_MCP_ARGS],
      });
    }
    if (connection === "stdio") {
      const argsRaw = spec.args;
      const args = Array.isArray(argsRaw)
        ? argsRaw.map(String)
        : String(argsRaw ?? "")
            .split(/\s+/)
            .filter(Boolean);
      return withSetupHints({
        command: String(spec.command ?? "").trim(),
        args,
        env: parseEnvRows(spec.env).filter((r) => r.name.trim()),
      });
    }
    const trimmed = manifestJson.trim();
    if (!trimmed) throw new Error("spec.manifest is required");
    const manifest = JSON.parse(trimmed) as unknown;
    if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
      throw new Error("spec.manifest must be a JSON object");
    }
    const argsRaw = spec.args;
    const args = Array.isArray(argsRaw)
      ? argsRaw.map(String)
      : String(argsRaw ?? "")
          .split(/\s+/)
          .filter(Boolean);
    return withSetupHints({
      manifest,
      secrets: parseEnvRows(spec.secrets).filter((r) => r.name.trim()),
      env_defaults: parseEnvDefaults(envDefaults),
      command: String(spec.command ?? "python").trim() || "python",
      args: args.length ? args : ["-m", "portunus_skill_bridge"],
    });
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const order = Number(sortOrder);
      if (!Number.isFinite(order) || order < 0 || order > 10_000) {
        throw new Error("sort_order must be 0–10000");
      }
      const bodySpec = buildSpec();
      if (editing && initial) {
        const updated = await api.updateApp(initial.id, {
          label: label.trim(),
          name: name.trim(),
          description: description.trim(),
          popular,
          categories: splitLabels(categories),
          tags: splitLabels(tags),
          sort_order: order,
          spec: bodySpec,
        });
        onSaved(updated as CatalogApp);
      } else {
        const created = await api.createApp({
          slug: slug.trim().toLowerCase(),
          label: label.trim(),
          name: name.trim(),
          description: description.trim(),
          connection,
          popular,
          categories: splitLabels(categories),
          tags: splitLabels(tags),
          sort_order: order,
          spec: bodySpec,
        });
        onSaved(created as CatalogApp);
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const scopesDisplay = Array.isArray(spec.scopes)
    ? (spec.scopes as string[]).join(", ")
    : String(spec.scopes ?? "");
  const argsDisplay = Array.isArray(spec.args)
    ? (spec.args as string[]).join(" ")
    : String(spec.args ?? "");
  const envRows = connection === "stdio" ? parseEnvRows(spec.env) : parseEnvRows(spec.secrets);

  function updateEnvRow(index: number, patch: Partial<EnvRow>) {
    const key = connection === "stdio" ? "env" : "secrets";
    const next = envRows.map((row, i) => (i === index ? { ...row, ...patch } : row));
    patchSpec({ [key]: next });
  }

  function addEnvRow() {
    const key = connection === "stdio" ? "env" : "secrets";
    patchSpec({ [key]: [...envRows, { name: "", label: "", secret: true }] });
  }

  function removeEnvRow(index: number) {
    const key = connection === "stdio" ? "env" : "secrets";
    patchSpec({ [key]: envRows.filter((_, i) => i !== index) });
  }

  return (
    <form className="stack custom-mcp-form catalog-config-form" onSubmit={submit}>
      <section className="form-section" aria-labelledby="catalog-identity">
        <div className="form-section-heading">
          <h3 id="catalog-identity">Identity</h3>
          <p>Catalog recipe shown in the Apps directory.</p>
        </div>
        <div className="form-grid">
          <label className="field">
            Slug
            <input
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              required
              disabled={editing}
              pattern="[a-z0-9][a-z0-9_-]{0,62}"
              title="lowercase slug"
            />
          </label>
          <label className="field">
            Label
            <input value={label} onChange={(e) => setLabel(e.target.value)} required />
          </label>
        </div>
        <div className="form-grid">
          <label className="field">
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} required />
          </label>
          <label className="field">
            Sort order
            <input
              type="number"
              min={0}
              max={10000}
              value={sortOrder}
              onChange={(e) => setSortOrder(e.target.value)}
              required
            />
          </label>
        </div>
        <label className="field">
          Description
          <textarea
            rows={2}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            required
          />
        </label>
        <div className="form-grid">
          <label className="field">
            Categories
            <input
              placeholder="productivity, storage"
              value={categories}
              onChange={(e) => setCategories(e.target.value)}
            />
          </label>
          <label className="field">
            Tags
            <input placeholder="optional tags" value={tags} onChange={(e) => setTags(e.target.value)} />
          </label>
        </div>
        <label className="field checkbox-field">
          <input type="checkbox" checked={popular} onChange={(e) => setPopular(e.target.checked)} />
          Show in Popular strip
        </label>
      </section>

      <section className="form-section" aria-labelledby="catalog-connection">
        <div className="form-section-heading">
          <h3 id="catalog-connection">Connection</h3>
          <p>{editing ? "Locked after create." : "Chooses Connect UX and spec shape."}</p>
        </div>
        <div className="segment catalog-connection-segment" role="tablist" aria-label="Connection type">
          {CONNECTIONS.map((c) => (
            <button
              key={c}
              type="button"
              aria-selected={connection === c}
              disabled={editing && connection !== c}
              onClick={() => setConnectionAndReset(c)}
            >
              {c}
            </button>
          ))}
        </div>
      </section>

      <section className="form-section" aria-labelledby="catalog-spec">
        <div className="form-section-heading">
          <h3 id="catalog-spec">Spec</h3>
          <p>Recipe fields validated by the gateway.</p>
        </div>

        {connection === "oauth" && (
          <div className="stack">
            <label className="field">
              MCP URL
              <input
                value={String(spec.url ?? "")}
                onChange={(e) => patchSpec({ url: e.target.value })}
                required
              />
            </label>
            <div className="form-grid">
              <label className="field">
                Authorize URL
                <input
                  value={String(spec.authorize_url ?? "")}
                  onChange={(e) => patchSpec({ authorize_url: e.target.value })}
                  required
                />
              </label>
              <label className="field">
                Token URL
                <input
                  value={String(spec.token_url ?? "")}
                  onChange={(e) => patchSpec({ token_url: e.target.value })}
                  required
                />
              </label>
            </div>
            <div className="form-grid">
              <label className="field">
                Scopes
                <input
                  value={scopesDisplay}
                  onChange={(e) =>
                    patchSpec({
                      scopes: e.target.value
                        .split(/[,\s]+/)
                        .map((s) => s.trim())
                        .filter(Boolean),
                    })
                  }
                />
              </label>
              <label className="field">
                Client ID (optional)
                <input
                  value={String(spec.client_id ?? "")}
                  onChange={(e) => patchSpec({ client_id: e.target.value })}
                />
              </label>
            </div>
            <div className="form-grid">
              <label className="field">
                Resource (optional)
                <input
                  value={String(spec.resource ?? "")}
                  onChange={(e) => patchSpec({ resource: e.target.value })}
                  placeholder="https://…"
                />
              </label>
              <label className="field">
                Registration endpoint
                <input
                  value={String(spec.registration_endpoint ?? "")}
                  onChange={(e) => patchSpec({ registration_endpoint: e.target.value })}
                  placeholder="https://… (DCR)"
                />
              </label>
            </div>
            <label className="field">
              Issuer (optional, RFC 9207)
              <input
                value={String(spec.issuer ?? "")}
                onChange={(e) => patchSpec({ issuer: e.target.value })}
                placeholder="https://idp.example (exact iss match)"
              />
            </label>
            <label className="field">
              Token endpoint auth method
              <select
                value={String(spec.token_endpoint_auth_method ?? "")}
                onChange={(e) =>
                  patchSpec({
                    token_endpoint_auth_method: e.target.value || undefined,
                  })
                }
              >
                <option value="">(default)</option>
                <option value="none">none</option>
                <option value="client_secret_basic">client_secret_basic</option>
                <option value="client_secret_post">client_secret_post</option>
              </select>
            </label>
          </div>
        )}

        {connection === "http" && (
          <div className="stack">
            <label className="field">
              MCP URL
              <input
                value={String(spec.url ?? "")}
                onChange={(e) => patchSpec({ url: e.target.value })}
                placeholder="https://example.com/mcp"
                required={spec.url_from_operator !== true}
              />
            </label>
            <div className="form-grid">
              <label className="field">
                Auth header (optional)
                <input
                  value={String(spec.auth_header ?? "")}
                  onChange={(e) => patchSpec({ auth_header: e.target.value })}
                  placeholder="WyHeader (blank = Bearer)"
                />
              </label>
              <label className="field">
                Token prefix
                <input
                  value={String(spec.token_prefix ?? "")}
                  onChange={(e) => patchSpec({ token_prefix: e.target.value })}
                  placeholder="mcp_token="
                  disabled={!String(spec.auth_header ?? "").trim()}
                />
              </label>
            </div>
          </div>
        )}

        {connection === "webdav" && (
          <div className="stack">
            <label className="field">
              Root URL
              <input
                value={String(spec.root_url ?? "")}
                onChange={(e) => patchSpec({ root_url: e.target.value })}
                required
              />
            </label>
            <div className="form-grid">
              <label className="field">
                Root path
                <input
                  value={String(spec.root_path ?? "/")}
                  onChange={(e) => patchSpec({ root_path: e.target.value })}
                />
              </label>
              <label className="field">
                Package
                <input
                  value={String(spec.package ?? WEBDAV_MCP_PACKAGE)}
                  onChange={(e) => patchSpec({ package: e.target.value })}
                />
              </label>
            </div>
            <label className="field">
              Args
              <input
                value={argsDisplay}
                onChange={(e) =>
                  patchSpec({
                    args: e.target.value.split(/\s+/).filter(Boolean),
                  })
                }
              />
            </label>
          </div>
        )}

        {connection === "stdio" && (
          <div className="stack">
            <div className="row">
              <label className="field command-field">
                Command
                <input
                  value={String(spec.command ?? "")}
                  onChange={(e) => patchSpec({ command: e.target.value })}
                  required
                />
              </label>
              <label className="field grow">
                Args
                <input
                  value={argsDisplay}
                  onChange={(e) =>
                    patchSpec({
                      args: e.target.value.split(/\s+/).filter(Boolean),
                    })
                  }
                />
              </label>
            </div>
            <EnvRowsEditor
              title="Env fields (Connect prompts)"
              rows={envRows}
              onChange={updateEnvRow}
              onAdd={addEnvRow}
              onRemove={removeEnvRow}
              supportsMultiline
            />
          </div>
        )}

        {connection === "skill_bridge" && (
          <div className="stack">
            <JsonObjectEditor
              label="Manifest (JSON object)"
              value={manifestJson}
              onChange={setManifestJson}
              onValidityChange={onManifestValidity}
              valueMode="object"
              rows={12}
              placeholder={'{\n  "name": "…",\n  "auth": { "type": "…" }\n}'}
            />
            <EnvRowsEditor
              title="Secrets (Connect prompts)"
              rows={envRows}
              onChange={updateEnvRow}
              onAdd={addEnvRow}
              onRemove={removeEnvRow}
              supportsMultiline={false}
            />
            <label className="field">
              Env defaults (KEY=value per line)
              <textarea
                rows={3}
                value={envDefaults}
                onChange={(e) => setEnvDefaults(e.target.value)}
                placeholder="SKILL_BASE_URL=https://…"
              />
            </label>
            <div className="row">
              <label className="field command-field">
                Command
                <input
                  value={String(spec.command ?? "python")}
                  onChange={(e) => patchSpec({ command: e.target.value })}
                />
              </label>
              <label className="field grow">
                Args
                <input
                  value={argsDisplay}
                  onChange={(e) =>
                    patchSpec({
                      args: e.target.value.split(/\s+/).filter(Boolean),
                    })
                  }
                />
              </label>
            </div>
          </div>
        )}

        <label className="field">
          Setup hints (one per line; optional <code>[label](https://…)</code> links)
          <textarea
            rows={3}
            value={setupHintsText}
            onChange={(e) => setSetupHintsText(e.target.value)}
            placeholder="Paste the API key from the [vendor console](https://example.com)."
          />
        </label>
      </section>

      <div className="form-actions">
        <button type="button" className="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button
          className="primary sm"
          type="submit"
          disabled={busy || (connection === "skill_bridge" && !manifestOk)}
        >
          {editing ? "Save catalog app" : "Create catalog app"}
        </button>
      </div>
      {error && <div className="notice error">{error}</div>}
    </form>
  );
}

function EnvRowsEditor({
  title,
  rows,
  onChange,
  onAdd,
  onRemove,
  supportsMultiline,
}: {
  title: string;
  rows: EnvRow[];
  onChange: (index: number, patch: Partial<EnvRow>) => void;
  onAdd: () => void;
  onRemove: (index: number) => void;
  supportsMultiline: boolean;
}) {
  return (
    <div className="stack env-rows-editor">
      <div className="env-rows-heading">
        <span>{title}</span>
        <button type="button" className="ghost sm" onClick={onAdd}>
          Add field
        </button>
      </div>
      {rows.length === 0 ? (
        <p className="provider-hint">No fields yet.</p>
      ) : (
        rows.map((row, i) => (
          <div key={i} className="env-row">
            <label className="field">
              Name
              <input value={row.name} onChange={(e) => onChange(i, { name: e.target.value })} required />
            </label>
            <label className="field">
              Label
              <input value={row.label} onChange={(e) => onChange(i, { label: e.target.value })} />
            </label>
            <label className="field checkbox-field">
              <input
                type="checkbox"
                checked={row.secret}
                onChange={(e) => onChange(i, { secret: e.target.checked })}
              />
              Secret
            </label>
            {supportsMultiline ? (
              <label className="field checkbox-field">
                <input
                  type="checkbox"
                  checked={row.multiline === true}
                  onChange={(e) => onChange(i, { multiline: e.target.checked })}
                />
                Multiline
              </label>
            ) : null}
            <button type="button" className="ghost sm" onClick={() => onRemove(i)} aria-label="Remove">
              Remove
            </button>
          </div>
        ))
      )}
    </div>
  );
}
