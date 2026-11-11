/** Format an ISO / parseable timestamp for operator tables. */
export function fmtTimestamp(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

/** Only allow http(s) trace deep-links from the gateway template. */
export function traceHref(template: string, traceId: string): string | null {
  if (!/^https?:\/\//i.test(template)) return null;
  return template.replace("{trace_id}", encodeURIComponent(traceId));
}
