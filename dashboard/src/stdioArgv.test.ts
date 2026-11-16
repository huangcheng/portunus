import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  normalizeStdioArgs,
  prepareStdioArgv,
  validateStdioArgs,
} from "./stdioArgv.ts";

describe("normalizeStdioArgs", () => {
  it("rebuilds -y --ignore-scripts prefix for npx", () => {
    assert.deepEqual(normalizeStdioArgs("npx", ["pkg@1.0.0"]), [
      "-y",
      "--ignore-scripts",
      "pkg@1.0.0",
    ]);
  });
  it("dedupes misplaced -y", () => {
    assert.deepEqual(
      normalizeStdioArgs("npx", ["--ignore-scripts", "-y", "pkg@1.0.0"]),
      ["-y", "--ignore-scripts", "pkg@1.0.0"],
    );
  });
  it("leaves non-npx alone", () => {
    assert.deepEqual(normalizeStdioArgs("uvx", ["pkg==1.0.0"]), ["pkg==1.0.0"]);
  });
});

describe("validateStdioArgs", () => {
  it("accepts pinned npx", () => {
    assert.equal(
      validateStdioArgs("npx", ["-y", "--ignore-scripts", "demo@1.2.3"]),
      null,
    );
  });
  it("rejects trailing npx flags", () => {
    const err = validateStdioArgs("npx", [
      "-y",
      "--ignore-scripts",
      "demo@1.2.3",
      "--registry=http://evil",
    ]);
    assert.match(err ?? "", /unexpected npx flag/);
  });
  it("accepts pinned uvx", () => {
    assert.equal(validateStdioArgs("uvx", ["demo==1.2.3"]), null);
  });
  it("rejects trailing uvx flags", () => {
    const err = validateStdioArgs("uvx", ["demo==1.2.3", "--index-url=http://evil"]);
    assert.match(err ?? "", /unexpected uvx flag/);
  });
});

describe("prepareStdioArgv", () => {
  it("normalizes and validates", () => {
    const { argv, error } = prepareStdioArgv("npx", "pkg@1.0.0");
    assert.equal(error, null);
    assert.deepEqual(argv, ["-y", "--ignore-scripts", "pkg@1.0.0"]);
  });
});
