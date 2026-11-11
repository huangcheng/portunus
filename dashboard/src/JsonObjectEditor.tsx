import { useEffect, useId, useState } from "react";

type Props = {
  label: string;
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  rows?: number;
  /** stringMap: object of string values (headers). object: any JSON object (manifests). */
  valueMode?: "stringMap" | "object";
  /** When set, surface parse errors to the parent (e.g. disable submit). */
  onValidityChange?: (ok: boolean) => void;
};

/** Minimal JSON object editor — monospace, validate + format, no heavy deps. */
export function JsonObjectEditor({
  label,
  value,
  onChange,
  placeholder = '{\n  "Authorization": "Bearer …"\n}',
  rows = 8,
  valueMode = "stringMap",
  onValidityChange,
}: Props) {
  const id = useId();
  const [error, setError] = useState("");

  useEffect(() => {
    const trimmed = value.trim();
    if (!trimmed) {
      setError("");
      onValidityChange?.(true);
      return;
    }
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        setError("Must be a JSON object");
        onValidityChange?.(false);
        return;
      }
      if (valueMode === "object") {
        setError("");
        onValidityChange?.(true);
        return;
      }
      for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof k !== "string" || !k.trim()) {
          setError("Header names must be non-empty strings");
          onValidityChange?.(false);
          return;
        }
        if (typeof v !== "string") {
          setError(`Value for “${k}” must be a string`);
          onValidityChange?.(false);
          return;
        }
      }
      setError("");
      onValidityChange?.(true);
    } catch {
      setError("Invalid JSON");
      onValidityChange?.(false);
    }
  }, [value, valueMode, onValidityChange]);

  function format() {
    const trimmed = value.trim();
    if (!trimmed) return;
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return;
      onChange(JSON.stringify(parsed, null, 2) + "\n");
    } catch {
      /* keep as-is; error already shown */
    }
  }

  return (
    <div className="json-editor">
      <div className="json-editor-bar">
        <label htmlFor={id}>{label}</label>
        <button type="button" className="ghost sm" onClick={format} disabled={!value.trim()}>
          Format
        </button>
      </div>
      <textarea
        id={id}
        className="json-editor-input"
        spellCheck={false}
        rows={rows}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={!!error}
      />
      {error ? <p className="json-editor-error">{error}</p> : null}
    </div>
  );
}

/** Parse a JSON object of string values; empty → {}. Throws on bad input. */
export function parseJsonStringMap(raw: string): Record<string, string> {
  const trimmed = raw.trim();
  if (!trimmed) return {};
  const parsed = JSON.parse(trimmed) as unknown;
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("headers must be a JSON object");
  }
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof k !== "string" || !k.trim()) throw new Error("header names must be non-empty strings");
    if (typeof v !== "string") throw new Error(`header “${k}” value must be a string`);
    out[k.trim()] = v;
  }
  return out;
}
