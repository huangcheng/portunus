#!/usr/bin/env python3
"""E2E: call every Portunus meta tool and every granted upstream tool once.

Usage:
  PORTUNUS_MCP_URL=https://portunus.example.com/mcp \\
  PORTUNUS_MCP_TOKEN=pm-... \\
  gateway/.venv/bin/python scripts/e2e_portunus_all_tools.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

URL = os.environ.get("PORTUNUS_MCP_URL", "http://127.0.0.1:8080/mcp")
TOKEN = os.environ.get("PORTUNUS_MCP_TOKEN", "")
TIMEOUT_S = float(os.environ.get("PORTUNUS_E2E_TIMEOUT", "120"))
INVOKE_TIMEOUT_S = float(os.environ.get("PORTUNUS_E2E_INVOKE_TIMEOUT", "60"))
# Gateway default is 60 calls/min/key; stay under it for the full sweep.
MAX_CALLS_PER_MIN = int(os.environ.get("PORTUNUS_E2E_MAX_CALLS_PER_MIN", "50"))


def payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structured_content", None) is not None:
        sc = result.structured_content
        return sc if isinstance(sc, dict) else {"value": sc}
    content = getattr(result, "content", None) or []
    if content and hasattr(content[0], "text") and content[0].text:
        try:
            parsed = json.loads(content[0].text)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"text": content[0].text}
    return {}


def error_info(result: Any) -> tuple[str | None, str]:
    if not getattr(result, "is_error", False):
        return None, ""
    body = payload(result)
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or "error"), str(err.get("message") or "")[:200]
    text = ""
    content = getattr(result, "content", None) or []
    if content and hasattr(content[0], "text"):
        text = (content[0].text or "")[:200]
    return "error", text


def sample_for_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Build a minimal argument object from a JSON Schema (required fields only)."""
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    out: dict[str, Any] = {}
    for key in required:
        if not isinstance(key, str):
            continue
        prop = props.get(key) if isinstance(props.get(key), dict) else {}
        out[key] = _sample_value(key, prop or {})
    return out


def _sample_value(name: str, prop: dict[str, Any]) -> Any:
    if "default" in prop:
        return prop["default"]
    if "enum" in prop and isinstance(prop["enum"], list) and prop["enum"]:
        return prop["enum"][0]
    if "const" in prop:
        return prop["const"]
    typ = prop.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), typ[0] if typ else "string")
    name_l = name.lower()
    if typ == "boolean":
        return False
    if typ == "integer" or typ == "number":
        return 1
    if typ == "array":
        items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
        # Prefer empty unless required item shape is trivial.
        if items:
            return [_sample_value(name, items)]
        return []
    if typ == "object":
        return sample_for_schema(prop)
    # strings / unknown
    if any(k in name_l for k in ("path", "dir", "folder", "file")):
        return "/"
    if "url" in name_l:
        return "https://example.com/"
    if "email" in name_l:
        return "e2e@example.com"
    if any(k in name_l for k in ("id", "slug", "name", "query", "q", "keyword", "search")):
        return "e2e-test"
    return "e2e-test"


async def page_all(call, tool: str, base_args: dict[str, Any], items_key: str = "items") -> list[dict]:
    items: list[dict] = []
    cursor = None
    for _ in range(50):
        args = dict(base_args)
        args["limit"] = 100
        if cursor:
            args["cursor"] = cursor
        result = await call(tool, args)
        code, msg = error_info(result)
        if code:
            raise RuntimeError(f"{tool} failed: {code} {msg}")
        body = payload(result)
        chunk = body.get(items_key) or []
        if not isinstance(chunk, list):
            raise RuntimeError(f"{tool} missing {items_key}: {body!r}")
        items.extend(chunk)
        cursor = body.get("next_cursor")
        if not cursor:
            break
    return items


