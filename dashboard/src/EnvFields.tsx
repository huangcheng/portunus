import type { EnvRow } from "./envRows";

type Props = {
  defs: EnvRow[];
  values: Record<string, string>;
  onChange: (name: string, value: string) => void;
  /** Connected / already vaulted — blank means keep. */
  active: boolean;
  /** Vaulted env names (values never returned). Used for non-secret "saved" hints. */
  storedNames?: ReadonlySet<string>;
  /** Multiline secret reveal map (stdio). Omit / empty for skill_bridge. */
  revealed?: Record<string, boolean>;
  onReveal?: (name: string) => void;
};

/**
 * Shared env/secret field chrome for stdio + skill_bridge Connect panels.
 * Presentation only: panels own values / reveal / save blank-to-keep semantics.
 * Masked `••••` must never be written into `values`.
 */
export function EnvFields({
  defs,
  values,
  onChange,
  active,
  storedNames,
  revealed,
  onReveal,
}: Props) {
  return (
    <>
      {defs.map((def) => {
        const keep = active ? "(leave blank to keep)" : "";
        if (def.multiline) {
          return (
            <div className="field" key={def.name}>
              {def.label} {keep}
              {renderControl(def, values, onChange, active, storedNames, revealed, onReveal)}
            </div>
          );
        }
        return (
          <label className="field" key={def.name}>
            {def.label} {keep}
            {renderControl(def, values, onChange, active, storedNames, revealed, onReveal)}
          </label>
        );
      })}
    </>
  );
}

function renderControl(
  def: EnvRow,
  values: Record<string, string>,
  onChange: (name: string, value: string) => void,
  active: boolean,
  storedNames: ReadonlySet<string> | undefined,
  revealed: Record<string, boolean> | undefined,
  onReveal: ((name: string) => void) | undefined,
) {
  if (def.multiline) {
    // Mask-by-default only when an existing stored value may exist.
    if (active && def.secret && !(revealed?.[def.name])) {
      return (
        <div className="multiline-secret">
          <input type="password" value="••••••••••••" readOnly tabIndex={-1} />
          <button type="button" className="ghost sm" onClick={() => onReveal?.(def.name)}>
            Edit config
          </button>
        </div>
      );
    }
    return (
      <textarea
        aria-label={def.label}
        rows={7}
        value={values[def.name] || ""}
        onChange={(e) => onChange(def.name, e.target.value)}
        required={!active && def.secret}
        autoComplete="off"
        spellCheck={false}
        placeholder={active ? undefined : "Paste the multi-line configuration value"}
      />
    );
  }
  const stored = !!storedNames?.has(def.name);
  const nonSecretStoredHint =
    !def.secret && stored && !(values[def.name] || "").trim()
      ? "Saved — leave blank to keep"
      : undefined;
  return (
    <input
      type={def.secret ? "password" : "text"}
      value={values[def.name] || ""}
      onChange={(e) => onChange(def.name, e.target.value)}
      // Secrets are HTML-required on first Connect. Skill-bridge Connect also
      // enforces non-secret fields in its submit handler; stdio panels leave them optional.
      required={!active && def.secret}
      autoComplete="off"
      placeholder={
        nonSecretStoredHint ?? (active && def.secret ? "••••••••" : undefined)
      }
    />
  );
}
