#!/usr/bin/env node
/**
 * Portunus skill wrapper around the official Baidu Netdisk `bdpan` CLI.
 *
 * Auth verbs (skill_exec / Connect): login | login-status | logout
 * Tool verbs: quota, ls, … (see baidu-drive.example.yaml)
 *
 * Auth emits Portunus oauth_link JSON: { code, msg, action, type, data }.
 * Tools print vendor --json on stdout (one JSON value).
 *
 * The authorize code is accepted as `--code` for Connect paste, then sent to
 * `bdpan login --set-code-stdin` on stdin. It is never passed as a bdpan argv.
 */
"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const BIN = process.env.BDPAN_BIN || "/usr/local/bin/bdpan";
const PENDING = -114;
const CODE_RE = /^[a-fA-F0-9]{32}$/;
const APP_ROOT = "/apps/bdpan";
// The bridge kills this wrapper after 60s (_CALL_TIMEOUT_S). Stay under that so
// this process kills bdpan itself and returns a JSON error instead of an orphan.
const CALL_TIMEOUT_MS = 55_000;
// Only the authorize code this process was given is redacted from output.
let pastedCode = "";

/** Thrown to end the run with a JSON payload; main() emits it and sets the exit code. */
class Fail extends Error {
  constructor(exitCode, payload) {
    super(payload.message || payload.msg || "failed");
    this.exitCode = exitCode;
    this.payload = payload;
  }
}

function fail(exitCode, payload) {
  throw new Fail(exitCode, payload);
}

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function result(code, msg, action, data = {}) {
  return { code, msg, action, type: "result", data };
}

function redact(text) {
  let s = String(text || "");
  if (pastedCode) s = s.split(pastedCode).join("[code]");
  return s;
}

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--") && a.length > 2) {
      const key = a.slice(2).replace(/-/g, "_");
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) {
        out[key] = true;
      } else {
        out[key] = next;
        i++;
      }
    } else {
      out._.push(a);
    }
  }
  return out;
}

function homeDir() {
  // No /tmp fallback: the bdpan token must live under the skill's own state dir.
  const home = process.env.HOME || process.env.STATE_DIR;
  if (!home) throw new Error("HOME or STATE_DIR must be set for bdpan credential storage");
  return home;
}

function configPath() {
  const dir = path.join(homeDir(), ".config", "bdpan");
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  return path.join(dir, "config.json");
}

function runBdpan(args, { stdinText = null, timeoutMs = CALL_TIMEOUT_MS } = {}) {
  return new Promise((resolve) => {
    if (!fs.existsSync(BIN)) {
      resolve({ code: 127, stdout: "", stderr: `bdpan binary missing: ${BIN}` });
      return;
    }
    const proc = spawn(BIN, args, {
      env: { ...process.env, BDPAN_CONFIG_DIR: path.dirname(configPath()) },
      stdio: [stdinText === null ? "ignore" : "pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      try {
        proc.kill("SIGKILL");
      } catch (_) {
        /* ignore */
      }
    }, timeoutMs);
    proc.on("error", (err) => {
      clearTimeout(timer);
      resolve({ code: 127, stdout, stderr: `failed to start ${BIN}: ${err.message}` });
    });
    if (stdinText !== null && proc.stdin) {
      proc.stdin.on("error", () => {
        /* bdpan may exit before reading stdin; the exit code reports the failure */
      });
      proc.stdin.write(stdinText);
      proc.stdin.end();
    }
    proc.stdout.on("data", (c) => {
      stdout += c.toString("utf8");
    });
    proc.stderr.on("data", (c) => {
      stderr += c.toString("utf8");
    });
    proc.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? 1, stdout, stderr });
    });
  });
}

function globalArgs() {
  return ["--config-path", configPath(), "--no-check-update"];
}

