import { useState, type FormEvent } from "react";
import { api } from "./api";

export function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await api.login(password);
      onLogin();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="auth-shell">
      <div className="auth-brand">
        <span className="mark" aria-hidden>
          P
        </span>
        <span className="name">Portunus</span>
      </div>
      <div className="auth-card">
        <h1>Sign in</h1>
        <p className="lede">Gateway console — operator access</p>
        <form onSubmit={submit}>
          <label className="field">
            Password
            <input
              type="password"
              autoFocus
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          <button className="primary" type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
          {error && <p className="error">{error}</p>}
        </form>
      </div>
      <p className="auth-foot">Single-operator console for this MCP gateway</p>
    </div>
  );
}
