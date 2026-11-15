import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ApiError, api, type AuthStatus } from "./api";
import { BrandMark } from "./icons/ui";
import { b64urlToBuffer, bufferToB64url } from "./webauthn";

const TURNSTILE_SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

type TurnstileApi = {
  render: (
    el: HTMLElement,
    opts: {
      sitekey: string;
      action?: string;
      theme?: "light" | "dark" | "auto";
      size?: "normal" | "compact" | "flexible";
      callback?: (token: string) => void;
      "expired-callback"?: () => void;
      "error-callback"?: () => void;
    },
  ) => string;
  reset: (widgetId?: string) => void;
  remove: (widgetId?: string) => void;
};

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

function loadTurnstile(): Promise<TurnstileApi> {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  return new Promise((resolve, reject) => {
    const src = TURNSTILE_SCRIPT.split("?")[0];
    const existing = document.querySelector<HTMLScriptElement>(`script[src^="${src}"]`);
    const done = () => {
      if (window.turnstile) resolve(window.turnstile);
      else reject(new Error("Turnstile did not load"));
    };
    if (existing) {
      const wait = () => (window.turnstile ? done() : window.setTimeout(wait, 50));
      wait();
      return;
    }
    const script = document.createElement("script");
    script.src = TURNSTILE_SCRIPT;
    script.async = true;
    script.onload = () => done();
    script.onerror = () => reject(new Error("failed to load Turnstile"));
    document.head.appendChild(script);
  });
}

function AuthFrame({
  eyebrow,
  title,
  lede,
  children,
  foot,
}: {
  eyebrow: string;
  title: string;
  lede: string;
  children: ReactNode;
  foot: string;
}) {
  return (
    <main className="auth-shell">
      <section className="auth-brand-panel" aria-label="Portunus introduction">
        <div className="auth-brand">
          <BrandMark />
          <span className="name">Portunus</span>
        </div>
        <div className="auth-brand-copy">
          <p className="auth-eyebrow">MCP control plane</p>
          <h2>
            Every tool.
            <br />
            One gateway.
          </h2>
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
          <p className="auth-eyebrow">{eyebrow}</p>
          <h1 id="login-title">{title}</h1>
          <p className="lede">{lede}</p>
          {children}
        </div>
        <p className="auth-foot">{foot}</p>
      </section>
    </main>
  );
}

export function Login({ status, onLogin }: { status: AuthStatus; onLogin: () => void }) {
  const sitekey = status.turnstile_sitekey ?? "";
  const [username, setUsername] = useState(status.username ?? "");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null);
  const widgetId = useRef<string | null>(null);
  const slot = useRef<HTMLDivElement | null>(null);

  function resetWidget() {
    if (widgetId.current && window.turnstile) {
      window.turnstile.reset(widgetId.current);
      setTurnstileToken(null);
    }
  }

  useEffect(() => {
    if (!sitekey || challenge || !slot.current) return;
    let cancelled = false;
    loadTurnstile()
      .then((ts) => {
        if (cancelled || !slot.current || widgetId.current) return;
        widgetId.current = ts.render(slot.current, {
          sitekey,
          action: "login",
          theme: "auto",
          size: "flexible",
          callback: (token) => setTurnstileToken(token),
          "expired-callback": () => setTurnstileToken(null),
          "error-callback": () => setTurnstileToken(null),
        });
      })
      .catch(() => setError("Human verification could not load. Refresh and try again."));
    return () => {
      cancelled = true;
      if (widgetId.current && window.turnstile) {
        window.turnstile.remove(widgetId.current);
        widgetId.current = null;
      }
    };
  }, [sitekey, challenge]);

  async function submitPassword(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (sitekey && !turnstileToken) {
      setError("Complete the human verification first.");
      return;
    }
    setBusy(true);
    try {
      await api.login({
        username: username.trim(),
        password,
        ...(turnstileToken ? { turnstile_token: turnstileToken } : {}),
      });
      onLogin();
    } catch (err) {
      if (err instanceof ApiError && err.code === "totp_required") {
        setChallenge(String(err.data.challenge_token ?? ""));
        setCode("");
        setError("");
      } else {
        setError(err instanceof Error ? err.message : "Sign in failed");
        resetWidget();
      }
      setBusy(false);
    }
  }

  async function submitTotp(e: FormEvent) {
    e.preventDefault();
    if (!challenge) return;
    setError("");
    setBusy(true);
    try {
      const res = await api.loginTotp(challenge, code.trim());
      if (typeof res.recovery_remaining === "number") {
        setError("");
      }
      onLogin();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Verification failed");
      setBusy(false);
    }
  }

  async function passkeyLogin() {
    setError("");
    setBusy(true);
    try {
      const opts = await api.passkeyAssertionOptions();
      const cred = (await navigator.credentials.get({
        publicKey: {
          challenge: b64urlToBuffer(opts.challenge),
          rpId: opts.rpId,
          allowCredentials: opts.allowCredentials.map((c) => ({
            type: "public-key" as const,
            id: b64urlToBuffer(c.id),
          })),
          userVerification: "preferred",
          timeout: opts.timeout,
        },
      })) as PublicKeyCredential | null;
      if (!cred) throw new Error("Passkey was cancelled");
      const response = cred.response as AuthenticatorAssertionResponse;
      await api.passkeyLogin({
        id: cred.id,
        challenge: opts.challenge,
        response: {
          clientDataJSON: bufferToB64url(response.clientDataJSON),
          authenticatorData: bufferToB64url(response.authenticatorData),
          signature: bufferToB64url(response.signature),
        },
      });
      onLogin();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Passkey sign in failed");
      setBusy(false);
    }
  }

  return (
    <AuthFrame
      eyebrow="Operator access"
      title={challenge ? "Two-factor." : "Welcome back."}
      lede={
        challenge
          ? "Enter the 6-digit code from your authenticator, or paste a recovery key."
          : "Username and password open the control room."
      }
      foot="Single-operator · Secured session"
    >
      {challenge ? (
        <form onSubmit={submitTotp}>
          <label className="field">
            Authenticator code
            <input
              inputMode="text"
              autoComplete="one-time-code"
              autoFocus
              value={code}
              onChange={(e) => setCode(e.target.value)}
              required
            />
          </label>
          <button className="primary" type="submit" disabled={busy}>
            {busy ? "Checking…" : "Verify and enter"}
          </button>
          <button
            className="ghost"
            type="button"
            onClick={() => {
              setChallenge(null);
              setCode("");
              setError("");
            }}
          >
            Back to password
          </button>
          {error && <div className="notice error">{error}</div>}
        </form>
      ) : (
        <form onSubmit={submitPassword}>
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
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {sitekey && <div className="turnstile-slot" ref={slot} />}
          <button className="primary" type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Enter control room"}
          </button>
          {status.has_passkey && (
            <button className="outline" type="button" disabled={busy} onClick={() => void passkeyLogin()}>
              Sign in with a passkey
            </button>
          )}
          {error && <div className="notice error">{error}</div>}
        </form>
      )}
    </AuthFrame>
  );
}

export { AuthFrame };
