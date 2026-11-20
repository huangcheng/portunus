"""First-party JMAP MCP adapter (Thundermail / Stalwart).

Catalog apps use ``connection: http`` with ``url: portunus-jmap://thundermail``
and vault ``Authorization: Basic …`` (email + app password).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from portunus_shared import PortunusError

SCHEME = "portunus-jmap://"
THUNDERMAIL_SESSION = "https://mail.thundermail.com/jmap/session"
THUNDERMAIL_USING = [
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:mail",
    "urn:ietf:params:jmap:submission",
]

_MAIL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_mailboxes",
        "description": "List mailboxes (folders) with roles and unread counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_messages",
        "description": "List messages in a mailbox (default Inbox), newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mailbox_id": {
                    "type": "string",
                    "description": "Mailbox id; omit to use Inbox role",
                },
                "top": {"type": "integer", "default": 20, "description": "Max (1–50)"},
            },
        },
    },
    {
        "name": "get_message",
        "description": "Get one email by id (body as text when available).",
        "inputSchema": {
            "type": "object",
            "properties": {"email_id": {"type": "string"}},
            "required": ["email_id"],
        },
    },
    {
        "name": "search_messages",
        "description": "Search emails by text (subject/body/from).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "send_mail",
        "description": "Create and submit an email (plain text).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["to", "subject", "body"],
        },
    },
]


def parse_jmap_builtin_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw.lower().startswith(SCHEME):
        return None
    profile = raw[len(SCHEME) :].strip("/").casefold()
    if profile in ("thundermail", "tb", "tb.pro"):
        return "thundermail"
    raise PortunusError("unsupported", f"unknown portunus-jmap profile: {profile!r}")


def is_jmap_builtin_url(url: str) -> bool:
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


def _safe_top(raw: Any, default: int = 20) -> int:
    try:
        return max(1, min(50, int(raw if raw is not None else default)))
    except (TypeError, ValueError) as e:
        raise PortunusError("invalid_tool_arguments", "top must be an integer") from e


def _email_addrs(addrs: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for a in addrs:
        if isinstance(a, str) and a.strip():
            out.append({"email": a.strip()})
    return out


class JmapBuiltinClient:
    """Drop-in for ``RemoteClient`` against ``portunus-jmap://…`` URLs."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.profile = parse_jmap_builtin_url(url)
        if self.profile is None:
            raise PortunusError("unsupported", "not a portunus-jmap URL")
        auth = ""
        for k, v in headers.items():
            if k.lower() == "authorization":
                auth = v
                break
        self._auth = auth.strip()
        self._timeout_s = float(timeout_s)
        self._transport = transport
        self._session: dict[str, Any] | None = None
        self._account_id: str | None = None
        self._api_url: str | None = None

    def _http(self, budget: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(budget),
            follow_redirects=True,
            trust_env=False,
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        # Static tool schemas — but when Authorization is present, hit the JMAP
        # session endpoint so a revoked/wrong app password fails probe as
        # auth_required (not only later on invoke).
        if self._auth:
            budget = float(self._timeout_s)
            try:
                async with self._http(budget) as http:
                    await self._ensure_session(http)
            except PortunusError:
                raise
            except httpx.HTTPError as e:
                raise PortunusError("upstream_crash", f"jmap session failed: {e}") from e
        return list(_MAIL_TOOLS)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
        input_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_schema
        if not self._auth:
            raise PortunusError("auth_required", "JMAP Authorization missing")
        if not self._auth.lower().startswith("basic "):
            raise PortunusError(
                "auth_required",
                "Thundermail expects Authorization: Basic (email + app password)",
            )
        budget = float(self._timeout_s if timeout_s is None else timeout_s)
        try:
            async with self._http(budget) as http:
                await self._ensure_session(http)
                return await self._dispatch(http, name, arguments or {})
        except PortunusError:
            raise
        except httpx.HTTPError as e:
            raise PortunusError("upstream_crash", f"jmap request failed: {e}") from e

    async def _ensure_session(self, http: httpx.AsyncClient) -> None:
        if self._session and self._account_id and self._api_url:
            return
        resp = await http.get(
            THUNDERMAIL_SESSION,
            headers={"Authorization": self._auth, "Accept": "application/json"},
        )
        if resp.status_code in (401, 403):
            raise PortunusError("auth_required", f"jmap session {resp.status_code}")
        if not resp.is_success:
            raise PortunusError(
                "upstream_error",
                f"jmap session {resp.status_code}: {resp.text[:300]}",
            )
        session = resp.json()
        if not isinstance(session, dict):
            raise PortunusError("upstream_error", "jmap session not an object")
        accounts = session.get("accounts") or {}
        primary = session.get("primaryAccounts") or {}
        account_id = primary.get("urn:ietf:params:jmap:mail")
        if not account_id and isinstance(accounts, dict) and accounts:
            account_id = next(iter(accounts))
        if not account_id or not isinstance(account_id, str):
            raise PortunusError(
                "auth_required",
                "jmap session has no mail account (check email + app password)",
            )
        api_url = session.get("apiUrl")
        if not isinstance(api_url, str) or not api_url.strip():
            raise PortunusError("upstream_error", "jmap session missing apiUrl")
        self._session = session
        self._account_id = account_id
        self._api_url = api_url.strip()

    async def _jmap(
        self, http: httpx.AsyncClient, method_calls: list[list[Any]]
    ) -> list[Any]:
        assert self._api_url
        payload = {"using": THUNDERMAIL_USING, "methodCalls": method_calls}
        resp = await http.post(
            self._api_url,
            headers={
                "Authorization": self._auth,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
        )
        if resp.status_code in (401, 403):
            raise PortunusError("auth_required", f"jmap {resp.status_code}")
        if not resp.is_success:
            return [
                [
                    "error",
                    {"type": "http", "status": resp.status_code, "detail": resp.text[:500]},
                    "0",
                ]
            ]
        data = resp.json()
        if not isinstance(data, dict):
            raise PortunusError("upstream_error", "jmap response not an object")
        calls = data.get("methodResponses") or []
        if not isinstance(calls, list):
            raise PortunusError("upstream_error", "jmap methodResponses missing")
        return calls

    @staticmethod
    def _first_result(calls: list[Any]) -> Any:
        if not calls:
            raise PortunusError("upstream_error", "empty jmap response")
        first = calls[0]
        if not isinstance(first, list) or len(first) < 2:
            raise PortunusError("upstream_error", "malformed jmap methodResponse")
        name, body = first[0], first[1]
        if name == "error" or (isinstance(name, str) and name.endswith("/error")):
            return _mcp_result({"ok": False, "error": body}, is_error=True)
        return body

    async def _inbox_id(self, http: httpx.AsyncClient) -> str:
        assert self._account_id
        calls = await self._jmap(
            http,
            [
                [
                    "Mailbox/query",
                    {"accountId": self._account_id, "filter": {"role": "inbox"}},
                    "0",
                ]
            ],
        )
        body = self._first_result(calls)
        if isinstance(body, dict) and body.get("isError"):
            raise PortunusError("upstream_error", "Mailbox/query failed")
        ids = (body or {}).get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids:
            raise PortunusError("upstream_error", "no Inbox mailbox")
        return str(ids[0])

    async def _emails_by_ids(
        self, http: httpx.AsyncClient, ids: list[str], *, properties: list[str]
    ) -> Any:
        assert self._account_id
        if not ids:
            return {"list": [], "notFound": []}
        calls = await self._jmap(
            http,
            [
                [
                    "Email/get",
                    {
                        "accountId": self._account_id,
                        "ids": ids,
                        "properties": properties,
                        "fetchTextBodyValues": True,
                        "maxBodyValueBytes": 100_000,
                    },
                    "0",
                ]
            ],
        )
        return self._first_result(calls)

    async def _dispatch(
        self, http: httpx.AsyncClient, name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        assert self._account_id

        if name == "list_mailboxes":
            calls = await self._jmap(
                http,
                [
                    [
                        "Mailbox/get",
                        {
                            "accountId": self._account_id,
                            "ids": None,
                            "properties": [
                                "id",
                                "name",
                                "role",
                                "totalEmails",
                                "unreadEmails",
                                "parentId",
                            ],
                        },
                        "0",
                    ]
                ],
            )
            body = self._first_result(calls)
            if isinstance(body, dict) and body.get("isError"):
                return body
            return _mcp_result(body)

        if name == "list_messages":
            top = _safe_top(args.get("top"))
            mailbox_id = str(args.get("mailbox_id") or "").strip() or await self._inbox_id(http)
            calls = await self._jmap(
                http,
                [
                    [
                        "Email/query",
                        {
                            "accountId": self._account_id,
                            "filter": {"inMailbox": mailbox_id},
                            "sort": [{"property": "receivedAt", "isAscending": False}],
                            "limit": top,
                        },
                        "0",
                    ]
                ],
            )
            q = self._first_result(calls)
            if isinstance(q, dict) and q.get("isError"):
                return q
            ids = [str(i) for i in ((q or {}).get("ids") or [])]
            emails = await self._emails_by_ids(
                http,
                ids,
                properties=["id", "subject", "from", "receivedAt", "preview", "keywords"],
            )
            if isinstance(emails, dict) and emails.get("isError"):
                return emails
            return _mcp_result({"query": q, "emails": emails})

        if name == "get_message":
            eid = str(args.get("email_id") or "").strip()
            if not eid:
                raise PortunusError("invalid_tool_arguments", "email_id required")
            emails = await self._emails_by_ids(
                http,
                [eid],
                properties=[
                    "id",
                    "subject",
                    "from",
                    "to",
                    "cc",
                    "receivedAt",
                    "preview",
                    "bodyValues",
                    "textBody",
                    "htmlBody",
                ],
            )
            if isinstance(emails, dict) and emails.get("isError"):
                return emails
            return _mcp_result(emails)

        if name == "search_messages":
            query = str(args.get("query") or "").strip()
            if not query:
                raise PortunusError("invalid_tool_arguments", "query required")
            top = _safe_top(args.get("top"))
            calls = await self._jmap(
                http,
                [
                    [
                        "Email/query",
                        {
                            "accountId": self._account_id,
                            "filter": {"text": query},
                            "sort": [{"property": "receivedAt", "isAscending": False}],
                            "limit": top,
                        },
                        "0",
                    ]
                ],
            )
            q = self._first_result(calls)
            if isinstance(q, dict) and q.get("isError"):
                return q
            ids = [str(i) for i in ((q or {}).get("ids") or [])]
            emails = await self._emails_by_ids(
                http,
                ids,
                properties=["id", "subject", "from", "receivedAt", "preview"],
            )
            if isinstance(emails, dict) and emails.get("isError"):
                return emails
            return _mcp_result({"query": q, "emails": emails})

        if name == "send_mail":
            to = args.get("to") or []
            if not isinstance(to, list) or not to:
                raise PortunusError("invalid_tool_arguments", "to must be a non-empty list")
            cc = args.get("cc") if isinstance(args.get("cc"), list) else []
            subject = str(args.get("subject") or "")
            body = str(args.get("body") or "")
            # Identity from session username when present
            from_email = ""
            if isinstance(self._session, dict):
                from_email = str(self._session.get("username") or "").strip()
            email_obj: dict[str, Any] = {
                "subject": subject,
                "from": [{"email": from_email}] if from_email else None,
                "to": _email_addrs([str(x) for x in to]),
                "cc": _email_addrs([str(x) for x in cc]),
                "keywords": {"$draft": True},
                "mailboxIds": {},  # filled after Drafts lookup
                "bodyValues": {"b": {"value": body, "charset": "utf-8"}},
                "textBody": [{"partId": "b", "type": "text/plain"}],
            }
            # Find drafts mailbox
            drafts = await self._jmap(
                http,
                [
                    [
                        "Mailbox/query",
                        {"accountId": self._account_id, "filter": {"role": "drafts"}},
                        "d0",
                    ]
                ],
            )
            dbody = self._first_result(drafts)
            draft_ids = (dbody or {}).get("ids") if isinstance(dbody, dict) else None
            if not isinstance(draft_ids, list) or not draft_ids:
                raise PortunusError("upstream_error", "no Drafts mailbox")
            email_obj["mailboxIds"] = {str(draft_ids[0]): True}
            if email_obj["from"] is None:
                email_obj.pop("from")

            create_id = "draft-1"
            submission: dict[str, Any] = {"emailId": f"#{create_id}"}
            if from_email:
                submission["envelope"] = {
                    "mailFrom": {"email": from_email},
                    "rcptTo": [{"email": a["email"]} for a in email_obj["to"]]
                    + [{"email": a["email"]} for a in email_obj["cc"]],
                }
            calls = await self._jmap(
                http,
                [
                    [
                        "Email/set",
                        {"accountId": self._account_id, "create": {create_id: email_obj}},
                        "s0",
                    ],
                    [
                        "EmailSubmission/set",
                        {
                            "accountId": self._account_id,
                            "create": {"sub-1": submission},
                        },
                        "s1",
                    ],
                ],
            )
            # Prefer submission result
            for item in calls:
                if isinstance(item, list) and item and item[0] == "EmailSubmission/set":
                    return _mcp_result({"ok": True, "submission": item[1], "responses": calls})
            first = self._first_result(calls)
            if isinstance(first, dict) and first.get("isError"):
                return first
            return _mcp_result({"ok": True, "responses": calls})

        raise PortunusError("tool_missing", f"unknown mail tool {name!r}")
