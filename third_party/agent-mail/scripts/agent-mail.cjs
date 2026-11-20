#!/usr/bin/env node
/**
 * Portunus skill wrapper around @tencent-qqmail/agently-cli.
 *
 * Auth verbs (skill_exec / Connect): login | login-status | logout
 * Tool verbs: pass-through to agently-cli (see agent-mail.example.yaml).
 *
 * Auth emits Portunus oauth_link JSON: { code, msg, action, type, data }.
 * Tools print vendor JSON (or a small envelope) on stdout.
 *
 * Credentials: rely on HOME=STATE_DIR (skill_exec + json_cli already pin this).
 * Do NOT set AGENTLY_CLI_CONFIG_DIR from STATE_DIR — that diverges from the
 * CLI's default-under-HOME layout and breaks tools after Connect.
 */
"use strict";

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const { URL } = require("url");

const AUTH = new Set(["login", "login-status", "logout", "relogin", "refresh-login"]);
const PIN = "1.0.18";
const PACKAGE = `@tencent-qqmail/agently-cli@${PIN}`;
const TRUSTED_HOST_RE = /(^|\.)agent\.qq\.com$/i;

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function result(code, msg, action, data = {}) {
  return { code, msg, action, type: "result", data };
}

function childEnv() {
  const env = { ...process.env };
  // Suppress CLI update-notifier noise that can inject unrelated https URLs.
  env.AGENTLY_CLI_NO_UPDATE_NOTIFIER = env.AGENTLY_CLI_NO_UPDATE_NOTIFIER || "1";
  return env;
}

function toolCwd() {
  const state = (process.env.STATE_DIR || process.env.HOME || "").trim();
  if (state) {
    try {
      fs.mkdirSync(state, { recursive: true });
      const scratch = `${state}/scratch`;
      fs.mkdirSync(scratch, { recursive: true });
      return scratch;
    } catch (_) {
      /* fall through */
    }
  }
  return process.cwd();
}

/** Resolve [cmd, ...prefixArgs] for spawning agently-cli. */
function resolveCli() {
  if (process.env.AGENT_MAIL_BIN) {
    const bin = process.env.AGENT_MAIL_BIN;
    if (!fs.existsSync(bin)) {
      return {
        error: `AGENT_MAIL_BIN not found: ${bin} (install @tencent-qqmail/agently-cli@${PIN})`,
      };
    }
    return { cmd: bin, prefix: [] };
  }
  const which = spawnSync("sh", ["-c", "command -v agently-cli"], {
    encoding: "utf8",
  });
  const found = (which.stdout || "").trim();
  if (found && fs.existsSync(found)) {
    return { cmd: found, prefix: [] };
  }
  const npxWhich = spawnSync("sh", ["-c", "command -v npx"], { encoding: "utf8" });
  const npx = (npxWhich.stdout || "").trim();
  if (!npx) {
    return {
      error:
        `agently-cli not on PATH and npx missing; set AGENT_MAIL_BIN or install ` +
        `@tencent-qqmail/agently-cli@${PIN}`,
    };
  }
  return { cmd: npx, prefix: ["-y", "--loglevel=error", PACKAGE] };
}

function rewriteFlags(argv) {
  // MCP forbids input name `from`; manifest uses `sender` → --sender.
  return argv.map((a) => (a === "--sender" ? "--from" : a));
}

function extractAuthUrl(text) {
  const re = /https:\/\/[^\s"'<>）\]，,]+/g;
  let m;
  while ((m = re.exec(String(text || ""))) !== null) {
    let raw = m[0].replace(/[.,;:]+$/, "");
    try {
      const u = new URL(raw);
      if (TRUSTED_HOST_RE.test(u.hostname)) return raw;
    } catch (_) {
      /* continue */
    }
  }
  return null;
}

async function openBrowser(url) {
  const browser = process.env.BROWSER;
  if (!browser) return;
  await new Promise((resolve) => {
    const p = spawn(browser, [url], {
      env: process.env,
      stdio: "ignore",
      detached: true,
    });
    p.on("error", () => resolve());
    p.unref();
    setTimeout(resolve, 200);
  });
}

function runCli(args, { timeoutMs = 120_000, cwd } = {}) {
  const resolved = resolveCli();
  if (resolved.error) {
    return Promise.resolve({ code: 127, stdout: "", stderr: resolved.error });
  }
  const { cmd, prefix } = resolved;
  return new Promise((resolve) => {
    const proc = spawn(cmd, [...prefix, ...args], {
      env: childEnv(),
      cwd: cwd || undefined,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (c) => {
      stdout += c.toString("utf8");
    });
    proc.stderr.on("data", (c) => {
      stderr += c.toString("utf8");
    });
    const timer = setTimeout(() => {
      try {
        proc.kill("SIGKILL");
      } catch (_) {
        /* ignore */
      }
    }, timeoutMs);
    proc.on("error", (err) => {
      clearTimeout(timer);
      resolve({
        code: 127,
        stdout: "",
        stderr: err && err.message ? err.message : String(err),
      });
    });
    proc.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? 1, stdout, stderr });
    });
  });
}

