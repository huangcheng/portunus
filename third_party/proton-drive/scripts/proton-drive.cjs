#!/usr/bin/env node
/**
 * Portunus skill wrapper around the official Proton Drive CLI.
 *
 * Auth verbs (skill_exec / Connect): login | login-status | logout
 * Tool verbs: fs-list, fs-info, … (see proton-drive.example.yaml)
 *
 * Auth emits Portunus oauth_link JSON: { code, msg, action, type, data }.
 * Tools print vendor --json (or a small envelope) on stdout.
 */
"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const { URL } = require("url");

const AUTH = new Set(["login", "login-status", "logout", "relogin", "refresh-login"]);
const BIN =
  process.env.PROTON_DRIVE_BIN ||
  process.env.PROTON_DRIVE_CLI_PATH ||
  "/usr/local/bin/proton-drive";

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function result(code, msg, action, data = {}) {
  return { code, msg, action, type: "result", data };
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

function childEnv() {
  const env = { ...process.env };
  if (!env.PROTON_DRIVE_CREDENTIALS_STORE) {
    env.PROTON_DRIVE_CREDENTIALS_STORE = "unsafe_file";
  }
  return env;
}

function runProton(args, { timeoutMs = 120_000 } = {}) {
  return new Promise((resolve) => {
    if (!fs.existsSync(BIN)) {
      resolve({
        code: 127,
        stdout: "",
        stderr: `proton-drive binary missing: ${BIN}`,
      });
      return;
    }
    const proc = spawn(BIN, args, {
      env: childEnv(),
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
    proc.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? 1, stdout, stderr });
    });
  });
}

