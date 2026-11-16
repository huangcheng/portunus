import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { choiceToRegions, regionsToChoice } from "./routingPreference.ts";

describe("regionsToChoice", () => {
  it("maps empty to any", () => {
    assert.equal(regionsToChoice([]), "any");
    assert.equal(regionsToChoice(null), "any");
    assert.equal(regionsToChoice(undefined), "any");
  });
  it("maps single domestic/overseas", () => {
    assert.equal(regionsToChoice(["domestic"]), "domestic");
    assert.equal(regionsToChoice(["overseas"]), "overseas");
  });
  it("returns null for unknown or mixed tags", () => {
    assert.equal(regionsToChoice(["region-a"]), null);
    assert.equal(regionsToChoice(["domestic", "overseas"]), null);
    assert.equal(regionsToChoice(["overseas", "region-a"]), null);
  });
});

describe("choiceToRegions", () => {
  it("round-trips known choices", () => {
    assert.deepEqual(choiceToRegions("any"), []);
    assert.deepEqual(choiceToRegions("domestic"), ["domestic"]);
    assert.deepEqual(choiceToRegions("overseas"), ["overseas"]);
  });
});
