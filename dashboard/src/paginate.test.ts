import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { paginate } from "./paginate.ts";

describe("paginate", () => {
  const items = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13];

  it("returns empty page for empty list", () => {
    const out = paginate([], 1, 12);
    assert.deepEqual(out, {
      pageCount: 1,
      safePage: 1,
      pageItems: [],
      rangeStart: 0,
      rangeEnd: 0,
    });
  });

  it("fits exactly one page when length equals pageSize", () => {
    const twelve = items.slice(0, 12);
    const out = paginate(twelve, 1, 12);
    assert.equal(out.pageCount, 1);
    assert.equal(out.safePage, 1);
    assert.deepEqual(out.pageItems, twelve);
    assert.equal(out.rangeStart, 1);
    assert.equal(out.rangeEnd, 12);
  });

  it("slices a partial last page", () => {
    const out = paginate(items, 2, 12);
    assert.equal(out.pageCount, 2);
    assert.equal(out.safePage, 2);
    assert.deepEqual(out.pageItems, [13]);
    assert.equal(out.rangeStart, 13);
    assert.equal(out.rangeEnd, 13);
  });

  it("clamps a stored page above pageCount", () => {
    const twenty = Array.from({ length: 20 }, (_, i) => i + 1);
    const out = paginate(twenty, 9, 12);
    assert.equal(out.pageCount, 2);
    assert.equal(out.safePage, 2);
    assert.equal(out.pageItems.length, 8);
  });

  it("clamps non-positive and non-finite page to 1", () => {
    assert.equal(paginate(items, 0, 12).safePage, 1);
    assert.equal(paginate(items, -3, 12).safePage, 1);
    assert.equal(paginate(items, Number.NaN, 12).safePage, 1);
  });

  it("floors fractional pages and sanitizes invalid pageSize", () => {
    assert.equal(paginate(items, 1.9, 12).safePage, 1);
    assert.deepEqual(paginate(items, 2, 0).pageItems, items.slice(0, 1));
    assert.equal(paginate(items, 2, Number.NaN).pageCount, items.length);
    assert.equal(paginate(items, 2, -5).pageCount, items.length);
  });
});