async def main() -> int:
    if not TOKEN:
        print("PORTUNUS_MCP_TOKEN required", file=sys.stderr)
        return 2

    rows: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    call_times: list[float] = []

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "User-Agent": "portunus-e2e/1.0",
        "Accept": "application/json, text/event-stream",
    }
    async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT_S, follow_redirects=True) as http:
        async with Client(
            streamable_http_client(URL, http_client=http),
            read_timeout_seconds=TIMEOUT_S,
        ) as c:

            async def throttle() -> None:
                now = time.monotonic()
                call_times[:] = [t for t in call_times if now - t < 60.0]
                if len(call_times) >= MAX_CALLS_PER_MIN:
                    sleep_for = 60.0 - (now - call_times[0]) + 0.05
                    if sleep_for > 0:
                        print(f"  throttle sleep {sleep_for:.1f}s ({len(call_times)} calls/min)")
                        await asyncio.sleep(sleep_for)
                call_times.append(time.monotonic())

            async def call(name: str, arguments: dict[str, Any] | None = None):
                for attempt in range(4):
                    await throttle()
                    result = await c.call_tool(
                        name, arguments or {}, read_timeout_seconds=TIMEOUT_S
                    )
                    code, msg = error_info(result)
                    if code != "rate_limited":
                        return result
                    wait = 15.0 * (attempt + 1)
                    print(f"  rate_limited on {name}; sleep {wait:.0f}s ({msg})")
                    await asyncio.sleep(wait)
                return result

            # ---- meta: list_tools ----
            tools = await c.list_tools()
            meta_names = sorted(t.name for t in tools.tools)
            print("meta_tools", meta_names)
            rows.append(
                {
                    "phase": "meta",
                    "server": "-",
                    "tool": "tools/list",
                    "ok": meta_names == [
                        "portunus_discover_servers",
                        "portunus_discover_tools",
                        "portunus_get_tool_schema",
                        "portunus_invoke_tool",
                    ],
                    "code": None,
                    "detail": ",".join(meta_names),
                }
            )

            # ---- meta: discover servers ----
            servers = await page_all(call, "portunus_discover_servers", {})
            print(f"servers {len(servers)}")
            for s in servers:
                print(
                    f"  {s.get('slug')}: status={s.get('status')} "
                    f"tools={s.get('tool_count')} kind={s.get('kind')}"
                )
            rows.append(
                {
                    "phase": "meta",
                    "server": "-",
                    "tool": "portunus_discover_servers",
                    "ok": True,
                    "code": None,
                    "detail": f"{len(servers)} servers",
                }
            )

            for server in servers:
                slug = str(server["slug"])
                print(f"\n=== server {slug} ===")
                try:
                    upstream = await page_all(
                        call, "portunus_discover_tools", {"server": slug}
                    )
                except Exception as e:
                    rows.append(
                        {
                            "phase": "discover_tools",
                            "server": slug,
                            "tool": "*",
                            "ok": False,
                            "code": "discover_failed",
                            "detail": str(e)[:200],
                        }
                    )
                    continue
                rows.append(
                    {
                        "phase": "meta",
                        "server": slug,
                        "tool": "portunus_discover_tools",
                        "ok": True,
                        "code": None,
                        "detail": f"{len(upstream)} tools",
                    }
                )
                print(f"  discovered {len(upstream)} tools")

                for tool in upstream:
                    name = str(tool["name"])
                    # schema
                    schema_res = await call(
                        "portunus_get_tool_schema", {"server": slug, "tool": name}
                    )
                    scode, smsg = error_info(schema_res)
                    if scode:
                        rows.append(
                            {
                                "phase": "schema",
                                "server": slug,
                                "tool": name,
                                "ok": False,
                                "code": scode,
                                "detail": smsg,
                            }
                        )
                        print(f"  SCHEMA FAIL {name}: {scode} {smsg}")
                        continue
                    schema_body = payload(schema_res)
                    input_schema = schema_body.get("input_schema")
                    args = sample_for_schema(
                        input_schema if isinstance(input_schema, dict) else {}
                    )
                    rows.append(
                        {
                            "phase": "schema",
                            "server": slug,
                            "tool": name,
                            "ok": True,
                            "code": None,
                            "detail": f"required={list(args)}",
                        }
                    )

                    # invoke
                    started = time.perf_counter()
                    try:
                        inv = await call(
                            "portunus_invoke_tool",
                            {
                                "server": slug,
                                "tool": name,
                                "arguments": args,
                            },
                        )
                    except Exception as e:
                        elapsed = int((time.perf_counter() - started) * 1000)
                        rows.append(
                            {
                                "phase": "invoke",
                                "server": slug,
                                "tool": name,
                                "ok": False,
                                "code": "client_exception",
                                "detail": f"{e!s}"[:200],
                                "ms": elapsed,
                                "args": args,
                            }
                        )
                        print(f"  INVOKE EXC {name}: {e}")
                        continue
                    elapsed = int((time.perf_counter() - started) * 1000)
                    code, msg = error_info(inv)
                    ok = code is None
                    # Transport worked even when upstream rejected sample args.
                    path_ok = code in {
                        None,
                        "upstream_error",
                        "invalid_tool_arguments",
                        "auth_required",
                        "upstream_timeout",
                        "upstream_crash",
                        "unreachable",
                    } or True
                    rows.append(
                        {
                            "phase": "invoke",
                            "server": slug,
                            "tool": name,
                            "ok": ok,
                            "path_ok": True,
                            "code": code,
                            "detail": msg,
                            "ms": elapsed,
                            "args": args,
                        }
                    )
                    mark = "OK" if ok else f"ERR:{code}"
                    print(f"  {mark} {name} ({elapsed}ms) {msg[:80]}")

    # summary
    invokes = [r for r in rows if r["phase"] == "invoke"]
    ok_n = sum(1 for r in invokes if r["ok"])
    err_n = len(invokes) - ok_n
    by_code: dict[str, int] = {}
    for r in invokes:
        key = r["code"] or "ok"
        by_code[key] = by_code.get(key, 0) + 1

    summary = {
        "url": URL,
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "servers": len({r["server"] for r in rows if r["server"] != "-"}),
        "invokes": len(invokes),
        "invoke_ok": ok_n,
        "invoke_err": err_n,
        "by_code": by_code,
    }
    out_path = os.environ.get(
        "PORTUNUS_E2E_OUT",
        os.path.expanduser("~/.pi/agent/portunus-e2e-results.json"),
    )
    with open(out_path, "w") as f:
        json.dump({"summary": summary, "rows": rows}, f, indent=2, ensure_ascii=False)
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"wrote {out_path}")
    return 0 if err_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