function extractUrl(text) {
  const m = String(text || "").match(/https:\/\/[^\s"'<>）\]]+/);
  return m ? m[0] : null;
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

async function cmdLogin(action) {
  if (!fs.existsSync(BIN)) {
    emit(result(1, `proton-drive binary missing: ${BIN}`, action));
    process.exit(1);
  }
  const proc = spawn(BIN, ["auth", "login"], {
    env: childEnv(),
    stdio: ["ignore", "pipe", "pipe"],
  });
  let buf = "";
  let opened = false;
  const onChunk = async (chunk) => {
    buf += chunk.toString("utf8");
    if (opened) return;
    const url = extractUrl(buf);
    if (!url) return;
    try {
      // eslint-disable-next-line no-new
      new URL(url);
    } catch (_) {
      return;
    }
    opened = true;
    process.stderr.write("portunus: proton login url captured\n");
    await openBrowser(url);
  };
  proc.stdout.on("data", (c) => {
    void onChunk(c);
  });
  proc.stderr.on("data", (c) => {
    void onChunk(c);
  });
  const code = await new Promise((resolve) => {
    proc.on("close", (c) => resolve(c ?? 1));
  });
  if (code === 0) {
    emit(result(0, "logged in", action));
    process.exit(0);
  }
  const url = extractUrl(buf);
  emit(
    result(-106, url ? `waiting for browser login: ${url}` : buf.trim() || "login failed", action, {
      auth_url: url,
    })
  );
  process.exit(code === 0 ? 0 : 1);
}

async function cmdStatus(action) {
  const r = await runProton(["filesystem", "list", "/my-files", "--json"], {
    timeoutMs: 60_000,
  });
  if (r.code === 0) {
    let data = {};
    try {
      data = { listing: JSON.parse(r.stdout) };
    } catch (_) {
      data = { raw: r.stdout };
    }
    emit(result(0, "ok", action, data));
    process.exit(0);
  }
  const msg = (r.stderr || r.stdout || "not authenticated").trim();
  emit(result(1, msg, action));
  process.exit(1);
}

async function cmdLogout(action) {
  const r = await runProton(["auth", "logout"], { timeoutMs: 60_000 });
  if (r.code === 0) {
    emit(result(0, "logged out", action));
    process.exit(0);
  }
  emit(result(r.code || 1, (r.stderr || r.stdout || "logout failed").trim(), action));
  process.exit(1);
}

function normalizePath(p) {
  if (p === undefined || p === null || p === "") return "/";
  let s = String(p).trim();
  if (!s.startsWith("/")) s = `/${s}`;
  while (s.length > 1 && s.endsWith("/")) s = s.slice(0, -1);
  return s;
}

/** Section roots that filesystem list/size/info reject or mis-handle. */
const PHOTOS_SECTIONS = new Set([
  "/photos",
  "/photos-shared-by-me",
  "/photos-shared-with-me",
  "/photos-trash",
]);
const ALBUMS_SECTIONS = new Set(["/albums"]);
const SIZE_UNSUPPORTED_SECTIONS = new Set([
  "/",
  "/devices",
  "/shared-by-me",
  "/shared-with-me",
  "/trash",
  ...PHOTOS_SECTIONS,
  ...ALBUMS_SECTIONS,
]);

function failTool(msg, code = 1) {
  emit({ error: msg, message: msg });
  process.exit(code);
}

/**
 * Vendor CLI treats /photos and /albums as photo/album namespaces, not folders.
 * Guard before spawn so agents get a redirect instead of "Photo not found:".
 */
function rejectFilesystemPath(verb, pathArg) {
  const p = normalizePath(pathArg);
  if (verb === "fs-list" && (p === "/photos" || p === "/albums")) {
    return p === "/photos"
      ? "Path /photos is not a filesystem folder. Use photo_timeline (library) or album_list / album_photos."
      : "Path /albums is not a filesystem folder. Use album_list, then album_photos with an album path.";
  }
  if (verb === "fs-info" || verb === "fs-size") {
    if (PHOTOS_SECTIONS.has(p) || ALBUMS_SECTIONS.has(p)) {
      return (
        `Path ${p} is a Photos/Albums section root, not a Drive folder. ` +
        "Use photo_timeline or album_list instead of fs_size/fs_info."
      );
    }
    if (SIZE_UNSUPPORTED_SECTIONS.has(p)) {
      return (
        `fs_size/fs_info is not supported on section root ${p}. ` +
        "Pass a concrete path under /my-files (list / first for section roots)."
      );
    }
  }
  return null;
}

async function toolFs(verb, args) {
  const pathArg = args.path;
  const parent = args.parent_path;
  const localPath = args.local_path;
  const localFolder = args.local_folder;
  const target = args.target_parent_path;
  const name = args.name;
  const newName = args.new_name;
  const typeFilter = args.type;
  const user = args.user;
  const role = args.role;
  const message = args.message;
  const fileConflict = args.file_conflict_strategy;
  const folderConflict = args.folder_conflict_strategy;
  const loadDetails = Boolean(args.load_details);

  const rejected = rejectFilesystemPath(verb, pathArg);
  if (rejected) failTool(rejected);

  /** @type {string[]} */
  let argv = [];
  switch (verb) {
    case "fs-list":
      argv = ["filesystem", "list"];
      if (typeFilter) argv.push("--type", String(typeFilter));
      argv.push(String(pathArg || "/"));
      break;
    case "fs-info":
      argv = ["filesystem", "info", String(pathArg)];
      break;
    case "fs-size":
      argv = ["filesystem", "size", String(pathArg)];
      break;
    case "fs-mkdir":
      argv = ["filesystem", "create-folder", String(parent), String(name)];
      break;
    case "fs-upload":
      argv = ["filesystem", "upload"];
      if (fileConflict) argv.push("--file-conflict-strategy", String(fileConflict));
      if (folderConflict) argv.push("--folder-conflict-strategy", String(folderConflict));
      argv.push(String(localPath), String(parent));
      break;
    case "fs-download":
      argv = ["filesystem", "download"];
      if (fileConflict) argv.push("--file-conflict-strategy", String(fileConflict));
      if (folderConflict) argv.push("--folder-conflict-strategy", String(folderConflict));
      argv.push(String(pathArg), String(localFolder));
      break;
    case "fs-rename":
      argv = ["filesystem", "rename", String(pathArg), String(newName)];
      break;
    case "fs-move":
      argv = ["filesystem", "move", String(pathArg), String(target)];
      break;
    case "fs-copy":
      argv = ["filesystem", "copy"];
      if (name) argv.push("--name", String(name));
      argv.push(String(pathArg), String(target));
      break;
    case "fs-trash":
      argv = ["filesystem", "trash", String(pathArg)];
      break;
    case "fs-restore":
      argv = ["filesystem", "restore", String(pathArg)];
      break;
    case "fs-delete":
      argv = ["filesystem", "delete", String(pathArg)];
      break;
    case "sharing-status":
      argv = ["sharing", "status", String(pathArg)];
      break;
    case "sharing-invite":
      argv = ["sharing", "invite", "--user", String(user)];
      if (role) argv.push("--role", String(role));
      if (message) argv.push("--message", String(message));
      argv.push(String(pathArg));
      break;
    case "invitation-list":
      argv = ["invitation", "list"];
      break;
    case "photo-timeline":
      argv = ["photo", "timeline"];
      if (loadDetails) argv.push("-d");
      break;
    case "album-list":
      argv = ["album", "list"];
      break;
    case "album-photos":
      argv = ["album", "photos"];
      if (loadDetails) argv.push("-d");
      argv.push(String(pathArg));
      break;
    default:
      failTool(`unknown verb ${verb}`, 2);
  }
  argv.push("--json");
  const r = await runProton(argv, { timeoutMs: 300_000 });
  const out = (r.stdout || "").trim();
  const err = (r.stderr || "").trim();
  if (out) {
    process.stdout.write(out.endsWith("\n") ? out : `${out}\n`);
    process.exit(r.code === 0 ? 0 : r.code || 1);
  }
  if (err) {
    // Surface stderr as message so gateway omni does not fall back to a bare exit_code.
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
    emit(result(2, "usage: proton-drive.cjs <action> [--flags]", "help"));
    process.exit(2);
  }
  const flags = parseArgs(argv.slice(1));
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
  await toolFs(action, flags);
}

main().catch((err) => {
  emit(result(1, String(err && err.message ? err.message : err), "crash"));
  process.exit(1);
});
