import { useEffect, useState } from "react";
import { api } from "./api";
import { KeysPage } from "./KeysPage";
import { Login } from "./Login";
import { ServersPage } from "./ServersPage";

type View = "servers" | "keys";

export function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [view, setView] = useState<View>("servers");

  useEffect(() => {
    api.session().then(
      () => setAuthed(true),
      () => setAuthed(false),
    );
  }, []);

  if (authed === null) return <main className="muted">Loading…</main>;
  if (!authed) return <Login onLogin={() => setAuthed(true)} />;

  return (
    <>
      <nav>
        <strong>Portunus</strong>
        <button onClick={() => setView("servers")} disabled={view === "servers"}>Servers</button>
        <button onClick={() => setView("keys")} disabled={view === "keys"}>Agent keys</button>
        <span className="spacer" />
        <button onClick={() => api.logout().finally(() => setAuthed(false))}>Log out</button>
      </nav>
      <main>{view === "servers" ? <ServersPage /> : <KeysPage />}</main>
    </>
  );
}
