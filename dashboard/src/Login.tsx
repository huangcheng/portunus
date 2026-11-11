import { useState, type FormEvent } from "react";
import { api } from "./api";
import { BrandMark } from "./icons/ui";

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
    <main className="auth-shell">
      <section className="auth-brand-panel" aria-label="Portunus introduction">
        <div className="auth-brand">
          <BrandMark />
          <span className="name">Portunus</span>
        </div>
        <div className="auth-brand-copy">
          <p className="auth-eyebrow">MCP control plane</p>
          <h2>Every tool.<br />One gateway.</h2>
          <p>Connect your apps, arm your agents, and keep every credential in one place.</p>
        </div>
        <div className="auth-gateway-art" aria-hidden="true">
          <span className="auth-arch arch-outer" />
          <span className="auth-arch arch-middle" />
          <span className="auth-arch arch-inner" />
          <span className="auth-threshold" />
        </div>
        <div className="auth-brand-foot">
          <span className="environment-dot" aria-hidden />
          Private by design
        </div>
      </section>
      <section className="auth-form-panel" aria-labelledby="login-title">
        <div className="auth-card">
          <p className="auth-eyebrow">Operator access</p>
          <h1 id="login-title">Welcome back.</h1>
          <p className="lede">Enter your password to open the control room.</p>
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
              {busy ? "Signing in…" : "Enter control room"}
            </button>
            {error && <div className="notice error">{error}</div>}
          </form>
        </div>
        <p className="auth-foot">Single-operator · Secured session</p>
      </section>
    </main>
  );
}