function parseJsonLoose(text) {
  const t = String(text || "").trim();
  if (!t) return null;
  try {
    return JSON.parse(t);
  } catch (_) {
    const start = t.indexOf("{");
    const end = t.lastIndexOf("}");
    if (start >= 0 && end > start) {
      try {
        return JSON.parse(t.slice(start, end + 1));
      } catch (__) {
        return null;
      }
    }
    return null;
  }
}

async function cmdLogin(action) {
  const resolved = resolveCli();
  if (resolved.error) {
    emit(result(1, resolved.error, action));
    process.exit(1);
  }
  const { cmd, prefix } = resolved;
  const proc = spawn(cmd, [...prefix, "auth", "login"], {
    env: childEnv(),
    stdio: ["ignore", "pipe", "pipe"],
  });
  let buf = "";
  let opened = false;
  const onChunk = async (chunk) => {
    buf += chunk.toString("utf8");
    if (opened) return;
    const url = extractAuthUrl(buf);
    if (!url) return;
    opened = true;
    process.stderr.write("portunus: agent-mail login url captured\n");
    // Runner browser_capture shims BROWSER/xdg-open and fabricates pending (-106).
    await openBrowser(url);
  };
  proc.stdout.on("data", (c) => {
    void onChunk(c);
  });
  proc.stderr.on("data", (c) => {
    void onChunk(c);
  });
  const code = await new Promise((resolve) => {
    proc.on("error", (err) => {
      buf += err && err.message ? err.message : String(err);
      resolve(127);
    });
    proc.on("close", (c) => resolve(c ?? 1));
  });
  if (code === 0) {
    emit(result(0, "logged in", action));
    process.exit(0);
  }
  const url = extractAuthUrl(buf);
  const msg = (buf.trim() || "login failed").slice(0, 2000);
  // Process ended without success — hard failure (not pending).
  emit(
    result(1, url ? `login failed (had auth_url): ${msg}` : msg, action, {
      auth_url: url || undefined,
    })
  );
  process.exit(1);
}

async function cmdStatus(action) {
  const status = await runCli(["auth", "status"], { timeoutMs: 60_000 });
  const parsed = parseJsonLoose(status.stdout) || parseJsonLoose(status.stderr);
  const loggedIn =
    parsed &&
    parsed.data &&
    (parsed.data.logged_in === true || parsed.data.status === "logged_in");
  if (!loggedIn) {
    const msg =
      (parsed && parsed.data && parsed.data.message) ||
      (status.stderr || status.stdout || "not authenticated").trim();
    emit(result(1, msg, action, parsed && parsed.data ? parsed.data : {}));
    process.exit(1);
  }
  const me = await runCli(["+me"], { timeoutMs: 60_000 });
  const meJson = parseJsonLoose(me.stdout);
  if (me.code !== 0 || !meJson || meJson.ok !== true) {
    const msg =
      (meJson && meJson.error && meJson.error.message) ||
      (me.stderr || me.stdout || "token present but +me failed").trim();
    emit(result(1, msg, action, { auth: parsed.data, me: meJson || me.stdout }));
    process.exit(1);
  }
  emit(result(0, "ok", action, { ...(parsed.data || {}), me: meJson }));
  process.exit(0);
}

async function cmdLogout(action) {
  const r = await runCli(["auth", "logout"], { timeoutMs: 60_000 });
  if (r.code === 0) {
    emit(result(0, "logged out", action));
    process.exit(0);
  }
  emit(result(r.code || 1, (r.stderr || r.stdout || "logout failed").trim(), action));
  process.exit(1);
}

async function toolPassthrough(argv) {
  const rewritten = rewriteFlags(argv);
  const cwd = toolCwd();
  const r = await runCli(rewritten, { timeoutMs: 300_000, cwd });
  const out = (r.stdout || "").trim();
  const err = (r.stderr || "").trim();
  if (out) {
    process.stdout.write(out.endsWith("\n") ? out : `${out}\n`);
    process.exit(r.code === 0 ? 0 : r.code || 1);
  }
  if (err) {
    emit({ error: err, message: err, exit_hint: r.code });
    process.exit(r.code === 0 ? 1 : r.code || 1);
  }
  emit({ ok: r.code === 0 });
  process.exit(r.code === 0 ? 0 : r.code || 1);
}

async function main() {
  const argv = process.argv.slice(2);
  const action = argv[0];
  if (!action) {
    emit(result(2, "usage: agent-mail.cjs <action> [--flags]", "help"));
    process.exit(2);
  }
  if (AUTH.has(action)) {
    if (action === "login" || action === "relogin" || action === "refresh-login") {
      await cmdLogin(action);
      return;
    }
    if (action === "login-status") {
      await cmdStatus(action);
      return;
    }
    if (action === "logout") {
      await cmdLogout(action);
      return;
    }
  }
  await toolPassthrough(argv);
}

main().catch((err) => {
  emit(result(1, String(err && err.message ? err.message : err), "crash"));
  process.exit(1);
});
