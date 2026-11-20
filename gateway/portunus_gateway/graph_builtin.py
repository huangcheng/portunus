"""First-party Microsoft Graph MCP adapter (personal Outlook Mail + OneDrive).

Catalog apps use ``connection: oauth`` against Entra (``consumers``) with
``url: portunus-graph://mail`` or ``portunus-graph://onedrive``. The gateway
handles probe/invoke in-process — no third-party MCP package.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from portunus_shared import PortunusError

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCHEME = "portunus-graph://"

# Tool catalogs — agent-facing only (no login/logout).
_MAIL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_mail_folders",
        "description": "List top-level mail folders in the signed-in mailbox.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_messages",
        "description": "List messages in a mail folder (default Inbox), newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Folder id or well-known name (inbox, sentitems, drafts).",
                    "default": "inbox",
                },
                "top": {
                    "type": "integer",
                    "description": "Max messages to return (1–50).",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "get_message",
        "description": "Get one message by id (body as text).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Graph message id"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "search_messages",
        "description": "Search messages across the mailbox (Graph $search).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (KQL-ish)"},
                "top": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "send_mail",
        "description": "Send an email from the signed-in mailbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipient email addresses",
                },
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text body"},
                "cc": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "reply_mail",
        "description": "Reply to a message (reply-all when reply_all is true).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "comment": {"type": "string", "description": "Reply body (plain text)"},
                "reply_all": {"type": "boolean", "default": False},
            },
            "required": ["message_id", "comment"],
        },
    },
]

_ONEDRIVE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_children",
        "description": "List files/folders under a drive item (default: root).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Drive item id; omit for root",
                },
            },
        },
    },
    {
        "name": "get_item",
        "description": "Get metadata for a drive item.",
        "inputSchema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    },
    {
        "name": "create_folder",
        "description": "Create a folder under a parent (default: root).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "parent_id": {"type": "string", "description": "Parent item id; omit for root"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "upload_text",
        "description": "Upload a small text file (≤4 MiB) to a path under root or a folder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to root, e.g. notes/hello.txt",
                },
                "content": {"type": "string"},
                "parent_id": {
                    "type": "string",
                    "description": "If set, path is relative to this folder id",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "download_text",
        "description": "Download a drive item's content as UTF-8 text (small files).",
        "inputSchema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    },
    {
        "name": "delete_item",
        "description": "Delete a drive item (moves to recycle bin when supported).",
        "inputSchema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    },
]


def parse_graph_builtin_url(url: str) -> str | None:
    """Return ``mail`` / ``onedrive`` profile, or None if not a builtin Graph URL."""
    raw = (url or "").strip()
    if not raw.lower().startswith(SCHEME):
        return None
    profile = raw[len(SCHEME) :].strip("/").casefold()
    if profile in ("mail", "outlook", "outlook-mail"):
        return "mail"
    if profile in ("onedrive", "files"):
        return "onedrive"
    raise PortunusError("unsupported", f"unknown portunus-graph profile: {profile!r}")


def is_graph_builtin_url(url: str) -> bool:
    return (url or "").strip().lower().startswith(SCHEME)


def _mcp_result(data: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    out: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "structuredContent": data if not isinstance(data, str) else {"text": data},
    }
    if is_error:
        out["isError"] = True
    return out


def _recipients(addrs: list[str]) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": a.strip()}} for a in addrs if isinstance(a, str) and a.strip()]


class GraphBuiltinClient:
    """Drop-in for ``RemoteClient`` against ``portunus-graph://…`` URLs."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.profile = parse_graph_builtin_url(url)
        if self.profile is None:
            raise PortunusError("unsupported", "not a portunus-graph URL")
        auth = ""
        for k, v in headers.items():
            if k.lower() == "authorization":
                auth = v
                break
        # Probe may list tools before Connect; Graph calls need a bearer.
        self._auth = auth.strip()
        self._timeout_s = float(timeout_s)
        self._transport = transport

    def _http(self, budget: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(budget),
            follow_redirects=False,
            trust_env=False,
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        # Static tool schemas — but when Authorization is present, hit Graph /me
        # so a revoked/expired bearer fails Temporal probe as auth_required
        # (not only later on invoke). Same contract as JmapBuiltinClient.
        if self._auth:
            budget = float(self._timeout_s)
            try:
                async with self._http(budget) as http:
                    await self._request(http, "GET", "/me")
            except PortunusError:
                raise
            except httpx.HTTPError as e:
                raise PortunusError("upstream_crash", f"graph /me failed: {e}") from e
        return list(_MAIL_TOOLS if self.profile == "mail" else _ONEDRIVE_TOOLS)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
        input_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_schema  # Graph builtins do not emit Mcp-Param mirrors
        if not self._auth:
            raise PortunusError("auth_required", "Microsoft Graph Authorization missing")
        budget = float(self._timeout_s if timeout_s is None else timeout_s)
        try:
            async with self._http(budget) as http:
                if self.profile == "mail":
                    return await self._mail(http, name, arguments or {})
                return await self._onedrive(http, name, arguments or {})
        except PortunusError:
            raise
        except httpx.HTTPError as e:
            raise PortunusError("upstream_crash", f"graph request failed: {e}") from e

    async def _request(
        self,
        http: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        raw_body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        url = f"{GRAPH_BASE}{path}"
        resp = await http.request(
            method,
            url,
            params=params,
            json=json_body,
            content=raw_body,
            headers=headers,
        )
        if resp.status_code in (401, 403):
            raise PortunusError("auth_required", f"graph {resp.status_code}")
        if resp.status_code in (202, 204):
            return {"ok": True}
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" in ct:
            try:
                data = resp.json()
            except ValueError:
                data = {"text": resp.text}
        else:
            data = resp.text
        if not resp.is_success:
            return _mcp_result(
                {"ok": False, "status_code": resp.status_code, "error": data},
                is_error=True,
            )
        return data

    @staticmethod
    def _wrap(data: Any) -> dict[str, Any]:
        if isinstance(data, dict) and data.get("isError") is True:
            return data
        return _mcp_result(data)

    async def _mail(
        self, http: httpx.AsyncClient, name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        if name == "list_mail_folders":
            return self._wrap(await self._request(http, "GET", "/me/mailFolders"))

        if name == "list_messages":
            folder = str(args.get("folder") or "inbox").strip() or "inbox"
            top = max(1, min(50, int(args.get("top") or 20)))
            path = f"/me/mailFolders/{quote(folder, safe='')}/messages"
            return self._wrap(
                await self._request(
                    http,
                    "GET",
                    path,
                    params={
                        "$top": top,
                        "$orderby": "receivedDateTime desc",
                        "$select": "id,subject,from,receivedDateTime,isRead,bodyPreview",
                    },
                )
            )

        if name == "get_message":
            mid = str(args.get("message_id") or "").strip()
            if not mid:
                raise PortunusError("invalid_tool_arguments", "message_id required")
            return self._wrap(
                await self._request(
                    http,
                    "GET",
                    f"/me/messages/{quote(mid, safe='')}",
                    params={
                        "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,bodyPreview"
                    },
                    extra_headers={"Prefer": 'outlook.body-content-type="text"'},
                )
            )

        if name == "search_messages":
            query = str(args.get("query") or "").strip()
            if not query:
                raise PortunusError("invalid_tool_arguments", "query required")
            top = max(1, min(50, int(args.get("top") or 20)))
            return self._wrap(
                await self._request(
                    http,
                    "GET",
                    "/me/messages",
                    params={
                        "$search": f'"{query}"',
                        "$top": top,
                        "$select": "id,subject,from,receivedDateTime,bodyPreview",
                    },
                    extra_headers={"ConsistencyLevel": "eventual"},
                )
            )

        if name == "send_mail":
            to = args.get("to") or []
            if not isinstance(to, list) or not to:
                raise PortunusError("invalid_tool_arguments", "to must be a non-empty list")
            cc = args.get("cc") if isinstance(args.get("cc"), list) else []
            subject = str(args.get("subject") or "")
            body = str(args.get("body") or "")
            payload = {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": _recipients([str(x) for x in to]),
                    "ccRecipients": _recipients([str(x) for x in cc]),
                },
                "saveToSentItems": True,
            }
            data = await self._request(http, "POST", "/me/sendMail", json_body=payload)
            if isinstance(data, dict) and data.get("isError") is True:
                return data
            return _mcp_result({"ok": True, "sent": True})

        if name == "reply_mail":
            mid = str(args.get("message_id") or "").strip()
            comment = str(args.get("comment") or "")
            if not mid:
                raise PortunusError("invalid_tool_arguments", "message_id required")
            reply_all = bool(args.get("reply_all"))
            action = "replyAll" if reply_all else "reply"
            data = await self._request(
                http,
                "POST",
                f"/me/messages/{quote(mid, safe='')}/{action}",
                json_body={"comment": comment},
            )
            if isinstance(data, dict) and data.get("isError") is True:
                return data
            return _mcp_result({"ok": True, "replied": True, "reply_all": reply_all})

        raise PortunusError("tool_missing", f"unknown mail tool {name!r}")

    async def _onedrive(
        self, http: httpx.AsyncClient, name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        if name == "list_children":
            item_id = str(args.get("item_id") or "").strip()
            path = (
                f"/me/drive/items/{quote(item_id, safe='')}/children"
                if item_id
                else "/me/drive/root/children"
            )
            return self._wrap(await self._request(http, "GET", path))

        if name == "get_item":
            item_id = str(args.get("item_id") or "").strip()
            if not item_id:
                raise PortunusError("invalid_tool_arguments", "item_id required")
            return self._wrap(
                await self._request(http, "GET", f"/me/drive/items/{quote(item_id, safe='')}")
            )

        if name == "create_folder":
            folder_name = str(args.get("name") or "").strip()
            if not folder_name:
                raise PortunusError("invalid_tool_arguments", "name required")
            parent_id = str(args.get("parent_id") or "").strip()
            path = (
                f"/me/drive/items/{quote(parent_id, safe='')}/children"
                if parent_id
                else "/me/drive/root/children"
            )
            return self._wrap(
                await self._request(
                    http,
                    "POST",
                    path,
                    json_body={
                        "name": folder_name,
                        "folder": {},
                        "@microsoft.graph.conflictBehavior": "rename",
                    },
                )
            )

        if name == "upload_text":
            rel = str(args.get("path") or "").strip().lstrip("/")
            content = str(args.get("content") or "")
            if not rel:
                raise PortunusError("invalid_tool_arguments", "path required")
            parent_id = str(args.get("parent_id") or "").strip()
            if parent_id:
                path = f"/me/drive/items/{quote(parent_id, safe='')}:/{quote(rel, safe='/')}:/content"
            else:
                path = f"/me/drive/root:/{quote(rel, safe='/')}:/content"
            return self._wrap(
                await self._request(
                    http,
                    "PUT",
                    path,
                    raw_body=content.encode("utf-8"),
                    extra_headers={"Content-Type": "text/plain; charset=utf-8"},
                )
            )

        if name == "download_text":
            item_id = str(args.get("item_id") or "").strip()
            if not item_id:
                raise PortunusError("invalid_tool_arguments", "item_id required")
            headers = {"Authorization": self._auth}
            url = f"{GRAPH_BASE}/me/drive/items/{quote(item_id, safe='')}/content"
            resp = await http.get(url, headers=headers, follow_redirects=True)
            if resp.status_code in (401, 403):
                raise PortunusError("auth_required", f"graph {resp.status_code}")
            if not resp.is_success:
                return _mcp_result(
                    {"ok": False, "status_code": resp.status_code, "error": resp.text},
                    is_error=True,
                )
            return _mcp_result({"ok": True, "text": resp.text})

        if name == "delete_item":
            item_id = str(args.get("item_id") or "").strip()
            if not item_id:
                raise PortunusError("invalid_tool_arguments", "item_id required")
            data = await self._request(
                http, "DELETE", f"/me/drive/items/{quote(item_id, safe='')}"
            )
            if isinstance(data, dict) and data.get("isError") is True:
                return data
            return _mcp_result({"ok": True, "deleted": True})

        raise PortunusError("tool_missing", f"unknown onedrive tool {name!r}")
