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
  const rest = args.filter((a) => a !== "-y" && a !== "--yes" && a !== "--ignore-scripts");
  return ["-y", "--ignore-scripts", ...rest];
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
    const extra = args.slice(i + 1).find((a) => a.startsWith("-"));
    if (extra) return `unexpected npx flag: ${extra}`;
    return null;
  }
  if (base === "uvx") {
    let i = 0;
    while (i + 1 < args.length && args[i] === "--with") {
      if (!UVX_PKG.test(args[i + 1])) {
        return "uvx --with requires name==MAJOR.MINOR.PATCH";
      }
      i += 2;
    }
    if (!args[i] || !UVX_PKG.test(args[i])) {
      return "uvx requires exactly one package pinned as name==MAJOR.MINOR.PATCH";
    }
    const extra = args.slice(i + 1).find((a) => a.startsWith("-"));
    if (extra) return `unexpected uvx flag: ${extra}`;
    return null;
  }
  return null;
}

/** Parse args text, normalize, and validate for create/edit paths. */
export function prepareStdioArgv(
  command: string,
  argsText: string,
): { argv: string[]; error: string | null } {
  const cmd = command.trim();
  const rawArgs = argsText.split(/\s+/).filter(Boolean);
  const argv = normalizeStdioArgs(cmd, rawArgs);
  return { argv, error: validateStdioArgs(cmd, argv) };
}

export function stdioArgsHint(command: string): string | null {
  const base = commandBasename(command);
  if (base === "npx") {
    return "npx must be: -y --ignore-scripts name@1.2.3 (exact semver; scripts are blocked)";
  }
  if (base === "uvx") {
    return "uvx must be: [--with dep==1.2.3]… name==1.2.3 (exact semver)";
  }
  return null;
}
