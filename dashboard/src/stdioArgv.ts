/** Client-side helpers mirroring portunus_shared.argv npx/uvx closed grammar. */

const NPX_PKG = /^(?:@[a-z0-9][a-z0-9._-]*\/)?[a-z0-9][a-z0-9._-]*@[0-9]+\.[0-9]+\.[0-9]+$/;
const UVX_PKG = /^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?==[0-9]+\.[0-9]+\.[0-9]+$/;

export const NPX_ARGS_DEFAULT = "-y --ignore-scripts ";

function commandBasename(command: string): string {
  const base = command.trim().split(/[/\\]/).pop() || command.trim();
  return base.replace(/\.(cmd|bat|exe)$/i, "").toLowerCase();
}

/** Ensure npx argv includes -y/--yes and --ignore-scripts in the required order. */
export function normalizeStdioArgs(command: string, args: string[]): string[] {
  if (commandBasename(command) !== "npx") return args;
  const out = [...args];
  let i = 0;
  if (out[0] === "-y" || out[0] === "--yes") i = 1;
  if (!out.includes("--ignore-scripts")) {
    out.splice(i, 0, "--ignore-scripts");
  }
  if (out[0] !== "-y" && out[0] !== "--yes") {
    out.unshift("-y");
  }
  // Keep --ignore-scripts immediately after the yes flag.
  const ignoreAt = out.indexOf("--ignore-scripts");
  const yesAt = out[0] === "-y" || out[0] === "--yes" ? 0 : -1;
  if (ignoreAt > 0 && yesAt === 0 && ignoreAt !== 1) {
    out.splice(ignoreAt, 1);
    out.splice(1, 0, "--ignore-scripts");
  }
  return out;
}

/** Validate npx/uvx argv before create; returns an error message or null. */
export function validateStdioArgs(command: string, args: string[]): string | null {
  const base = commandBasename(command);
  if (base === "npx") {
    let i = 0;
    if (args[i] === "-y" || args[i] === "--yes") i += 1;
    if (args[i] !== "--ignore-scripts") {
      return "npx requires --ignore-scripts (e.g. -y --ignore-scripts pkg@1.2.3)";
    }
    i += 1;
    if (!args[i] || !NPX_PKG.test(args[i])) {
      return "npx requires exactly one package pinned as name@MAJOR.MINOR.PATCH";
    }
    return null;
  }
  if (base === "uvx") {
    if (!args[0] || !UVX_PKG.test(args[0])) {
      return "uvx requires exactly one package pinned as name==MAJOR.MINOR.PATCH";
    }
    return null;
  }
  return null;
}

export function stdioArgsHint(command: string): string | null {
  const base = commandBasename(command);
  if (base === "npx") {
    return "npx must be: -y --ignore-scripts name@1.2.3 (exact semver; scripts are blocked)";
  }
  if (base === "uvx") {
    return "uvx must be: name==1.2.3 (exact semver)";
  }
  return null;
}
