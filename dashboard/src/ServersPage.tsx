import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type Server, type Tool } from "./api";

export function ServersPage() {
  const [servers, setServers] = useState<Server[]>([]);
  const [tools, setTools] = useState<Record<string, Tool[]>>({});
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const list = await api.servers();
      setServers(list);
      const entries = await Promise.all(list.map(async (s) => [s.id, await api.tools(s.id)] as const));
      setTools(Object.fromEntries(entries));
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

  return (
    <>
      <CreateServer onCreated={refresh} />
      {error && <p className="error">{error}</p>}
      <section>
        <h2>Servers</h2>
        {servers.length === 0 && <p className="muted">No servers yet.</p>}
        {servers.map((s) => (
          <div key={s.id}>
            <p>
              <strong>{s.name}</strong> <code>{s.slug}</code> — <code>{[s.command, ...(s.args ?? [])].join(" ")}</code>{" "}
              {s.enabled ? "enabled" : "disabled"}
              {s.env_names.length > 0 && <span className="muted"> env: {s.env_names.join(", ")}</span>}{" "}
              <button onClick={act(() => api.probe(s.id))}>Probe</button>{" "}
              <button onClick={act(() => api.setServerEnabled(s.id, !s.enabled))}>{s.enabled ? "Disable" : "Enable"}</button>
            </p>
            {(tools[s.id] ?? []).length > 0 && (
              <table>
                <tbody>
                  {tools[s.id].map((t) => (
                    <tr key={t.id}>
                      <td>{t.name}</td>
                      <td className="muted">{t.description}</td>
                      <td>
                        <button onClick={act(() => api.setToolEnabled(t.id, !t.enabled))}>{t.enabled ? "Disable" : "Enable"}</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))}
      </section>
    </>
  );
}

function CreateServer({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [command, setCommand] = useState("npx");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const [error, setError] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      // Args are whitespace-split literals; env is KEY=value per line (stored encrypted).
      const envMap: Record<string, string> = {};
      for (const line of env.split("\n").map((l) => l.trim()).filter(Boolean)) {
        const i = line.indexOf("=");
        if (i < 1) throw new Error(`bad env line: ${line.split("=")[0]}`);
        envMap[line.slice(0, i)] = line.slice(i + 1);
      }
      await api.createServer({ name, slug, command, args: args.split(/\s+/).filter(Boolean), env: envMap });
      setName("");
      setSlug("");
      setArgs("");
      setEnv("");
      onCreated();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section>
      <h2>Create stdio server</h2>
      <form onSubmit={submit}>
        <input placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} required />
        <input placeholder="slug" value={slug} onChange={(e) => setSlug(e.target.value)} required />
        <input placeholder="command (npx / uvx)" value={command} onChange={(e) => setCommand(e.target.value)} required />
        <input placeholder="args (space separated)" size={40} value={args} onChange={(e) => setArgs(e.target.value)} />
        <br />
        <textarea placeholder={"ENV_NAME=value (one per line, optional)"} rows={2} cols={50} value={env} onChange={(e) => setEnv(e.target.value)} />
        <br />
        <button type="submit">Create (disabled until enabled)</button>
      </form>
      {error && <p className="error">{error}</p>}
    </section>
  );
}
