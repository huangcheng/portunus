import { useState, type FormEvent } from "react";
import { api } from "./api";

export function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await api.login(password);
      onLogin();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <main>
      <section>
        <h2>Portunus operator login</h2>
        <form onSubmit={submit}>
          <input type="password" autoFocus placeholder="Password" value={password} onChange={(e) => setPassword(e.target.value)} />
          <button type="submit">Log in</button>
        </form>
        {error && <p className="error">{error}</p>}
      </section>
    </main>
  );
}
