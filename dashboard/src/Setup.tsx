import { useState, type FormEvent } from "react";
import { api, type AuthStatus } from "./api";
import { AuthFrame } from "./Login";

export function Setup({ status, onSetup }: { status: AuthStatus; onSetup: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [bootstrap, setBootstrap] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (password !== confirm) {
      setError("The passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      await api.setup({
        username: username.trim(),
        password,
        ...(status.bootstrap_required ? { bootstrap_password: bootstrap } : {}),
      });
      onSetup();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Setup failed");
      setBusy(false);
    }
  }

  return (
    <AuthFrame
      eyebrow="First run"
      title="Create the operator."
      lede="This username and password become the console credentials. The shared environment password stops working as a login."
      foot="After setup, turn on two-factor and a passkey from Account"
    >
      <form onSubmit={submit}>
        <label className="field">
          Username
          <input
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            minLength={3}
            maxLength={64}
          />
        </label>
        <label className="field">
          Password
          <input
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={8}
          />
        </label>
        <label className="field">
          Confirm password
          <input
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
            minLength={8}
          />
        </label>
        {status.bootstrap_required && (
          <label className="field">
            Current environment password
            <input
              type="password"
              autoComplete="current-password"
              value={bootstrap}
              onChange={(e) => setBootstrap(e.target.value)}
              required
            />
          </label>
        )}
        <button className="primary" type="submit" disabled={busy}>
          {busy ? "Creating…" : "Create account"}
        </button>
        {error && <div className="notice error">{error}</div>}
      </form>
    </AuthFrame>
  );
}
