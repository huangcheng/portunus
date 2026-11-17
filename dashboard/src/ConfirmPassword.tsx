import { useEffect, useId, useRef, useState, type FormEvent } from "react";

type AskOpts = {
  title?: string;
  message?: string;
};

type Pending = AskOpts & {
  resolve: (password: string | null) => void;
};

type AskFn = (opts?: AskOpts) => Promise<string | null>;

let askImpl: AskFn | null = null;

/** Collect operator password in-UI. Returns null when cancelled. */
export function confirmPassword(opts?: AskOpts): Promise<string | null> {
  if (!askImpl) return Promise.resolve(null);
  return askImpl(opts);
}

export function setConfirmPasswordAsk(fn: AskFn | null) {
  askImpl = fn;
}

/**
 * Global host for step-up password collection. Mount once inside the authenticated shell.
 * Styles: `.confirm-password-*` in style.css.
 */
export function ConfirmPasswordHost() {
  const [pending, setPending] = useState<Pending | null>(null);
  const [password, setPassword] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    const ask: AskFn = (opts) =>
      new Promise((resolve) => {
        setPassword("");
        setPending((prev) => {
          prev?.resolve(null);
          return {
            title: opts?.title,
            message: opts?.message,
            resolve,
          };
        });
      });
    setConfirmPasswordAsk(ask);
    return () => {
      setConfirmPasswordAsk(null);
      setPending((prev) => {
        prev?.resolve(null);
        return null;
      });
    };
  }, []);

  useEffect(() => {
    if (!pending) return;
    inputRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        pending.resolve(null);
        setPending(null);
        setPassword("");
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [pending]);

  if (!pending) return null;

  function finish(value: string | null) {
    pending?.resolve(value);
    setPending(null);
    setPassword("");
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!password) return;
    finish(password);
  }

  return (
    <div className="confirm-password-root" role="presentation">
      <button
        type="button"
        className="confirm-password-backdrop"
        aria-label="Cancel"
        onClick={() => finish(null)}
      />
      <div
        className="confirm-password-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
      >
        <h3 id={titleId}>{pending.title ?? "Confirm password"}</h3>
        <p id={descId} className="muted">
          {pending.message ?? "Re-enter your operator password to continue."}
        </p>
        <form className="stack" onSubmit={submit}>
          <label className="field">
            Operator password
            <input
              ref={inputRef}
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          <div className="row">
            <button className="primary sm" type="submit" disabled={!password}>
              Continue
            </button>
            <button className="ghost" type="button" onClick={() => finish(null)}>
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
