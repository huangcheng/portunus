import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type Key } from "./api";

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

  const refresh = useCallback(async () => {
    try {
      setKeys(await api.keys());
    } catch (e) {
      setError((e as Error).message);
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
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setCopyingId(null);
    }
  }

  return (
    <>
      {error && <p className="error">{error}</p>}

      {minted && (
        <div className="secret-banner">
          <strong>Copy this key now</strong>
          Or use Actions → Copy later — the secret is recoverable.
          <p className="token">{minted}</p>
          <button type="button" className="outline sm" onClick={() => void copyText(minted).then(() => setMinted(""))}>
            Copy & dismiss
          </button>
          <button type="button" className="ghost sm" onClick={() => setMinted("")}>
            Dismiss
          </button>
        </div>
      )}

      {mintOpen && (
        <div className="card create-card">
          <h2>Mint key</h2>
          <form className="mint-row" onSubmit={mint}>
            <label className="field grow">
              Label
              <input
                placeholder="optional — e.g. cursor-agent"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                autoFocus
              />
            </label>
            <button type="button" className="ghost" onClick={() => setMintOpen(false)}>
              Cancel
            </button>
            <button className="primary sm" type="submit">
              Mint
            </button>
          </form>
        </div>
      )}

      <div className="card">
        <div className="card-header">
          <h2>Keys</h2>
          <p className="desc">Bearer keys for /mcp. Every key can use every server.</p>
          <div className="action">
            <button type="button" className="primary sm" onClick={() => setMintOpen(true)}>
              Create key
            </button>
          </div>
        </div>
        <div className="card-body">
          {keys.length === 0 ? (
            <div className="empty">
              <strong>No agent keys yet</strong>
              Create a bearer key so agents can call /mcp.
            </div>
          ) : (
            <div className="table-wrap">
              <table className="flush">
                <thead>
                  <tr>
                    <th>Prefix</th>
                    <th>Label</th>
                    <th>Access</th>
                    <th>State</th>
                    <th className="text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {keys.map((k) => (
                    <tr key={k.key_id}>
                      <td className="mono">{k.key_id.slice(0, 8)}…</td>
                      <td className="muted">{k.label?.trim() ? k.label : "—"}</td>
                      <td>
                        <span className="access-pill">all servers</span>
                      </td>
                      <td>
                        <span className="status on">active</span>
                      </td>
                      <td>
                        <div className="actions">
                          <button
                            type="button"
                            className="outline sm"
                            disabled={!k.has_secret || copyingId === k.key_id}
                            title={k.has_secret ? "Copy key" : "Key not recoverable — delete and create a new one."}
                            onClick={() => void copyKey(k.key_id)}
                          >
                            {copyingId === k.key_id ? "Copying…" : "Copy"}
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
      </div>
    </>
  );
}
