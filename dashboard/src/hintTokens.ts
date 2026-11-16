/** Pure tokenizer for catalog `setup_hints` — no React / DOM. */

export type HintText = { kind: "text"; text: string };
export type HintLink = { kind: "link"; label: string; href: string };
export type HintSegment = HintText | HintLink;

/** Only match https links — other schemes stay literal text (incl. javascript:). */
const HTTPS_LINK_RE = /\[([^\]]*)\]\((https:\/\/[^)\s]+)\)/g;

/** Allow only https:// absolute URLs (defense in depth vs gateway validation). */
export function isSafeHintHref(href: string): boolean {
  const h = href.trim();
  if (!h.startsWith("https://")) return false;
  try {
    const u = new URL(h);
    return u.protocol === "https:";
  } catch {
    return false;
  }
}

/**
 * Split a hint string into text + link segments.
 * Trailing punctuation after `)` (e.g. `[CLI](url).`) stays as text.
 * Non-https targets are left as literal text (never become anchors).
 */
export function tokenizeSetupHint(raw: string): HintSegment[] {
  const input = raw ?? "";
  if (!input) return [];
  const out: HintSegment[] = [];
  let last = 0;
  HTTPS_LINK_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = HTTPS_LINK_RE.exec(input)) !== null) {
    const label = match[1] ?? "";
    const href = (match[2] ?? "").trim();
    if (match.index > last) {
      out.push({ kind: "text", text: input.slice(last, match.index) });
    }
    if (isSafeHintHref(href)) {
      out.push({ kind: "link", label, href });
    } else {
      out.push({ kind: "text", text: match[0] });
    }
    last = match.index + match[0].length;
  }
  if (last < input.length) {
    out.push({ kind: "text", text: input.slice(last) });
  }
  return out;
}
