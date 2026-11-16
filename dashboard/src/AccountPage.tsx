import { useCallback, useEffect, useState, type FormEvent } from "react";
import QRCode from "qrcode";
import { api, type AuthStatus, type PasskeyInfo, type RecoveryStatus } from "./api";
import { Icon } from "./icons/ui";
import { b64urlToBuffer, bufferToB64url } from "./webauthn";

type Section = "signin" | "totp" | "recovery" | "passkeys";

const SECTIONS: { id: Section; label: string }[] = [
  { id: "signin", label: "Sign-in" },
  { id: "totp", label: "Two-factor" },
  { id: "recovery", label: "Recovery" },
  { id: "passkeys", label: "Passkeys" },
];

function QrFigure({ text }: { text: string }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    let alive = true;
    if (!text) {
      setUrl("");
      return;
    }
    QRCode.toDataURL(text, { width: 480, margin: 2 })
      .then((next) => {
        if (alive) setUrl(next);
      })
      .catch(() => {
        if (alive) setUrl("");
      });
    return () => {
      alive = false;
    };
  }, [text]);
  return url ? (
    <img className="qr-frame" src={url} width={176} height={176} alt="Authenticator QR code" />
  ) : (
    <div className="qr-frame qr-pending" aria-hidden />
  );
}

function RecoveryCodes({ codes }: { codes: string[] }) {
  function download() {
    const text = [
      "Portunus — recovery keys",
      "Each key works once, in place of an authenticator code.",
      "",
      ...codes,
      "",
    ].join("\n");
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "portunus-recovery-keys.txt";
    a.click();
    URL.revokeObjectURL(url);
  }
  return (
    <div className="stack">
      <div className="notice warn">Shown only this once. Save them before leaving this section.</div>
      <div className="recovery-grid">
        {codes.map((code) => (
          <code key={code}>{code}</code>
        ))}
      </div>
      <div className="row">
        <button type="button" className="outline sm" onClick={() => void navigator.clipboard.writeText(codes.join("\n"))}>
          <Icon name="copy" size={14} />
          Copy all
        </button>
        <button type="button" className="ghost" onClick={download}>
          Download .txt
        </button>
      </div>
    </div>
  );
}

export function AccountPage() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [passkeys, setPasskeys] = useState<PasskeyInfo[]>([]);
  const [recovery, setRecovery] = useState<RecoveryStatus | null>(null);
  const [section, setSection] = useState<Section>("signin");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const [s, p, r] = await Promise.all([api.authStatus(), api.passkeys(), api.recovery()]);
      setStatus(s);
      setPasskeys(p.passkeys ?? []);
      setRecovery(r);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the account");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const initial = (status?.username ?? "?").slice(0, 1).toUpperCase();

  return (
    <div className="stack account-page">
      {error && <div className="notice error">{error}</div>}
      <section className="account-identity" aria-label="Operator">
        <div className="account-who">
          <div className="account-avatar" aria-hidden>
            {initial}
          </div>
          <div>
            <h2>{status?.username ?? "…"}</h2>
            <p>Console operator</p>
          </div>
        </div>
        <dl aria-label="Sign-in methods">
          <div>
            <dt>Password</dt>
            <dd>{status ? (status.has_password ? "Active" : "Off") : "…"}</dd>
          </div>
          <div>
            <dt>Two-factor</dt>
            <dd>{status ? (status.has_totp ? "Enabled" : "Off") : "…"}</dd>
          </div>
          <div>
            <dt>Passkeys</dt>
            <dd>{status ? passkeys.length : "…"}</dd>
          </div>
        </dl>
      </section>

      <div className="account-layout">
        <nav className="account-nav" aria-label="Account settings">
          {SECTIONS.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              aria-current={section === id ? "page" : undefined}
              onClick={() => setSection(id)}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="account-panel">
          {section === "signin" && <PasswordSection status={status} onDone={load} />}
          {section === "totp" && <TotpSection status={status} onDone={load} />}
          {section === "recovery" && <RecoverySection status={status} recovery={recovery} onDone={load} />}
          {section === "passkeys" && <PasskeySection passkeys={passkeys} onDone={load} />}
        </div>
      </div>
    </div>
  );
}

