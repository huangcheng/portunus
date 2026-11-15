export type EnvRow = {
  name: string;
  label: string;
  secret: boolean;
  multiline: boolean;
};

/** Parse the server-side stdio env / secrets contract: name/label/secret(+multiline).
 * Bare env-name strings (legacy TOML) become secret rows with label=name. */
export function parseEnvRows(raw: unknown): EnvRow[] {
  if (!Array.isArray(raw)) return [];
  const out: EnvRow[] = [];
  for (const item of raw) {
    if (typeof item === "string" && item.trim()) {
      const name = item.trim();
      out.push({ name, label: name, secret: true, multiline: false });
      continue;
    }
    if (!item || typeof item !== "object") continue;
    const name = (item as { name?: unknown }).name;
    if (typeof name !== "string" || !name.trim()) continue;
    const label = (item as { label?: unknown }).label;
    const secret = (item as { secret?: unknown }).secret;
    const multiline = (item as { multiline?: unknown }).multiline;
    out.push({
      name: name.trim(),
      label: typeof label === "string" && label.trim() ? label.trim() : name.trim(),
      secret: secret !== false,
      multiline: multiline === true,
    });
  }
  return out;
}
