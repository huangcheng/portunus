import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { extractAccessToken, httpUsesTokenFlow, looksLikeAddress } from "./tokenPaste.ts";

const REDIRECT =
  "https://openapi.baidu.com/oauth/2.0/login_success#expires_in=2592000&access_token=121.abc_DEF-123.456&session_secret=&session_key=&scope=basic+netdisk";

describe("extractAccessToken", () => {
  it("pulls access_token out of a pasted redirect address", () => {
    assert.equal(extractAccessToken(REDIRECT), "121.abc_DEF-123.456");
  });
  it("accepts the fragment or query alone", () => {
    assert.equal(
      extractAccessToken("#expires_in=1&access_token=tok123&scope=a"),
      "tok123",
    );
    assert.equal(extractAccessToken("?access_token=tok123"), "tok123");
  });
  it("takes a token at the end of the string", () => {
    assert.equal(extractAccessToken("x#access_token=tok123"), "tok123");
  });
  it("accepts a bare access_token=value paste", () => {
    assert.equal(extractAccessToken("access_token=tok123&x=1"), "tok123");
  });
  it("decodes percent-encoding", () => {
    assert.equal(extractAccessToken("#access_token=a%2Bb%3D"), "a+b=");
  });
  it("trims surrounding whitespace", () => {
    assert.equal(extractAccessToken(`  ${REDIRECT}\n`), "121.abc_DEF-123.456");
  });
  it("returns a plain token unchanged (trimmed)", () => {
    assert.equal(extractAccessToken("  121.plainToken  "), "121.plainToken");
    assert.equal(extractAccessToken("Bearer abc"), "Bearer abc");
    assert.equal(extractAccessToken(""), "");
  });
  it("does not mistake other parameters for access_token", () => {
    assert.equal(extractAccessToken("x#my_access_token=zzz"), "x#my_access_token=zzz");
  });
  it("leaves an empty access_token value untouched", () => {
    assert.equal(extractAccessToken("#access_token=&scope=a"), "#access_token=&scope=a");
  });
});

describe("looksLikeAddress", () => {
  it("flags urls, even with surrounding text", () => {
    assert.equal(looksLikeAddress("https://openapi.baidu.com/oauth/2.0/authorize?x=1"), true);
    assert.equal(looksLikeAddress("登录成功 https://a.example/login_success#expires_in=1"), true);
    assert.equal(looksLikeAddress("//evil.example/x"), true);
    assert.equal(looksLikeAddress("data://x"), true);
  });
  it("does not flag bare tokens", () => {
    assert.equal(looksLikeAddress("121.abc_DEF-123.456"), false);
    assert.equal(looksLikeAddress("key:secret"), false);
    assert.equal(looksLikeAddress("urn:abc"), false);
    assert.equal(looksLikeAddress(""), false);
  });
});

describe("httpUsesTokenFlow", () => {
  it("is false for credential-URL and Basic recipes", () => {
    assert.equal(httpUsesTokenFlow({ url_is_credential: true }), false);
    assert.equal(httpUsesTokenFlow({ auth_scheme: "basic" }), false);
    assert.equal(httpUsesTokenFlow({ auth_scheme: " Basic " }), false);
  });
  it("is true otherwise", () => {
    assert.equal(httpUsesTokenFlow({}), true);
    assert.equal(httpUsesTokenFlow({ auth_header: "Authorization" }), true);
  });
});
