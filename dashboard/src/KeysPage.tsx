import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type Key, type Server } from "./api";

export function KeysPage() {
  const [keys, setKeys] = useState<Key[]>([]);
  const [servers, setServers] = useState<Server[]>([]);
  const [label, setLabel] = useState("");
  const [minted, setMinted] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [k, s] = await Promise.all([api.keys(), api.servers()]);
      setKeys(k);
      setServers(s);
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
    } catch (e) {
      if ((e as { code?: string }).code !== "cancelled") setError((e as Error).message);
    }
  };

  async function mint(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const k = await api.mintKey(label);
      setMinted(k.token); // shown once; never fetched again
      setLabel("");
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const name = (id: string) => servers.find((s) => s.id === id)?.slug ?? id;

  return (
    <>
      <section>
        <h2>Mint agent key</h2>
        <form onSubmit={mint}>
          <input placeholder="label (optional)" value={label} onChange={(e) => setLabel(e.target.value)} />
          <button type="submit">Mint</button>
        </form>
        {minted && (
          <>
            <p>Copy this key now — it will not be shown again:</p>
            <p className="token">{minted}</p>
            <button onClick={() => setMinted("")}>Dismiss</button>
          </>
        )}
        {error && <p className="error">{error}</p>}
      </section>
      <section>
        <h2>Agent keys</h2>
        {keys.length === 0 && <p className="muted">No keys yet.</p>}
        <table>
          <tbody>
            {keys.map((k) => (
              <tr key={k.key_id}>
                <td>
                  <code>{k.key_id.slice(0, 8)}…</code> {k.label}
                  {k.revoked_at && <strong className="error"> revoked</strong>}
                </td>
                <td>
                  {k.server_ids.map((sid) => (
                    <span key={sid}>
                      {name(sid)} <button onClick={act(() => api.ungrant(k.key_id, sid))}>×</button>{" "}
                    </span>
                  ))}
                  {!k.revoked_at && (
                    <select value="" onChange={(e) => e.target.value && act(() => api.grant(k.key_id, e.target.value))()}>
                      <option value="">grant server…</option>
                      {servers
                        .filter((s) => !k.server_ids.includes(s.id))
                        .map((s) => (
                          <option key={s.id} value={s.id}>{s.slug}</option>
                        ))}
                    </select>
                  )}
                </td>
                <td>{!k.revoked_at && <button onClick={act(() => api.revokeKey(k.key_id))}>Revoke</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