function PasswordSection({ status, onDone }: { status: AuthStatus | null; onDone: () => void }) {
  const [editing, setEditing] = useState(false);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (next !== confirm) {
      setError("The new passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      await api.changePassword(current, next);
      setEditing(false);
      setCurrent("");
      setNext("");
      setConfirm("");
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update the password");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" aria-label="Sign-in settings">
      <header className="card-header">
        <div>
          <h2>Sign-in password</h2>
          <p className="desc">
            {status?.has_password
              ? "Your password is active. Use at least 8 characters."
              : "No password is set. Sign-in currently requires a passkey."}
          </p>
        </div>
      </header>
      <div className="card-pad">
        {editing ? (
          <form className="stack" onSubmit={submit}>
            <label className="field">
              Current password
              <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required autoFocus />
            </label>
            <label className="field">
              New password
              <input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required minLength={8} />
            </label>
            <label className="field">
              Confirm new password
              <input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required minLength={8} />
            </label>
            {error && <div className="notice error">{error}</div>}
            <div className="row">
              <button className="primary sm" type="submit" disabled={busy}>
                {busy ? "Updating…" : "Update password"}
              </button>
              <button className="ghost" type="button" onClick={() => setEditing(false)} disabled={busy}>
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <button className="outline sm" type="button" disabled={!status?.has_password} onClick={() => setEditing(true)}>
            <Icon name="key" size={14} />
            Change password
          </button>
        )}
      </div>
    </section>
  );
}

function TotpSection({ status, onDone }: { status: AuthStatus | null; onDone: () => void }) {
  const enabled = !!status?.has_totp;
  const [mode, setMode] = useState<"idle" | "scan" | "verify" | "keys" | "qr" | "disable">("idle");
  const [secret, setSecret] = useState("");
  const [uri, setUri] = useState("");
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[]>([]);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function reset() {
    setMode("idle");
    setSecret("");
    setUri("");
    setCode("");
    setPassword("");
    setError("");
  }

  async function startEnable() {
    setError("");
    setBusy(true);
    try {
      const r = await api.totpEnable();
      setSecret(r.secret);
      setUri(r.provisioning_uri);
      setMode("scan");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start two-factor");
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    setError("");
    setBusy(true);
    try {
      const r = await api.totpConfirm(secret, code.trim());
      setCodes(r.codes);
      setMode("keys");
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "That code did not match");
    } finally {
      setBusy(false);
    }
  }

  async function reveal(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const r = await api.totpQr(password);
      setSecret(r.secret);
      setUri(r.provisioning_uri);
      setPassword("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reveal the key");
    } finally {
      setBusy(false);
    }
  }

  async function disable(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await api.totpDisable(password);
      reset();
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not disable two-factor");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" aria-label="Two-factor settings">
      <header className="card-header">
        <div>
          <h2>Two-factor authentication</h2>
          <p className="desc">Require an authenticator code after password sign-in. Passkeys skip this step.</p>
        </div>
        <span className={`account-badge ${enabled ? "on" : ""}`}>{enabled ? "Enabled" : "Not enabled"}</span>
      </header>
      <div className="card-pad stack">
        {mode === "idle" && (
          <div className="row">
            {enabled ? (
              <>
                <button className="outline sm" type="button" onClick={() => setMode("qr")}>
                  Show QR and key
                </button>
                <button className="destructive sm" type="button" onClick={() => setMode("disable")}>
                  Disable…
                </button>
              </>
            ) : (
              <button className="primary sm" type="button" disabled={busy} onClick={() => void startEnable()}>
                <Icon name="shield" size={14} />
                {busy ? "Preparing…" : "Enable two-factor"}
              </button>
            )}
          </div>
        )}
        {mode === "scan" && (
          <div className="stack">
            <p className="muted">Scan with your authenticator, or enter the key by hand. Nothing is saved until the code is verified.</p>
            <div className="totp-scan">
              <QrFigure text={uri} />
              <div className="stack">
                <code className="secret-code">{secret}</code>
                <button className="outline sm" type="button" onClick={() => void navigator.clipboard.writeText(secret)}>
                  <Icon name="copy" size={14} />
                  Copy key
                </button>
              </div>
            </div>
            <div className="row">
              <button className="primary sm" type="button" onClick={() => setMode("verify")}>
                Continue
              </button>
              <button className="ghost" type="button" onClick={reset}>
                Cancel
              </button>
            </div>
          </div>
        )}
        {mode === "verify" && (
          <div className="stack">
            <label className="field">
              6-digit code
              <input
                className="totp-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                autoFocus
              />
            </label>
            <div className="row">
              <button className="primary sm" type="button" disabled={busy || code.length !== 6} onClick={() => void confirm()}>
                {busy ? "Checking…" : "Verify and enable"}
              </button>
              <button className="ghost" type="button" onClick={() => setMode("scan")} disabled={busy}>
                Back
              </button>
            </div>
          </div>
        )}
        {mode === "keys" && (
          <div className="stack">
            <RecoveryCodes codes={codes} />
            <button className="primary sm" type="button" onClick={reset}>
              Done — I saved them
            </button>
          </div>
        )}
        {mode === "qr" && (
          <form className="stack" onSubmit={reveal}>
            {uri ? (
              <div className="totp-scan">
                <QrFigure text={uri} />
                <code className="secret-code">{secret}</code>
              </div>
            ) : (
              <label className="field">
                Console password
                <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required autoFocus />
              </label>
            )}
            <div className="row">
              {!uri && (
                <button className="primary sm" type="submit" disabled={busy || !password}>
                  {busy ? "Checking…" : "Reveal"}
                </button>
              )}
              <button className="ghost" type="button" onClick={reset}>
                Close
              </button>
            </div>
          </form>
        )}
        {mode === "disable" && (
          <form className="stack" onSubmit={disable}>
            <p className="muted">Password sign-in drops back to one factor, and every recovery key is wiped.</p>
            <label className="field">
              Console password
              <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required autoFocus />
            </label>
            <div className="row">
              <button className="destructive sm" type="submit" disabled={busy || !password}>
                {busy ? "Disabling…" : "Disable two-factor"}
              </button>
              <button className="ghost" type="button" onClick={reset} disabled={busy}>
                Cancel
              </button>
            </div>
          </form>
        )}
        {error && <div className="notice error">{error}</div>}
      </div>
    </section>
  );
}

