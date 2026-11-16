/** Clamp + slice a list into a 1-based page. */
export function paginate<T>(
  items: readonly T[],
  page: number,
  pageSize: number,
): { pageCount: number; safePage: number; pageItems: T[]; rangeStart: number; rangeEnd: number } {
  const size = Math.max(1, Math.floor(pageSize) || 1);
  const pageCount = Math.max(1, Math.ceil(items.length / size));
  const requested = Number.isFinite(page) ? Math.floor(page) : 1;
  const safePage = Math.min(pageCount, Math.max(1, requested));
  const start = (safePage - 1) * size;
  const pageItems = items.slice(start, start + size);
  const rangeStart = items.length === 0 ? 0 : start + 1;
  const rangeEnd = start + pageItems.length;
  return { pageCount, safePage, pageItems, rangeStart, rangeEnd };
}
