"""Minimal MCP stdio server for tests: newline-delimited JSON-RPC, answers ``tools/list``.

Behaviour switches (environment, so tests can drive them through the vault):
  FAKE_MCP_ECHO_ENV=NAME   -> adds a tool whose description is the value of $NAME
  FAKE_MCP_HANG=1          -> never answers tools/list
  FAKE_MCP_PAGES=1         -> splits the tool list over two pages (nextCursor)
  FAKE_MCP_V2=1            -> alpha tool has a different input schema
  FAKE_MCP_EXTRA=1         -> adds ``big`` (result of 1MiB+1 bytes) and ``slow`` (never answers)

``tools/call``: ``alpha`` echoes its arguments as JSON text; ``beta`` returns an ``isError`` result.
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
    if os.environ.get("FAKE_MCP_EXTRA"):
        out.append({"name": "big", "description": "huge result", "inputSchema": {"type": "object"}})
        out.append({"name": "slow", "description": "never answers", "inputSchema": {"type": "object"}})
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
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            if name == "alpha":
                text = json.dumps(params.get("arguments") or {}, sort_keys=True)
                result = {"content": [{"type": "text", "text": text}]}
            elif name == "beta":
                result = {"content": [{"type": "text", "text": "beta failed"}], "isError": True}
            elif name == "big":
                result = {"content": [{"type": "text", "text": "x" * (1024 * 1024 + 1)}]}
            elif name == "slow":
                time.sleep(3600)
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})


if __name__ == "__main__":
    main()
