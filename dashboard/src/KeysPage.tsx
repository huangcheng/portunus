import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type Key } from "./api";
import { Icon } from "./icons/ui";

async function copyText(text: string) {
  await navigator.clipboard.writeText(text);
}

export function KeysPage() {
  const [keys, setKeys] = useState<Key[]>([]);
  const [label, setLabel] = useState("");
  const [minted, setMinted] = useState("");
  const [error, setError] = useState("");
  const [mintOpen, setMintOpen] = useState(false);
  const [copyingId, setCopyingId] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      setKeys(await api.keys());
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => void refresh(), [refresh]);

  const act = (fn: () => Promise<unknown>) => async () => {
    setError("");
    try {
      await fn();
      await refresh();
      window.dispatchEvent(new Event("portunus-refresh"));
    } catch (e) {
      if ((e as { code?: string }).code !== "cancelled") setError((e as Error).message);
    }
  };

  async function mint(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const k = await api.mintKey(label);
      setMinted(k.token);
      setLabel("");
      setMintOpen(false);
      await refresh();
      window.dispatchEvent(new Event("portunus-refresh"));
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function copyKey(keyId: string) {
    setError("");
    setCopyingId(keyId);
    try {
      const { token } = await api.revealKey(keyId);
      await copyText(token);
      setCopiedId(keyId);
      window.setTimeout(() => setCopiedId(null), 1500);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setCopyingId(null);
    }
  }

  return (
    <>
      {error && <div className="notice error">{error}</div>}

      <section className="keys-summary" aria-label="Keyring summary">
        <div className="keys-summary-count">
          <span className="summary-label">Keyring / active</span>
          <strong>{loading ? "—" : String(keys.length).padStart(2, "0")}</strong>
          <span>Active credentials</span>
        </div>
        <div className="keys-summary-note">
          <span className="summary-icon"><Icon name="shield" size={23} /></span>
          <div>
            <strong>Full access, explicitly granted.</strong>
            <p>Each key can call every connected app through <code>/mcp</code>. Revoke access here at any time.</p>
          </div>
        </div>
        <div className="keys-summary-route" aria-hidden="true">
          <span>AGENT</span><Icon name="chevron-right" size={14} /><span>MCP</span><Icon name="chevron-right" size={14} /><span>TOOLS</span>
        </div>
      </section>

      {minted && (
        <section className="secret-banner" aria-label="New agent key">
          <div className="secret-banner-header">
            <span className="secret-icon"><Icon name="key" size={19} /></span>
            <div>
              <span className="secret-kicker">Credential created</span>
              <strong>Your new key is ready</strong>
              <p>Copy it now, or recover it later from the key inventory.</p>
            </div>
          </div>
          <p className="token">{minted}</p>
          <div className="secret-actions">
            <button type="button" className="primary sm" onClick={() => void copyText(minted).then(() => setMinted(""))}>
              <Icon name="copy" size={14} />
              Copy & dismiss
            </button>
            <button type="button" className="ghost sm" onClick={() => setMinted("")}>
              Dismiss
            </button>
          </div>
        </section>
      )}

      {mintOpen && (
        <div className="card create-card">
          <span className="create-card-kicker">New credential</span>
          <h2>Create an agent key</h2>
          <p className="desc">Use a recognizable label to identify where this key is used.</p>
          <form className="mint-row" onSubmit={mint}>
            <label className="field grow">
              Label <span className="muted">(optional)</span>
              <input
                placeholder="e.g. cursor-agent"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                autoFocus
              />
            </label>
            <button type="button" className="ghost" onClick={() => setMintOpen(false)}>
              Cancel
            </button>
            <button className="primary sm" type="submit">
              <Icon name="plus" size={14} />
              Create key
            </button>
          </form>
        </div>
      )}

      <section className="card keys-card" aria-labelledby="keys-inventory-title">
        <div className="card-header">
          <div>
            <p className="section-kicker">Credential directory</p>
            <h2 id="keys-inventory-title">Key inventory</h2>
          </div>
          <p className="desc">Bearer credentials for <code>/mcp</code>. Every key can use every app.</p>
          <div className="action">
            <button type="button" className="primary sm" onClick={() => setMintOpen(true)}>
              <Icon name="plus" size={14} />
              Create key
            </button>
          </div>
        </div>
        <div className="card-body">
          {loading ? (
            <div className="empty keys-empty"><span className="empty-icon"><Icon name="key" size={24} /></span><strong>Opening your keyring</strong><span>Loading credentials…</span></div>
          ) : keys.length === 0 ? (
            <div className="empty keys-empty">
              <span className="empty-icon"><Icon name="key" size={24} /></span>
              <strong>Your keyring is empty</strong>
              <span>Create a bearer key so agents can call <code>/mcp</code>.</span>
              <button type="button" className="primary sm" onClick={() => setMintOpen(true)}>
                <Icon name="plus" size={14} />
                Create your first key
              </button>
            </div>
          ) : (
            <div className="table-wrap">
              <table className="flush">
                <thead>
                  <tr>
                    <th>Key</th>
                    <th>Label</th>
                    <th>Access</th>
                    <th>State</th>
                    <th className="text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {keys.map((k) => (
                    <tr key={k.key_id}>
                      <td data-label="Key">
                        <div className="key-identity">
                          <span className="key-icon"><Icon name="key" size={15} /></span>
                          <code title={k.key_id}>{k.key_id.slice(0, 8)}…</code>
                        </div>
                      </td>
                      <td data-label="Label" className="key-label">
                        {k.label?.trim() ? k.label : <span className="muted">Untitled key</span>}
                      </td>
                      <td data-label="Access">
                        <span className="access-pill"><Icon name="apps" size={11} />All apps</span>
                      </td>
                      <td data-label="State">
                        <span className="status on">Active</span>
                      </td>
                      <td data-label="Actions">
                        <div className="actions">
                          <button
                            type="button"
                            className="outline sm"
                            disabled={!k.has_secret || copyingId === k.key_id}
                            title={k.has_secret ? "Copy key" : "Key not recoverable — delete and create a new one."}
                            onClick={() => void copyKey(k.key_id)}
                          >
                            <Icon name={copiedId === k.key_id ? "check" : "copy"} size={13} />
                            {copyingId === k.key_id ? "Copying…" : copiedId === k.key_id ? "Copied" : "Copy"}
                          </button>
                          <button
                            type="button"
                            className="destructive sm"
                            onClick={act(async () => {
                              if (!window.confirm(`Delete key ${k.key_id.slice(0, 8)}…? This cannot be undone.`)) {
                                throw Object.assign(new Error("cancelled"), { code: "cancelled" });
                              }
                              await api.deleteKey(k.key_id);
                            })}
                          >
                            <Icon name="trash" size={13} />
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
    </>
  );
}
