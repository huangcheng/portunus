import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { appInitial, appInitialHue } from "./appInitial.ts";

describe("appInitial", () => {
  it("uppercases a Latin first letter", () => {
    assert.equal(appInitial("github"), "G");
    assert.equal(appInitial("  Dropbox"), "D");
  });

  it("keeps a Chinese first character", () => {
    assert.equal(appInitial("智谱视觉理解"), "智");
    assert.equal(appInitial("微信读书"), "微");
  });

  it("falls back for empty input", () => {
    assert.equal(appInitial(""), "?");
    assert.equal(appInitial(null), "?");
    assert.equal(appInitial("   ", "!"), "!");
  });
});

describe("appInitialHue", () => {
  it("is stable for the same seed", () => {
    assert.equal(appInitialHue("zhipu-vision"), appInitialHue("zhipu-vision"));
  });

  it("stays in 0..359", () => {
    for (const seed of ["a", "智谱", "qqmusic", ""]) {
      const hue = appInitialHue(seed);
      assert.ok(hue >= 0 && hue < 360);
    }
  });
});