function RecoverySection({
  status,
  recovery,
  onDone,
}: {
  status: AuthStatus | null;
  recovery: RecoveryStatus | null;
  onDone: () => void;
}) {
  const enabled = !!status?.has_totp;
  const total = recovery?.total ?? 10;
  const remaining = recovery?.remaining ?? 0;
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function regenerate(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const r = await api.regenerateRecovery(password);
      setCodes(r.codes);
      setPassword("");
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not regenerate keys");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" aria-label="Recovery settings">
      <header className="card-header">
        <div>
          <h2>Recovery keys</h2>
          <p className="desc">One-time backup codes for authenticator sign-in.</p>
        </div>
        <span className="account-badge">{enabled ? `${remaining} of ${total} left` : "Requires 2FA"}</span>
      </header>
      <div className="card-pad stack">
        {!enabled ? (
          <p className="muted">Enable two-factor authentication to create recovery keys.</p>
        ) : codes ? (
          <div className="stack">
            <RecoveryCodes codes={codes} />
            <button
              className="primary sm"
              type="button"
              onClick={() => {
                setCodes(null);
                setOpen(false);
              }}
            >
              Done — I saved them
            </button>
          </div>
        ) : (
          <>
            <div className="recovery-meter" aria-hidden>
              {Array.from({ length: total }).map((_, i) => (
                <span key={i} className={i < remaining ? "on" : ""} />
              ))}
            </div>
            <p className="muted">{remaining} unused</p>
            {open ? (
              <form className="stack" onSubmit={regenerate}>
                <p className="muted">Every existing key stops working, including any saved in a password manager.</p>
                <label className="field">
                  Console password
                  <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required autoFocus />
                </label>
                {error && <div className="notice error">{error}</div>}
                <div className="row">
                  <button className="primary sm" type="submit" disabled={busy || !password}>
                    {busy ? "Replacing…" : "Regenerate"}
                  </button>
                  <button className="ghost" type="button" onClick={() => setOpen(false)} disabled={busy}>
                    Cancel
                  </button>
                </div>
              </form>
            ) : (
              <button className="outline sm" type="button" onClick={() => setOpen(true)}>
                <Icon name="shield" size={14} />
                Regenerate keys
              </button>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function PasskeySection({ passkeys, onDone }: { passkeys: PasskeyInfo[]; onDone: () => void }) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function register(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (!name.trim()) {
      setError("Name this passkey first.");
      return;
    }
    if (!password) {
      setError("Confirm your operator password first.");
      return;
    }
    setBusy(true);
    try {
      // Password already collected — keep WebAuthn create() inside the click activation window.
      const opts = await api.passkeyRegisterOptions(password);
      const cred = (await navigator.credentials.create({
        publicKey: {
          challenge: b64urlToBuffer(opts.challenge),
          rp: opts.rp,
          user: { ...opts.user, id: b64urlToBuffer(opts.user.id) },
          pubKeyCredParams: opts.pubKeyCredParams.map((p) => ({
            type: "public-key" as const,
            alg: p.alg,
          })),
          timeout: opts.timeout,
          authenticatorSelection: opts.authenticatorSelection,
          attestation: (opts.attestation ?? "none") as AttestationConveyancePreference,
        },
      })) as PublicKeyCredential | null;
      if (!cred) throw new Error("Passkey creation was cancelled");
      const response = cred.response as AuthenticatorAttestationResponse;
      await api.passkeyRegister(
        {
          name: name.trim(),
          challenge: opts.challenge,
          response: {
            clientDataJSON: bufferToB64url(response.clientDataJSON),
            attestationObject: bufferToB64url(response.attestationObject),
          },
        },
        password,
      );
      setName("");
      setPassword("");
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Registration failed");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    setError("");
    try {
      await api.deletePasskey(id);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove the passkey");
    }
  }

  return (
    <section className="card" aria-label="Passkey settings">
      <header className="card-header">
        <div>
          <h2>Passkeys</h2>
          <p className="desc">Touch ID, Windows Hello, or a security key. Phishing-resistant, and they skip the authenticator code.</p>
        </div>
        <span className="account-badge">{passkeys.length} registered</span>
      </header>
      <div className="card-pad stack">
        <form className="row" onSubmit={register}>
          <label className="field grow">
            Name a new passkey
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={64} placeholder="This laptop" />
          </label>
          <label className="field grow">
            Operator password
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          <button className="primary sm" type="submit" disabled={busy || !password}>
            {busy ? "Waiting…" : "Register"}
          </button>
        </form>
        {passkeys.length === 0 ? (
          <p className="muted">No passkeys yet.</p>
        ) : (
          <ul className="passkey-list">
            {passkeys.map((pk) => (
              <li key={pk.credential_id}>
                <div>
                  <strong>{pk.name}</strong>
                  <span>{pk.last_used_at ? `Last used ${pk.last_used_at}` : "Not used yet"}</span>
                </div>
                <button className="ghost" type="button" onClick={() => void remove(pk.credential_id)}>
                  <Icon name="trash" size={14} />
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
        {error && <div className="notice error">{error}</div>}
      </div>
    </section>
  );
}
