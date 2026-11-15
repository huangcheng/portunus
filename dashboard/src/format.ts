function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/**
 * Format an ISO / parseable timestamp for operator tables.
 * Always local timezone as ``YYYY-MM-DD HH:mm:ss`` (browser zone).
 */
export function fmtTimestamp(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
  );
}

/** Only allow http(s) trace deep-links from the gateway template. */
export function traceHref(template: string, traceId: string): string | null {
  if (!/^https?:\/\//i.test(template)) return null;
  return template.replace("{trace_id}", encodeURIComponent(traceId));
}
