"""Build a one-account Himalaya TOML from Connect env (shared by shim + tests)."""

from __future__ import annotations

REQUIRED = ("IMAP_URL", "SMTP_URL", "MAIL_ADDRESS", "MAIL_PASSWORD")


def toml_string(value: str) -> str:
    """Encode a TOML basic string (double-quoted) safely."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def build_config(
    *,
    imap_url: str,
    smtp_url: str,
    address: str,
    password: str,
    account: str = "portunus",
) -> str:
    for label, raw in (
        ("IMAP_URL", imap_url),
        ("SMTP_URL", smtp_url),
        ("MAIL_ADDRESS", address),
        ("MAIL_PASSWORD", password),
    ):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"{label} is required")
    imap_url = imap_url.strip()
    smtp_url = smtp_url.strip()
    address = address.strip()
    # password: preserve leading/trailing spaces (app passwords rarely need them,
    # but stripping would silently break a deliberate paste).
    if not account or not account.replace("_", "").isalnum():
        raise ValueError("account name must be alphanumeric/underscore")
    return (
        f"[accounts.{account}]\n"
        f"email = {toml_string(address)}\n"
        f"default = true\n"
        f"imap.server = {toml_string(imap_url)}\n"
        f"imap.sasl.plain.username = {toml_string(address)}\n"
        f"imap.sasl.plain.password.raw = {toml_string(password)}\n"
        f"smtp.server = {toml_string(smtp_url)}\n"
        f"smtp.sasl.plain.username = {toml_string(address)}\n"
        f"smtp.sasl.plain.password.raw = {toml_string(password)}\n"
    )


def build_config_from_env(env: dict[str, str]) -> str:
    missing = [k for k in REQUIRED if not (env.get(k) or "").strip()]
    if missing:
        raise ValueError(f"missing required env: {', '.join(missing)}")
    return build_config(
        imap_url=env["IMAP_URL"],
        smtp_url=env["SMTP_URL"],
        address=env["MAIL_ADDRESS"],
        password=env["MAIL_PASSWORD"],
    )


# Subcommands that take a message id as a trailing positional.
_ID_POSITIONAL_PREFIXES = (
    ("message", "read"),
    ("msg", "read"),
    ("message", "reply"),
    ("msg", "reply"),
    ("message", "forward"),
    ("msg", "forward"),
    ("message", "delete"),
    ("msg", "delete"),
    ("message", "move"),
    ("msg", "move"),
    ("message", "copy"),
    ("msg", "copy"),
    ("attachment", "list"),
    ("attachment", "download"),
)


def rewrite_argv(argv: list[str]) -> list[str]:
    """Rewrite skill-bridge flags into Himalaya CLI grammar.

    * ``--id`` / ``--message-id`` → trailing positional for message/attachment cmds
    * ``--query`` → trailing positional for ``envelope search``
    * ``--source-mailbox`` → ``--from`` (``from`` is a Python keyword in manifests)
    """
    id_val: str | None = None
    query_parts: list[str] = []
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--id", "--message-id") and i + 1 < len(argv):
            id_val = argv[i + 1]
            i += 2
            continue
        if a.startswith("--id=") or a.startswith("--message-id="):
            id_val = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--query" and i + 1 < len(argv):
            query_parts.append(argv[i + 1])
            i += 2
            continue
        if a.startswith("--query="):
            query_parts.append(a.split("=", 1)[1])
            i += 1
            continue
        if a == "--source-mailbox" and i + 1 < len(argv):
            out += ["--from", argv[i + 1]]
            i += 2
            continue
        if a.startswith("--source-mailbox="):
            out += ["--from", a.split("=", 1)[1]]
            i += 1
            continue
        out.append(a)
        i += 1

    if id_val is not None:
        needs_positional = any(tuple(out[: len(p)]) == p for p in _ID_POSITIONAL_PREFIXES)
        if needs_positional:
            out.append(id_val)
        else:
            out += ["--id", id_val]

    if query_parts and tuple(out[:2]) == ("envelope", "search"):
        out.extend(query_parts)
    elif query_parts:
        for q in query_parts:
            out += ["--query", q]
    return out
