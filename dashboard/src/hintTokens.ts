/** Pure tokenizer for catalog `setup_hints` — no React / DOM. */

export type HintText = { kind: "text"; text: string };
export type HintLink = { kind: "link"; label: string; href: string };
export type HintCode = { kind: "code"; text: string };
export type HintBold = { kind: "bold"; text: string };
export type HintSegment = HintText | HintLink | HintCode | HintBold;

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

/** Apply `**bold**` only inside a plain run (no links/code). */
function tokenizeBold(text: string): HintSegment[] {
  if (!text) return [];
  const re = /\*\*([^*]+)\*\*/g;
  const out: HintSegment[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) {
      out.push({ kind: "text", text: text.slice(last, match.index) });
    }
    out.push({ kind: "bold", text: match[1] ?? "" });
    last = match.index + match[0].length;
  }
  if (last < text.length) {
    out.push({ kind: "text", text: text.slice(last) });
  }
  return out;
}

/**
 * Inline `` `code` `` then `**bold**` inside a plain-text span (no nested links).
 * Code is tokenized first so `` ` `` wins over surrounding `**`.
 * Limitation (by design): bold must not wrap code or markdown links — leftover `**`
 * markers stay literal (catalog hints keep bold and links adjacent, not nested).
 */
function tokenizeInline(text: string): HintSegment[] {
  if (!text) return [];
  const codeRe = /`([^`]+)`/g;
  const out: HintSegment[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = codeRe.exec(text)) !== null) {
    if (match.index > last) {
      out.push(...tokenizeBold(text.slice(last, match.index)));
    }
    out.push({ kind: "code", text: match[1] ?? "" });
    last = match.index + match[0].length;
  }
  if (last < text.length) {
    out.push(...tokenizeBold(text.slice(last)));
  }
  return out;
}
/**
 * Split a hint string into text + link + code + bold segments.
 * Trailing punctuation after `)` (e.g. `[CLI](url).`) stays as text.
 * Non-https targets are left as literal text (never become anchors).
 * URLs containing `(` are left literal (markdown link closer is the first `)`).
 * Links are split first, so bold cannot wrap a `[label](https://…)` link.
 */
export function tokenizeSetupHint(raw: string): HintSegment[] {
  const input = raw ?? "";
  if (!input) return [];
  // Local regex: avoid shared `g` lastIndex across calls.
  const linkRe = /\[([^\]]*)\]\((https:\/\/[^)\s(]+)\)/g;
  const out: HintSegment[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = linkRe.exec(input)) !== null) {
    const label = match[1] ?? "";
    const href = (match[2] ?? "").trim();
    if (match.index > last) {
      out.push(...tokenizeInline(input.slice(last, match.index)));
    }
    if (isSafeHintHref(href)) {
      out.push({ kind: "link", label, href });
    } else {
      out.push(...tokenizeInline(match[0]));
    }
    last = match.index + match[0].length;
  }
  if (last < input.length) {
    out.push(...tokenizeInline(input.slice(last)));
  }
  return out;
}