function extractUrl(text) {
  const m = String(text || "").match(/https:\/\/[^\s"'<>）\]]+/);
  return m ? m[0] : null;
}

function lastJson(text) {
  const lines = String(text || "")
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    try {
      return JSON.parse(lines[i]);
    } catch (_) {
      /* keep scanning */
    }
  }
  try {
    return JSON.parse(String(text || "").trim());
  } catch (_) {
    return null;
  }
}

function loggedIn(stdout) {
  const text = String(stdout || "");
  if (text.includes("已登录")) return true;
  if (text.includes("未登录")) return false;
  const parsed = lastJson(text);
  if (!parsed || typeof parsed !== "object") return false;
  const username =
    (typeof parsed.username === "string" && parsed.username) ||
    (parsed.data && typeof parsed.data.username === "string" && parsed.data.username) ||
    "";
  return Boolean(username);
}

async function whoami() {
  return runBdpan([...globalArgs(), "whoami", "--json"]);
}

async function cmdLogin(flags) {
  const code = typeof flags.code === "string" ? flags.code.trim() : "";
  if (code) {
    if (!CODE_RE.test(code)) {
      fail(1, result(1, "授权码格式不正确（应为 32 位十六进制字符）", "login"));
    }
    pastedCode = code;
    const set = await runBdpan(
      [...globalArgs(), "login", "--set-code-stdin", "--accept-disclaimer"],
      { stdinText: `${code}\n` },
    );
    if (set.code !== 0) {
      fail(set.code || 1, result(1, redact(set.stderr || set.stdout || "授权失败"), "login"));
    }
    const st = await whoami();
    if (st.code === 0 && loggedIn(st.stdout)) {
      return emit(result(0, "已登录百度网盘", "login"));
    }
    fail(1, result(1, redact(st.stderr || st.stdout || "授权后仍未登录"), "login"));
  }

  const st = await whoami();
  if (st.code === 0 && loggedIn(st.stdout)) {
    return emit(result(0, "已登录百度网盘", "login"));
  }

  const got = await runBdpan([...globalArgs(), "login", "--get-auth-url", "--accept-disclaimer"]);
  const url = extractUrl(got.stdout);
  if (got.code !== 0 || !url) {
    fail(got.code || 1, result(1, redact(got.stderr || got.stdout || "获取授权链接失败"), "login"));
  }
  emit(result(PENDING, url, "login", { auth_url: url }));
}

async function cmdStatus() {
  const st = await whoami();
  if (st.code === 0 && loggedIn(st.stdout)) {
    return emit(result(0, "已登录", "login-status", { whoami: lastJson(st.stdout) }));
  }
  fail(st.code === 0 ? 1 : st.code || 1, result(1, redact(st.stderr || st.stdout || "未登录"), "login-status"));
}

async function cmdLogout() {
  const r = await runBdpan([...globalArgs(), "logout"]);
  if (r.code !== 0) {
    fail(r.code || 1, result(1, redact(r.stderr || r.stdout || "注销失败"), "logout"));
  }
  emit(result(0, "已注销", "logout"));
}

function safeValue(value, label) {
  // A value starting with "-" would be read by bdpan as one of its own flags.
  if (value.startsWith("-")) {
    fail(2, { code: 1, error: `${label} must not start with '-'`, message: `${label} must not start with '-'` });
  }
  return value;
}

function need(flags, key, label) {
  const v = flags[key];
  if (typeof v !== "string" || !v.trim()) {
    fail(2, { code: 1, error: `${label} is required`, message: `${label} is required` });
  }
  return safeValue(v.trim(), label);
}

function opt(flags, key) {
  const v = flags[key];
  if (typeof v !== "string" || !v.trim()) return null;
  return safeValue(v.trim(), key);
}

/** Cloud paths are relative to /apps/bdpan or absolute under it; reject escapes. */
function appPath(label, p) {
  const abs = p.startsWith("/") ? p : `${APP_ROOT}/${p}`;
  const norm = path.posix.normalize(abs);
  if (norm !== APP_ROOT && !norm.startsWith(`${APP_ROOT}/`)) {
    fail(2, { code: 1, error: `${label} must stay inside ${APP_ROOT}/`, message: `${label} must stay inside ${APP_ROOT}/` });
  }
  return p;
}

function pushOpt(argv, flag, value) {
  if (value !== null && value !== undefined && value !== "") argv.push(flag, String(value));
}

function toolArgv(verb, flags) {
  switch (verb) {
    case "quota":
      return ["quota"];
    case "ls": {
      const argv = ["ls"];
      const p = opt(flags, "path");
      if (p) argv.push(appPath("path", p));
      if (flags.folder === true) argv.push("--folder");
      return argv;
    }
    case "search": {
      const argv = ["search", need(flags, "keyword", "keyword")];
      pushOpt(argv, "--category", opt(flags, "category"));
      pushOpt(argv, "--page", opt(flags, "page"));
      pushOpt(argv, "--page-size", opt(flags, "page_size"));
      if (flags.no_dir === true) argv.push("--no-dir");
      if (flags.dir_only === true) argv.push("--dir-only");
      return argv;
    }
    case "search-content": {
      const argv = ["search-content", need(flags, "query", "query")];
      pushOpt(argv, "--dir", opt(flags, "dir") && appPath("dir", opt(flags, "dir")));
      pushOpt(argv, "--dir-fsid", opt(flags, "dir_fsid"));
      pushOpt(argv, "--file-types", opt(flags, "file_types"));
      pushOpt(argv, "--page-size", opt(flags, "page_size"));
      return argv;
    }
    case "upload": {
      // `local` is an absolute runner path by design; runners are isolated containers.
      const argv = [
        "upload",
        need(flags, "local", "local"),
        appPath("remote", need(flags, "remote", "remote")),
      ];
      if (flags.overwrite === true) argv.push("--overwrite");
      return argv;
    }
    case "download": {
      const remote = need(flags, "remote", "remote");
      // Share URLs are allowed; app-folder paths must stay inside /apps/bdpan.
      const argv = [
        "download",
        /^https:\/\//.test(remote) ? remote : appPath("remote", remote),
        need(flags, "local", "local"),
      ];
      pushOpt(argv, "-p", opt(flags, "pwd"));
      const transferDir = opt(flags, "transfer_dir");
      pushOpt(argv, "-t", transferDir && appPath("transfer_dir", transferDir));
      return argv;
    }
    case "transfer": {
      const argv = ["transfer", need(flags, "url", "url")];
      pushOpt(argv, "-p", opt(flags, "pwd"));
      pushOpt(argv, "-d", opt(flags, "dest"));
      return argv;
    }
    case "transfer-list": {
      const argv = ["transfer", "list", need(flags, "url", "url")];
      pushOpt(argv, "-p", opt(flags, "pwd"));
      pushOpt(argv, "--source-dir", opt(flags, "source_dir"));
      pushOpt(argv, "--page", opt(flags, "page"));
      pushOpt(argv, "--page-size", opt(flags, "page_size"));
      return argv;
    }
    case "transfer-select": {
      const argv = [
        "transfer",
        "select",
        need(flags, "url", "url"),
        "--fsid",
        need(flags, "fsid", "fsid"),
      ];
      pushOpt(argv, "-p", opt(flags, "pwd"));
      pushOpt(argv, "-d", opt(flags, "dest"));
      return argv;
    }
    case "share": {
      const argv = ["share", appPath("path", need(flags, "path", "path"))];
      pushOpt(argv, "--period", opt(flags, "period"));
      return argv;
    }
    case "mkdir":
      return ["mkdir", appPath("path", need(flags, "path", "path"))];
    case "mv":
      return ["mv", appPath("source", need(flags, "source", "source")), appPath("dest", need(flags, "dest", "dest"))];
    case "cp":
      return ["cp", appPath("source", need(flags, "source", "source")), appPath("dest", need(flags, "dest", "dest"))];
    case "rename":
      return ["rename", appPath("path", need(flags, "path", "path")), need(flags, "name", "name")];
    case "rm": {
      if (flags.confirm !== "yes") {
        fail(2, {
          code: 1,
          error: "rm requires confirm=yes after the user confirmed this exact path",
          message: "rm requires confirm=yes after the user confirmed this exact path",
        });
      }
      return ["rm", appPath("path", need(flags, "path", "path")), "--force"];
    }
    case "get-content": {
      const raw = need(flags, "fsid", "fsid");
      const ids = raw.split(/[,\s]+/).filter(Boolean);
      if (ids.length < 1 || ids.length > 5 || !ids.every((id) => /^\d+$/.test(id))) {
        fail(2, { code: 1, error: "fsid must be 1 to 5 numeric ids", message: "fsid must be 1 to 5 numeric ids" });
      }
      const argv = ["get-content", ...ids];
      pushOpt(argv, "--page-start", opt(flags, "page_start"));
      pushOpt(argv, "--page-end", opt(flags, "page_end"));
      return argv;
    }
    case "get-content-result":
      return ["get-content-result", need(flags, "task_id", "task_id")];
    case "whoami":
      return ["whoami"];
    case "vip":
      return ["vip"];
    default:
      return null;
  }
}

async function cmdTool(verb, flags) {
  const sub = toolArgv(verb, flags);
  if (!sub) {
    fail(2, { code: 2, error: `unknown verb ${verb}`, message: `unknown verb ${verb}` });
  }
  // bdpan documents --json after the subcommand (e.g. `bdpan ls --json`).
  const r = await runBdpan([...globalArgs(), ...sub, "--json"]);
  const parsed = lastJson(r.stdout);
  if (parsed !== null) {
    emit(parsed);
    if (r.code !== 0) process.exitCode = r.code || 1;
    return;
  }
  const message = redact((r.stderr || r.stdout || "bdpan failed").trim());
  fail(r.code || 1, { code: r.code || 1, error: message, message });
}

async function main() {
  const argv = process.argv.slice(2);
  const action = argv[0];
  if (!action) {
    fail(2, result(2, "usage: baidu-drive.cjs <action> [--flags]", "help"));
  }
  const flags = parseArgs(argv.slice(1));
  if (action === "login" || action === "relogin" || action === "refresh-login") {
    return cmdLogin(flags);
  }
  if (action === "login-status") return cmdStatus();
  if (action === "logout") return cmdLogout();
  return cmdTool(action, flags);
}

// No process.exit(): setting exitCode lets stdout flush before the process ends.
main().catch((err) => {
  if (err instanceof Fail) {
    emit(err.payload);
    process.exitCode = err.exitCode;
    return;
  }
  emit(result(1, redact(err && err.message ? err.message : err), "crash"));
  process.exitCode = 1;
});
