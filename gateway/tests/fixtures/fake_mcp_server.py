"""Minimal MCP stdio server for tests: newline-delimited JSON-RPC, answers ``tools/list``.

Behaviour switches (environment, so tests can drive them through the vault):
  FAKE_MCP_ECHO_ENV=NAME   -> adds a tool whose description is the value of $NAME
  FAKE_MCP_HANG=1          -> never answers tools/list
  FAKE_MCP_PAGES=1         -> splits the tool list over two pages (nextCursor)
  FAKE_MCP_V2=1            -> alpha tool has a different input schema
"""

import json
import os
import sys
import time

ALPHA = {
    "name": "alpha",
    "description": "first tool",
    "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
}
ALPHA_V2 = {
    "name": "alpha",
    "description": "first tool",
    "inputSchema": {"type": "object", "properties": {"q": {"type": "integer"}}},
}
BETA = {"name": "beta", "description": "second tool", "inputSchema": {"type": "object"}}


def tools() -> list[dict]:
    out = [ALPHA_V2 if os.environ.get("FAKE_MCP_V2") else ALPHA, BETA]
    echo = os.environ.get("FAKE_MCP_ECHO_ENV")
    if echo:
        out.append(
            {
                "name": "echo_env",
                "description": os.environ.get(echo, "<unset>"),
                "inputSchema": {"type": "object"},
            }
        )
    return out


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "protocolVersion": msg["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1.0.0"},
                    },
                }
            )
        elif method == "tools/list":
            if os.environ.get("FAKE_MCP_HANG"):
                time.sleep(3600)
            all_tools = tools()
            if os.environ.get("FAKE_MCP_PAGES"):
                cursor = (msg.get("params") or {}).get("cursor")
                if cursor is None:
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": mid,
                            "result": {"tools": all_tools[:1], "nextCursor": "p2"},
                        }
                    )
                else:
                    send({"jsonrpc": "2.0", "id": mid, "result": {"tools": all_tools[1:]}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": all_tools}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})


if __name__ == "__main__":
    main()
