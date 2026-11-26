import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  httpUsesTokenFlow,
  isBasicAuth,
  looksLikeAddress,
  parseAccessToken,
} from "./tokenPaste.ts";

const REDIRECT =
  "https://openapi.baidu.com/oauth/2.0/login_success#expires_in=2592000&access_token=121.abc_DEF-123.456&session_secret=&session_key=&scope=basic+netdisk";

describe("parseAccessToken", () => {
  it("pulls access_token out of a pasted redirect address", () => {
    assert.equal(parseAccessToken(REDIRECT), "121.abc_DEF-123.456");
  });
  it("accepts the fragment or query alone", () => {
    assert.equal(parseAccessToken("#expires_in=1&access_token=tok123&scope=a"), "tok123");
    assert.equal(parseAccessToken("?access_token=tok123"), "tok123");
  });
  it("takes a token at the end of the string", () => {
    assert.equal(parseAccessToken("x#access_token=tok123"), "tok123");
  });
  it("accepts a bare access_token=value paste", () => {
    assert.equal(parseAccessToken("access_token=tok123&x=1"), "tok123");
  });
  it("decodes percent-encoding", () => {
    assert.equal(parseAccessToken("#access_token=a%2Bb%3D"), "a+b=");
  });
  it("trims surrounding whitespace", () => {
    assert.equal(parseAccessToken(`  ${REDIRECT}\n`), "121.abc_DEF-123.456");
  });
  it("returns null when there is no token, including for a bare token", () => {
    assert.equal(parseAccessToken("121.plainToken"), null);
    assert.equal(parseAccessToken("Bearer abc"), null);
    assert.equal(parseAccessToken(""), null);
    assert.equal(parseAccessToken("https://x.example/authorize?a=1"), null);
  });
  it("does not mistake other parameters for access_token", () => {
    assert.equal(parseAccessToken("x#my_access_token=zzz"), null);
  });
  it("returns null for an empty access_token value", () => {
    assert.equal(parseAccessToken("#access_token=&scope=a"), null);
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

describe("isBasicAuth", () => {
  it("normalizes case and whitespace", () => {
    assert.equal(isBasicAuth({ auth_scheme: " BASIC " }), true);
    assert.equal(isBasicAuth({ auth_scheme: "bearer" }), false);
    assert.equal(isBasicAuth({}), false);
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
