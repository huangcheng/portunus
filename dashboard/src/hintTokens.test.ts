import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { isSafeHintHref, tokenizeSetupHint } from "./hintTokens.ts";

describe("isSafeHintHref", () => {
  it("allows https", () => {
    assert.equal(isSafeHintHref("https://example.com/a"), true);
  });
  it("rejects http, javascript, relative", () => {
    assert.equal(isSafeHintHref("http://example.com"), false);
    assert.equal(isSafeHintHref("javascript:alert(1)"), false);
    assert.equal(isSafeHintHref("/relative"), false);
    assert.equal(isSafeHintHref("//evil.example"), false);
  });
});

describe("tokenizeSetupHint", () => {
  it("plain text", () => {
    assert.deepEqual(tokenizeSetupHint("hello"), [{ kind: "text", text: "hello" }]);
  });
  it("link with trailing punctuation", () => {
    const segs = tokenizeSetupHint("See [CLI](https://example.com/cli).");
    assert.deepEqual(segs, [
      { kind: "text", text: "See " },
      { kind: "link", label: "CLI", href: "https://example.com/cli" },
      { kind: "text", text: "." },
    ]);
  });
  it("multiple links", () => {
    const segs = tokenizeSetupHint(
      "[A](https://a.example) and [B](https://b.example/x).",
    );
    assert.equal(segs.filter((s) => s.kind === "link").length, 2);
    assert.equal(segs.at(-1)?.kind, "text");
  });
  it("rejects javascript as literal text", () => {
    const segs = tokenizeSetupHint("[x](javascript:alert(1))");
    assert.deepEqual(segs, [{ kind: "text", text: "[x](javascript:alert(1))" }]);
  });
  it("CJK around links", () => {
    const segs = tokenizeSetupHint("设置 → [文档](https://example.com/docs)。");
    assert.equal(segs[0]?.kind, "text");
    assert.equal(segs[1]?.kind, "link");
    assert.equal(segs[2]?.kind, "text");
  });
  it("empty", () => {
    assert.deepEqual(tokenizeSetupHint(""), []);
  });
  it("leaves URLs with parentheses as literal text", () => {
    const raw = "[wiki](https://en.wikipedia.org/wiki/Foo_(bar))";
    assert.deepEqual(tokenizeSetupHint(raw), [{ kind: "text", text: raw }]);
  });
  it("inline code with backticks", () => {
    const segs = tokenizeSetupHint(
      "Grant `AliyunOpenAPIMCPServerStaticCredentialAccess` then paste.",
    );
    assert.deepEqual(segs, [
      { kind: "text", text: "Grant " },
      { kind: "code", text: "AliyunOpenAPIMCPServerStaticCredentialAccess" },
      { kind: "text", text: " then paste." },
    ]);
  });
  it("bold emphasis", () => {
    const segs = tokenizeSetupHint("Type **MCP**, then Save.");
    assert.deepEqual(segs, [
      { kind: "text", text: "Type " },
      { kind: "bold", text: "MCP" },
      { kind: "text", text: ", then Save." },
    ]);
  });
  it("code and link together", () => {
    const segs = tokenizeSetupHint(
      "Use `AK`. Docs: [api.aliyun.com/mcp](https://api.aliyun.com/mcp).",
    );
    assert.deepEqual(segs, [
      { kind: "text", text: "Use " },
      { kind: "code", text: "AK" },
      { kind: "text", text: ". Docs: " },
      { kind: "link", label: "api.aliyun.com/mcp", href: "https://api.aliyun.com/mcp" },
      { kind: "text", text: "." },
    ]);
  });
  it("code wins over surrounding bold markers", () => {
    const segs = tokenizeSetupHint("**use `foo`**");
    assert.deepEqual(segs, [
      { kind: "text", text: "**use " },
      { kind: "code", text: "foo" },
      { kind: "text", text: "**" },
    ]);
  });
});
