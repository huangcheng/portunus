const segmenter =
  typeof Intl !== "undefined" && "Segmenter" in Intl
    ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
    : null;

/** First grapheme of a display name (Latin uppercased; CJK left as-is). */
export function appInitial(label: string | undefined | null, fallback = "?"): string {
  const s = (label ?? "").trim();
  if (!s) return fallback;
  let ch: string | undefined;
  if (segmenter) {
    for (const part of segmenter.segment(s)) {
      ch = part.segment;
      break;
    }
  }
  ch ??= Array.from(s)[0];
  if (!ch) return fallback;
  return /^[a-z]$/i.test(ch) ? ch.toUpperCase() : ch;
}

/** Stable 0–359 hue from a seed (slug/label) so the same app keeps its color. */
export function appInitialHue(seed: string | undefined | null): number {
  const s = (seed ?? "").trim() || "?";
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) % 360;
}
