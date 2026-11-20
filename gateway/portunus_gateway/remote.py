"""Remote MCP proxy (Streamable HTTP client) with SSRF controls (spec §6.3).

Policy
------
* **Default-deny non-public addresses.** Every address a hostname resolves to must be globally
  routable; loopback, RFC1918, link-local (incl. ``169.254.169.254``), CGNAT, multicast,
  reserved and IPv6 transition forms embedding a non-public IPv4 (v4-mapped, NAT64, 6to4,
  Teredo, v4-compatible) are refused. A mixed DNS answer is refused if *any* address is bad.
* **Loopback allowlist exception.** ``RemotePolicy.loopback_allowlist`` (hostnames / IPs, or
  ``host:port``) permits loopback targets, and only loopback: an allowlisted name that resolves
  to RFC1918 or link-local is still denied. Plain ``http`` is accepted only for such targets.
* **https only off-loopback**; TLS verification stays on; ``https -> http`` redirects are refused.
* **DNS pin per hop.** Each request (and each redirect hop) resolves, validates, then connects to
  the validated IP literal, keeping ``Host`` and SNI/cert-verification on the original name, so a
  second lookup can never rebind the connection to a different address.
* **Redirects are followed by hand** (307/308 only, bounded): each hop is fully re-validated.
  On a cross-origin hop all caller-supplied headers (vault credentials, cookies, session id) are
  dropped for that and every later hop. A redirect from a non-loopback origin to loopback is
  refused even if the target is allowlisted.
* Vault credentials (headers) are decrypted just-in-time and never appear in error messages.
* Modern (2026-07-28) client duties: ``mcp-method`` / ``mcp-name`` / ``Mcp-Param-*`` routing
  headers (non-ASCII values use the ``=?base64?...?=`` sentinel), ``x-mcp-header`` annotations
  validated before a tool is stored or mirrored, and 4xx discover bodies inspected for the
  modern error codes before any legacy ``initialize`` fallback — ``-32022`` follows the
  server's advertised version list instead of guessing the peer's era.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.shared.inbound import (
    encode_header_value,
    find_invalid_x_mcp_header,
    mcp_param_headers,
    x_mcp_header_map,
)
from mcp_types import (
    HEADER_MISMATCH,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    UNSUPPORTED_PROTOCOL_VERSION,
)
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
from portunus_shared import DEFAULTS, MAX_FRAME_BODY, PortunusError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Server, VaultEntry
from .oauth import AUTH_HEADER_NAME, canonicalize_authorization
from .registry import RegistryError
from .vault import Vault, VaultError

log = logging.getLogger("portunus.remote")

LOOPBACK_ALLOWLIST_ENV = "PORTUNUS_REMOTE_LOOPBACK_ALLOWLIST"
# Prefer the current MCP revision; fall back to the newest initialize-handshake revision.
MODERN_PROTOCOL_VERSION = "2026-07-28"
HANDSHAKE_PROTOCOL_VERSION = "2025-11-25"
PROTOCOL_VERSION = HANDSHAKE_PROTOCOL_VERSION  # legacy alias for tests / docs
CLIENT_INFO = {"name": "portunus-gateway", "version": "0.1.0"}
MAX_TOOL_PAGES = 50
_REDIRECT_STATUSES = frozenset({307, 308})  # method + body preserving
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", re.ASCII)
_FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "trailer"}
)
# Headers that survive a cross-origin redirect: protocol plumbing, never credentials.
# ``Mcp-Param-*`` mirrors argument values the (preserved) body already carries, so keeping
# them costs no privacy and avoids a body/header mismatch a conforming server must reject.
_CROSS_ORIGIN_KEEP = frozenset(
    {"accept", "content-type", "mcp-protocol-version", "mcp-method", "mcp-name"}
)


def _cross_origin_safe(name: str) -> bool:
    low = name.lower()
    return low in _CROSS_ORIGIN_KEEP or low.startswith("mcp-param-")
_METHODS_WITH_NAME = frozenset({"tools/call", "resources/read", "prompts/get"})
# Recognized modern JSON-RPC errors (spec 2026-07-28): a 4xx body carrying one of these
# identifies a modern server, so the client must not fall back to ``initialize``.
_MODERN_ERROR_CODES = frozenset({HEADER_MISMATCH, MISSING_REQUIRED_CLIENT_CAPABILITY})

Resolver = Callable[[str, int], Awaitable[list[str]]]


def _rpc_error_of(data: bytes) -> dict[str, Any] | None:
    """The JSON-RPC ``error`` object when the body is a JSON-RPC error response, else ``None``."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    err = raw.get("error")
    return err if isinstance(err, dict) else None


def _format_rpc_error(err: Mapping[str, Any]) -> str:
    """Human-readable upstream JSON-RPC error (code + message; unwrap nested QQ Mail JSON)."""
    code = err.get("code")
    msg = err.get("message")
    detail = ""
    if isinstance(msg, str) and msg.strip():
        detail = msg.strip()
        # QQ Mail (and similar) nest ``{"content":[{"text":"{...40101...}"}]}`` as a string.
        try:
            outer = json.loads(detail)
        except (ValueError, TypeError):
            outer = None
        if isinstance(outer, dict):
            content = outer.get("content")
            if isinstance(content, list) and content:
                first = content[0]
                text = first.get("text") if isinstance(first, dict) else None
                if isinstance(text, str) and text.strip():
                    try:
                        inner = json.loads(text)
                    except (ValueError, TypeError):
                        inner = None
                    if isinstance(inner, dict):
                        nested = inner.get("error")
                        if isinstance(nested, dict):
                            ncode = nested.get("code")
                            nmsg = nested.get("message")
                            if isinstance(nmsg, str) and nmsg.strip():
                                detail = (
                                    f"{nmsg.strip()} (vendor {ncode})"
                                    if ncode is not None
                                    else nmsg.strip()
                                )
                            elif ncode is not None:
                                detail = f"vendor error {ncode}"
                    if detail == msg.strip() and text.strip():
                        detail = text.strip()[:300]
    if detail:
        return f"upstream JSON-RPC error {code}: {detail[:400]}"
    return f"upstream JSON-RPC error {code}"


def _portunus_error_from_rpc(err: Mapping[str, Any]) -> PortunusError:
    """Map a JSON-RPC error object to PortunusError (auth when the peer says token bad)."""
    text = _format_rpc_error(err).casefold()
    if any(
        needle in text
        for needle in (
            "invalid or expired access token",
            "invalid_token",
            "expired access token",
            "unauthorized",
            "auth required",
            "authentication required",
            "vendor 40101",
        )
    ):
        return PortunusError("auth_required", _format_rpc_error(err))
    return PortunusError("upstream_crash", _format_rpc_error(err))


def _drop_invalid_x_mcp_header_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude tools whose ``inputSchema`` carries an invalid ``x-mcp-header`` annotation.

    On Streamable HTTP such a tool definition is invalid as a whole (spec 2026-07-28): the
    parameters could not be mirrored into ``Mcp-Param-*`` headers, so a conforming server
    would reject every call. The drop is logged per the spec's SHOULD.
    """
    out: list[dict[str, Any]] = []
    for tool in tools:
        schema = tool.get("inputSchema") if isinstance(tool, dict) else None
        reason = find_invalid_x_mcp_header(schema)
        if reason is None:
            out.append(tool)
        else:
            name = tool.get("name") if isinstance(tool.get("name"), str) else "<unnamed>"
            log.warning("tools/list: dropping tool %r (invalid x-mcp-header: %s)", name, reason)
    return out


class SsrfError(PortunusError):
    """Request refused by the SSRF policy (``unsupported`` on the wire; ``reason`` is local)."""

    def __init__(self, reason: str) -> None:
        super().__init__("unsupported", f"remote target refused: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class RemotePolicy:
    loopback_allowlist: frozenset[str] = frozenset()
    max_redirects: int = 5
    max_response_bytes: int = MAX_FRAME_BODY
    timeout_s: float = float(DEFAULTS.invoke_timeout_s)

    @classmethod
    def from_env(cls, **kw: Any) -> RemotePolicy:
        raw = os.environ.get(LOOPBACK_ALLOWLIST_ENV, "")
        allow = frozenset(p.strip().lower() for p in raw.split(",") if p.strip())
        return cls(loopback_allowlist=allow, **kw)


# ---- address policy ----------------------------------------------------------------------


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    packed = ip.packed
    if ip in ipaddress.ip_network("64:ff9b::/96"):  # NAT64
        return ipaddress.IPv4Address(packed[12:])
    if ip in ipaddress.ip_network("2002::/16"):  # 6to4
        return ipaddress.IPv4Address(packed[2:6])
    return None


def check_address(addr: str, *, allow_loopback: bool) -> None:
    """Raise ``SsrfError`` unless ``addr`` is a public address (or allowed loopback)."""
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(addr)
    except ValueError:
        raise SsrfError("unparseable address") from None
    if isinstance(ip, ipaddress.IPv6Address):
        # Deprecated/tunnel forms: ::/8 (unspecified, loopback, v4-compatible), Teredo,
        # NAT64 local-use. Anything embedding a v4 is judged by that v4.
        embedded = _embedded_v4(ip)
        if embedded is not None:
            ip = embedded
        elif (
            (ip in ipaddress.ip_network("::/8") and not ip.is_loopback)
            or ip in ipaddress.ip_network("2001::/32")
            or ip in ipaddress.ip_network("64:ff9b:1::/48")
        ):
            raise SsrfError("non-public address")
    if ip.is_loopback:
        if allow_loopback:
            return
        raise SsrfError("loopback address is not allowlisted")
    if ip.is_multicast or ip.is_unspecified or not ip.is_global:
        raise SsrfError("non-public address")


async def _default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise PortunusError("upstream_crash", "remote host did not resolve") from None
    out: list[str] = []
    for info in infos:
        addr = info[4][0].split("%", 1)[0]  # drop IPv6 zone id
        if addr not in out:
            out.append(addr)
    if not out:
        raise PortunusError("upstream_crash", "remote host did not resolve")
    return out


@dataclass(frozen=True)
class _Target:
    url: httpx.URL
    scheme: str
    host: str  # lowercase, no brackets
    port: int
    origin: tuple[str, str, int]
    addresses: list[str]
    loopback: bool

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == default else f"{host}:{self.port}"


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


# ---- client ------------------------------------------------------------------------------


class RemoteClient:
    """One remote MCP server: ``list_tools`` (probe) and ``call_tool`` (invoke).

    Negotiates MCP ``2026-07-28`` via ``server/discover`` when the peer supports it; otherwise
    falls back to the initialize handshake (short-lived session + best-effort DELETE).
    ``headers`` are credentials / custom headers.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str],
        policy: RemotePolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.url = url
        self.policy = policy or RemotePolicy()
        self._transport = transport
        self._resolve = resolver or _default_resolver
        self._headers: dict[str, str] = {}
        for name, value in headers.items():
            low = name.lower()
            if not _HEADER_NAME.fullmatch(name) or low in _FORBIDDEN_HEADERS:
                raise PortunusError("unsupported", f"header name not allowed: {name!r}")
            if "\r" in value or "\n" in value or "\x00" in value:
                raise PortunusError("unsupported", "invalid header value")
            self._headers[low] = value

    # ---- public ----

    async def list_tools(self) -> list[dict[str, Any]]:
        async def op(s: _Session) -> list[dict[str, Any]]:
            tools: list[dict[str, Any]] = []
            cursor: str | None = None
            for _ in range(MAX_TOOL_PAGES):
                params = {"cursor": cursor} if cursor else {}
                result = await s.rpc("tools/list", params)
                page = result.get("tools")
                if not isinstance(page, list):
                    raise PortunusError("upstream_crash", "malformed tools/list result")
                tools.extend(page)
                cursor = result.get("nextCursor")
                if not cursor:
                    return _drop_invalid_x_mcp_header_tools(tools)
            raise PortunusError("upstream_crash", "tools/list pagination too long")

        return await self._run(op)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
        input_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async def op(s: _Session) -> dict[str, Any]:
            return await s.rpc(
                "tools/call",
                {"name": name, "arguments": arguments},
                input_schema=input_schema,
            )

        return await self._run(op, timeout_s=timeout_s)

    # ---- session ----

    async def _run(
        self, op: Callable[[_Session], Awaitable[Any]], *, timeout_s: float | None = None
    ) -> Any:
        self._parse_start()  # fail fast on malformed URLs before opening anything
        if _is_classic_sse_url(self.url):
            return await self._run_classic_sse(op, timeout_s=timeout_s)
        budget = float(self.policy.timeout_s if timeout_s is None else timeout_s)
        try:
            async with asyncio.timeout(budget):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    follow_redirects=False,
                    trust_env=False,  # env proxies would bypass DNS pinning
                    timeout=httpx.Timeout(budget),
                ) as http:
                    session = _Session(self, http)
                    await session.connect()
                    try:
                        return await op(session)
                    finally:
                        await session.close()
        except TimeoutError:
            raise PortunusError("upstream_timeout", "remote server timed out") from None

    async def _run_classic_sse(
        self, op: Callable[[_Session], Awaitable[Any]], *, timeout_s: float | None = None
    ) -> Any:
        """Legacy MCP SSE (GET /sse + POST /message) — e.g. Youdao Note.

        Streamable HTTP POSTs to the SSE URL 404; responses arrive on the GET
        event stream while JSON-RPC requests POST to the ``endpoint`` event URL.
        Two httpx clients share the transport so the open GET does not block POSTs.
        """
        budget = float(self.policy.timeout_s if timeout_s is None else timeout_s)
        limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
        timeout = httpx.Timeout(budget, read=max(budget, 60.0))
        try:
            async with asyncio.timeout(budget):
                async with (
                    httpx.AsyncClient(
                        transport=self._transport,
                        follow_redirects=False,
                        trust_env=False,
                        timeout=timeout,
                        limits=limits,
                    ) as sse_http,
                    httpx.AsyncClient(
                        transport=self._transport,
                        follow_redirects=False,
                        trust_env=False,
                        timeout=timeout,
                        limits=limits,
                    ) as post_http,
                ):
                    session = _ClassicSseSession(
                        self, sse_http, post_http, timeout_s=budget
                    )
                    try:
                        await session.connect()
                        return await op(session)  # type: ignore[arg-type]
                    finally:
                        await session.close()
        except TimeoutError:
            raise PortunusError("upstream_timeout", "remote server timed out") from None

    def _parse_start(self) -> httpx.URL:
        try:
            url = httpx.URL(self.url)
        except (httpx.InvalidURL, ValueError):
            raise SsrfError("invalid url") from None
        return url

    # ---- one hop-by-hop request ----

    async def _target(self, url: httpx.URL, *, prev: _Target | None):
        scheme = url.scheme.lower()
        if scheme not in ("http", "https"):
            raise SsrfError("scheme must be https (or allowlisted loopback http)")
        if url.userinfo:
            raise SsrfError("credentials in url are not allowed")
        host = url.host.lower().strip("[]") if url.host else ""
        if not host:
            raise SsrfError("missing host")
        port = url.port or (443 if scheme == "https" else 80)
        if prev is not None and prev.scheme == "https" and scheme == "http":
            raise SsrfError("https to http downgrade")

        allow = self.policy.loopback_allowlist
        allowlisted = host in allow or f"{host}:{port}" in allow
        if _is_ip_literal(host):
            addresses = [str(ipaddress.ip_address(host))]
        else:
            addresses = await self._resolve(host, port)
        if not addresses:
            raise PortunusError("upstream_crash", "remote host did not resolve")
        # A redirect may not steer a non-loopback origin at an allowlisted loopback service.
        # Loopback only for the first hop or when the immediately previous hop
        # was already loopback — never re-enter loopback after a public redirect.
        may_use_loopback = allowlisted and (prev is None or prev.loopback)
        for addr in addresses:
            check_address(addr, allow_loopback=may_use_loopback)
        loopback = all(ipaddress.ip_address(a).is_loopback or _mapped_loopback(a) for a in addresses)
        if scheme == "http" and not (loopback and may_use_loopback):
            raise SsrfError("plain http is only allowed for allowlisted loopback")
        return _Target(url, scheme, host, port, (scheme, host, port), addresses, loopback)

    async def send(
        self,
        http: httpx.AsyncClient,
        method: str,
        *,
        body: bytes | None,
        headers: dict[str, str],
        url: httpx.URL | None = None,
    ) -> tuple[httpx.Response, bytes]:
        """Send with manual, fully re-validated redirects. Returns the final response + body."""
        url = url if url is not None else self._parse_start()
        hdrs = dict(headers)
        target = await self._target(url, prev=None)
        hops = 0
        while True:
            response, data = await self._send_pinned(http, method, target, hdrs, body)
            if response.status_code not in _REDIRECT_STATUSES:
                if 300 <= response.status_code < 400:
                    raise PortunusError(
                        "upstream_crash", f"unsupported redirect status {response.status_code}"
                    )
                return response, data
            if hops >= self.policy.max_redirects:
                raise PortunusError("upstream_crash", "too many redirects")
            hops += 1
            location = response.headers.get("location")
            if not location:
                raise PortunusError("upstream_crash", "redirect without location")
            try:
                next_url = target.url.join(location)
            except (httpx.InvalidURL, ValueError):
                raise SsrfError("invalid redirect location") from None
            nxt = await self._target(next_url, prev=target)
            if nxt.origin != target.origin:
                # Never forward caller credentials across origins, on this or any later hop.
                hdrs = {k: v for k, v in hdrs.items() if _cross_origin_safe(k)}
            target = nxt

    async def _send_pinned(
        self,
        http: httpx.AsyncClient,
        method: str,
        target: _Target,
        headers: dict[str, str],
        body: bytes | None,
        *,
        stream: bool = False,
    ) -> tuple[httpx.Response, bytes]:
        last: Exception | None = None
        for addr in target.addresses:
            ip_host = f"[{addr}]" if ":" in addr else addr
            pinned = target.url.copy_with(host=addr if ":" not in addr else ip_host)
            req_headers = {**headers, "host": target.host_header}
            request = http.build_request(
                method,
                pinned,
                headers=req_headers,
                content=body,
                extensions={"sni_hostname": target.host} if target.scheme == "https" else {},
            )
            try:
                response = await http.send(request, stream=True)
            except httpx.TimeoutException:
                raise PortunusError("upstream_timeout", "remote server timed out") from None
            except httpx.ConnectError as e:
                last = e
                continue
            except httpx.HTTPError as e:
                raise PortunusError("upstream_crash", f"transport error: {type(e).__name__}") from None
            if stream:
                return response, b""
            try:
                data = await self._read_capped(response)
            finally:
                await response.aclose()
            return response, data
        raise PortunusError(
            "upstream_crash", f"cannot connect: {type(last).__name__ if last else 'no address'}"
        )

    async def open_stream(
        self,
        http: httpx.AsyncClient,
        method: str,
        *,
        headers: dict[str, str],
        url: httpx.URL | None = None,
    ) -> httpx.Response:
        """DNS-pinned streaming request (classic SSE GET). Caller must ``aclose``."""
        url = url if url is not None else self._parse_start()
        target = await self._target(url, prev=None)
        response, _ = await self._send_pinned(http, method, target, headers, None, stream=True)
        return response

    async def _read_capped(self, response: httpx.Response) -> bytes:
        cap = self.policy.max_response_bytes
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            raise PortunusError("payload_too_large", "remote response too large")
        buf = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                buf += chunk
                if len(buf) > cap:
                    raise PortunusError("payload_too_large", "remote response too large")
        except httpx.TimeoutException:
            raise PortunusError("upstream_timeout", "remote server timed out") from None
        except httpx.HTTPError as e:
            raise PortunusError("upstream_crash", f"transport error: {type(e).__name__}") from None
        return bytes(buf)


def _mapped_loopback(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None and (
        ip.ipv4_mapped.is_loopback
    )


def _is_classic_sse_url(url: str) -> bool:
    """True when the path is the legacy MCP SSE listen URL (…/sse or …/sse/<token>).

    Youdao-style peers end at ``/sse``. Credential-in-path peers (e.g. Xunlei
    ``…/models/sse/<token>``) keep a segment after ``sse`` — still classic SSE,
    not Streamable HTTP.
    """
    try:
        path = httpx.URL(url).path.rstrip("/").lower()
    except (httpx.InvalidURL, ValueError):
        return False
    parts = [p for p in path.split("/") if p]
    if not parts:
        return False
    if parts[-1] == "sse":
        return True
    # One credential segment after /sse/ (do not match /sse/a/b/...).
    return len(parts) >= 2 and parts[-2] == "sse"


class _ClassicSseSession:
    """Legacy SSE transport: GET event stream + POST to ``endpoint`` URL.

    Compatible with ``_Session.rpc`` so ``list_tools`` / ``call_tool`` stay shared.
    """

    def __init__(
        self,
        client: RemoteClient,
        sse_http: httpx.AsyncClient,
        post_http: httpx.AsyncClient,
        *,
        timeout_s: float,
    ) -> None:
        self.c = client
        self.sse_http = sse_http
        self.post_http = post_http
        # Per-call budget (policy timeout when the caller did not pass one). The
        # RPC wait must honor it; capping at min(60, policy) ignores a longer budget.
        self._rpc_timeout_s = timeout_s
        self.protocol = HANDSHAKE_PROTOCOL_VERSION
        self.modern = False
        self._next_id = 0
        self._endpoint: httpx.URL | None = None
        self._sse_response: httpx.Response | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._endpoint_fut: asyncio.Future[httpx.URL] = asyncio.get_running_loop().create_future()

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            **self.c._headers,
        }

    async def connect(self) -> None:
        sse_headers = {
            "accept": "text/event-stream",
            **{k: v for k, v in self.c._headers.items() if k.lower() != "accept"},
        }
        response = await self.c.open_stream(self.sse_http, "GET", headers=sse_headers)
        code = response.status_code
        if code in (401, 403):
            await response.aclose()
            raise PortunusError("auth_required", "remote server requires authorization")
        if code != 200:
            body = b""
            with contextlib.suppress(Exception):
                body = await self.c._read_capped(response)
            await response.aclose()
            log.warning(
                "remote SSE GET failed status=%s body=%r",
                code,
                body[:512].decode("utf-8", "replace"),
            )
            raise PortunusError("upstream_crash", f"remote SSE returned HTTP {code}")
        self._sse_response = response
        self._reader = asyncio.create_task(self._read_loop(response), name="portunus-sse-reader")
        try:
            self._endpoint = await asyncio.wait_for(self._endpoint_fut, timeout=30.0)
        except TimeoutError:
            await self.close()
            raise PortunusError("upstream_timeout", "SSE endpoint event not received") from None
        # Legacy initialize (Youdao speaks 2024-11-05; skip modern discover).
        self.protocol = "2024-11-05"
        result = await self.rpc(
            "initialize",
            {
                "protocolVersion": self.protocol,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            },
        )
        proto = result.get("protocolVersion")
        if isinstance(proto, str) and proto:
            self.protocol = proto
        await self._post_notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _fail_waiters(self, err: PortunusError) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()
        if not self._endpoint_fut.done():
            self._endpoint_fut.set_exception(err)

    async def _read_loop(self, response: httpx.Response) -> None:
        event = "message"
        data_lines: list[str] = []
        event_bytes = 0
        max_bytes = self.c.policy.max_response_bytes
        # Byte-oriented read so a huge line cannot balloon memory before the cap.
        buf = b""
        total_bytes = 0
        try:
            async for chunk in response.aiter_bytes():
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise PortunusError("payload_too_large", "SSE stream too large")
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        if len(buf) > max_bytes:
                            raise PortunusError("payload_too_large", "SSE line too large")
                        break
                    raw_line, buf = buf[:nl], buf[nl + 1 :]
                    if raw_line.endswith(b"\r"):
                        raw_line = raw_line[:-1]
                    line = raw_line.decode("utf-8", "replace")
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip() or "message"
                        continue
                    if line.startswith("data:"):
                        piece = line[5:].lstrip(" ")
                        piece_len = len(piece.encode("utf-8", "replace"))
                        # Empty `data:` lines still append a list entry. Count the
                        # raw line so they cannot grow data_lines without bound.
                        event_bytes += piece_len if piece_len else len(raw_line) + 1
                        if event_bytes > max_bytes:
                            raise PortunusError("payload_too_large", "SSE event too large")
                        data_lines.append(piece)
                        continue
                    if line != "":
                        continue
                    data = "\n".join(data_lines)
                    data_lines = []
                    event_bytes = 0
                    ev, event = event, "message"
                    if not data:
                        continue
                    if ev == "endpoint":
                        await self._on_endpoint(data.strip())
                        continue
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    mid = obj.get("id")
                    if isinstance(mid, int) and mid in self._pending:
                        fut = self._pending.pop(mid)
                        if not fut.done():
                            fut.set_result(obj)
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            self._fail_waiters(PortunusError("upstream_timeout", "SSE read timed out"))
        except PortunusError as e:
            self._fail_waiters(e)
        except Exception as e:  # noqa: BLE001 — surface to waiters
            self._fail_waiters(PortunusError("upstream_crash", f"SSE read failed: {type(e).__name__}"))
        else:
            # Clean EOF with in-flight RPCs: fail fast instead of waiting out rpc timeout.
            self._fail_waiters(PortunusError("upstream_crash", "SSE stream closed"))

    async def _on_endpoint(self, raw: str) -> None:
        if self._endpoint_fut.done():
            return
        try:
            base = self.c._parse_start()
            if raw.startswith("http://") or raw.startswith("https://"):
                msg = httpx.URL(raw)
            else:
                msg = base.join(raw)
            # Same-origin only — never follow a cross-origin endpoint injection.
            if (
                (msg.scheme or "").lower() != (base.scheme or "").lower()
                or (msg.host or "").lower() != (base.host or "").lower()
                or msg.port != base.port
            ):
                raise PortunusError("upstream_crash", "SSE endpoint changed origin")
            # Re-validate through SSRF policy.
            await self.c._target(msg, prev=None)
            self._endpoint_fut.set_result(msg)
        except Exception as e:  # noqa: BLE001
            if not self._endpoint_fut.done():
                self._endpoint_fut.set_exception(e)

    async def _post_notify(self, message: dict[str, Any]) -> None:
        assert self._endpoint is not None
        body = json.dumps(message, separators=(",", ":")).encode()
        response, data = await self.c.send(
            self.post_http, "POST", body=body, headers=self._headers(), url=self._endpoint
        )
        if response.status_code in (401, 403):
            raise PortunusError("auth_required", "remote server requires authorization")
        if response.status_code not in (200, 202, 204):
            raise PortunusError(
                "upstream_crash", f"remote server returned HTTP {response.status_code}"
            )

    async def rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        input_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_schema  # classic SSE has no Mcp-Param mirroring
        assert self._endpoint is not None
        self._next_id += 1
        mid = self._next_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[mid] = fut
        message = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}
        body = json.dumps(message, separators=(",", ":")).encode()
        try:
            response, data = await self.c.send(
                self.post_http, "POST", body=body, headers=self._headers(), url=self._endpoint
            )
        except Exception:
            self._pending.pop(mid, None)
            raise
        if response.status_code in (401, 403):
            self._pending.pop(mid, None)
            raise PortunusError("auth_required", "remote server requires authorization")
        if response.status_code not in (200, 202, 204):
            self._pending.pop(mid, None)
            raise PortunusError(
                "upstream_crash", f"remote server returned HTTP {response.status_code}"
            )
        # Prefer an immediate JSON body; otherwise wait for the SSE frame.
        if data:
            try:
                obj = json.loads(data.decode("utf-8"))
                if isinstance(obj, dict) and obj.get("id") == mid and (
                    "result" in obj or "error" in obj
                ):
                    self._pending.pop(mid, None)
                    if "error" in obj:
                        err = obj["error"] if isinstance(obj["error"], dict) else {}
                        raise _portunus_error_from_rpc(err)
                    result = obj.get("result")
                    if not isinstance(result, dict):
                        raise PortunusError("upstream_crash", "malformed JSON-RPC result")
                    return result
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        try:
            msg = await asyncio.wait_for(fut, timeout=self._rpc_timeout_s)
        except TimeoutError:
            self._pending.pop(mid, None)
            raise PortunusError("upstream_timeout", "SSE response timed out") from None
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise _portunus_error_from_rpc(err)
        result = msg.get("result")
        if not isinstance(result, dict):
            raise PortunusError("upstream_crash", "malformed JSON-RPC result")
        return result

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._sse_response is not None:
            with contextlib.suppress(Exception):
                await self._sse_response.aclose()
            self._sse_response = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()


class _Session:
    def __init__(self, client: RemoteClient, http: httpx.AsyncClient) -> None:
        self.c = client
        self.http = http
        self.session_id: str | None = None
        self.protocol = MODERN_PROTOCOL_VERSION
        self.modern = False
        self._next_id = 0

    def _client_meta(self) -> dict[str, Any]:
        return {
            "io.modelcontextprotocol/protocolVersion": self.protocol,
            "io.modelcontextprotocol/clientInfo": dict(CLIENT_INFO),
            "io.modelcontextprotocol/clientCapabilities": {},
        }

    def _headers(
        self,
        *,
        method: str | None = None,
        params: dict[str, Any] | None = None,
        input_schema: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        h = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            **self.c._headers,
            # Always advertise the negotiated / intended revision (legacy initialize needs
            # this before a session id exists; peers like flomo reject 2026-only probes).
            "mcp-protocol-version": self.protocol,
        }
        if self.modern and method:
            h["mcp-method"] = method
            if method in _METHODS_WITH_NAME and isinstance(params, dict):
                name = params.get("name") or params.get("uri")
                if isinstance(name, str) and name:
                    # Non-ASCII / control-char values use the =?base64?...?= sentinel.
                    h["mcp-name"] = encode_header_value(name)
            if method == "tools/call" and input_schema is not None and isinstance(params, dict):
                args = params.get("arguments")
                # Only mirror from a schema whose annotations validate; an invalid one was
                # supposed to be dropped at probe time (stored schemas come from there).
                if isinstance(args, dict) and find_invalid_x_mcp_header(input_schema) is None:
                    h.update(mcp_param_headers(x_mcp_header_map(input_schema), args))
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    @staticmethod
    def _body_excerpt(data: bytes, limit: int = 512) -> str:
        text = data[:limit].decode("utf-8", errors="replace").replace("\n", " ").strip()
        return text[:limit]

    @classmethod
    def _log_upstream_failure(
        cls,
        *,
        phase: str,
        status: int,
        data: bytes,
        method: str | None = None,
    ) -> None:
        log.warning(
            "remote upstream failure phase=%s method=%s status=%s body=%r",
            phase,
            method or "-",
            status,
            cls._body_excerpt(data),
        )

    @staticmethod
    def _is_premodern_http(code: int) -> bool:
        """HTTP statuses on the discover probe that mean 'peer has no server/discover'.

        Only unsupported-endpoint / method / media-type responses fall back to initialize.
        Auth, size, timeout, and rate-limit statuses (401/403/408/413/425/429, …) hard-fail.
        Spec-legal peers may reject an unknown protocol version with plain 400.
        """
        return code in (400, 404, 405, 406, 415)

    def _check_status(
        self,
        response: httpx.Response,
        data: bytes = b"",
        *,
        method: str | None = None,
        phase: str = "rpc",
    ) -> None:
        code = response.status_code
        if code in (401, 403):
            self._log_upstream_failure(phase=phase, status=code, data=data, method=method)
            raise PortunusError("auth_required", "remote server requires authorization")
        if code == 413:
            self._log_upstream_failure(phase=phase, status=code, data=data, method=method)
            raise PortunusError("payload_too_large", "remote rejected payload as too large")
        if code not in (200, 202):
            self._log_upstream_failure(phase=phase, status=code, data=data, method=method)
            raise PortunusError("upstream_crash", f"remote server returned HTTP {code}")

    async def _post(
        self,
        message: dict[str, Any],
        *,
        method: str | None = None,
        params: dict[str, Any] | None = None,
        input_schema: dict[str, Any] | None = None,
    ) -> tuple[httpx.Response, bytes]:
        body = json.dumps(message, separators=(",", ":")).encode()
        response, data = await self.c.send(
            self.http,
            "POST",
            body=body,
            headers=self._headers(method=method, params=params, input_schema=input_schema),
        )
        self._check_status(response, data, method=method, phase="rpc")
        return response, data

    async def connect(self) -> None:
        """Prefer ``server/discover`` (2026-07-28); fall back to initialize handshake."""
        if await self._try_modern_discover():
            return
        await self._legacy_initialize()

    @staticmethod
    def _advertised_supported(err: Mapping[str, Any]) -> list[str]:
        data = err.get("data")
        supported = data.get("supported") if isinstance(data, dict) else None
        return supported if isinstance(supported, list) else []

    @staticmethod
    def _unsupported_message(err: Mapping[str, Any]) -> str:
        supported = _Session._advertised_supported(err)
        if supported:
            return f"remote server does not speak MCP {MODERN_PROTOCOL_VERSION} (supports {supported})"
        return f"remote server does not speak MCP {MODERN_PROTOCOL_VERSION}"

    def _select_advertised_version(self, err: Mapping[str, Any]) -> bool:
        """On ``-32022`` pick a mutually supported version from the advertised list.

        Spec 2026-07-28 era rules: a recognized modern error means a modern server, so the
        fallback (when the list only offers handshake revisions) follows the server's own
        advertisement rather than guessing "pre-modern peer". Returns False when no
        mutually supported version exists.
        """
        handshake = [
            v
            for v in self._advertised_supported(err)
            if isinstance(v, str) and v in HANDSHAKE_PROTOCOL_VERSIONS
        ]
        if not handshake:
            return False
        self.modern = False
        self.protocol = max(handshake)  # date-shaped revisions sort newest-last
        return True

    def _handle_modern_error(self, err: Mapping[str, Any]) -> bool | None:
        """Classify a JSON-RPC error seen on the discover probe.

        Returns True (an advertised handshake version was selected — switch to legacy),
        False (no mutually supported version — raise), or None (not a modern-era signal).
        ``-32020`` / ``-32021`` raise directly: a modern server rejected our correct
        headers or needs capabilities the gateway does not declare; guessing "legacy
        peer" would erase the real cause.
        """
        rpc_code = err.get("code")
        if isinstance(rpc_code, bool) or not isinstance(rpc_code, int):
            # Malformed upstream error object (unhashable / non-numeric code): no era signal.
            return None
        if rpc_code == UNSUPPORTED_PROTOCOL_VERSION:
            return self._select_advertised_version(err)
        if rpc_code in _MODERN_ERROR_CODES:
            # A modern server rejected our (correct) headers or requires capabilities the
            # gateway does not declare: surface it instead of mis-erasing the peer.
            raise PortunusError("unsupported", f"remote server rejected the probe: {rpc_code}")
        return None

    async def _try_modern_discover(self) -> bool:
        self.modern = True
        self.protocol = MODERN_PROTOCOL_VERSION
        self._next_id += 1
        mid = self._next_id
        params = {"_meta": self._client_meta()}
        message = {"jsonrpc": "2.0", "id": mid, "method": "server/discover", "params": params}
        body = json.dumps(message, separators=(",", ":")).encode()
        response, data = await self.c.send(
            self.http,
            "POST",
            body=body,
            headers=self._headers(method="server/discover", params=params),
        )
        code = response.status_code
        if code in (401, 403):
            self._log_upstream_failure(
                phase="discover", status=code, data=data, method="server/discover"
            )
            raise PortunusError("auth_required", "remote server requires authorization")
        if code == 413:
            self._log_upstream_failure(
                phase="discover", status=code, data=data, method="server/discover"
            )
            raise PortunusError("payload_too_large", "remote rejected payload as too large")
        if code not in (200, 202):
            # Inspect the body before falling back: a recognized modern JSON-RPC error
            # identifies a modern server even when it arrives as a plain 4xx.
            err = _rpc_error_of(data)
            if err is not None:
                verdict = self._handle_modern_error(err)
                if verdict is False:
                    raise PortunusError("unsupported", self._unsupported_message(err))
                if verdict is True:
                    self._log_upstream_failure(
                        phase="discover-advertised", status=code, data=data, method="server/discover"
                    )
                    return False
            if self._is_premodern_http(code):
                # Pre-2026 peers often answer unknown protocol with plain HTTP 4xx.
                self._log_upstream_failure(
                    phase="discover-fallback", status=code, data=data, method="server/discover"
                )
                self.modern = False
                return False
            self._log_upstream_failure(
                phase="discover", status=code, data=data, method="server/discover"
            )
            raise PortunusError("upstream_crash", f"remote server returned HTTP {code}")
        msg = _extract_response(response, data, mid)
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            verdict = self._handle_modern_error(err)
            if verdict is False:
                raise PortunusError("unsupported", self._unsupported_message(err))
            if verdict is True:
                return False
            rpc_code = err.get("code")
            # Only unknown/invalid method means "pre-2026 peer"; transport errors must surface.
            if rpc_code not in (-32601, -32600):
                raise PortunusError("upstream_crash", f"upstream JSON-RPC error {rpc_code}")
            self.modern = False
            return False
        result = msg.get("result")
        if not isinstance(result, dict):
            self.modern = False
            return False
        versions = result.get("supportedVersions") or result.get("supported_versions")
        if isinstance(versions, list) and MODERN_PROTOCOL_VERSION not in versions and versions:
            # Peer answered discover but does not list the modern revision — stay legacy.
            self.modern = False
            return False
        return True

    async def _legacy_initialize(self) -> None:
        self.modern = False
        if self.protocol == MODERN_PROTOCOL_VERSION:
            self.protocol = HANDSHAKE_PROTOCOL_VERSION
        result = await self.rpc(
            "initialize",
            {
                "protocolVersion": self.protocol,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            },
        )
        proto = result.get("protocolVersion")
        if isinstance(proto, str) and proto:
            self.protocol = proto
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        input_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        wire_params = dict(params)
        if self.modern:
            meta = wire_params.get("_meta")
            if not isinstance(meta, dict):
                meta = {}
            wire_params["_meta"] = {**meta, **self._client_meta()}
        self._next_id += 1
        mid = self._next_id
        response, data = await self._post(
            {"jsonrpc": "2.0", "id": mid, "method": method, "params": wire_params},
            method=method,
            params=wire_params,
            input_schema=input_schema,
        )
        if method == "initialize":
            sid = response.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
        msg = _extract_response(response, data, mid)
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise _portunus_error_from_rpc(err)
        result = msg.get("result")
        if not isinstance(result, dict):
            raise PortunusError("upstream_crash", "malformed JSON-RPC result")
        return result

    async def close(self) -> None:
        if self.modern or not self.session_id:
            return
        with contextlib.suppress(Exception):  # best effort; never mask the operation's outcome
            await self.c.send(self.http, "DELETE", body=None, headers=self._headers())


def _extract_response(response: httpx.Response, data: bytes, mid: int) -> dict[str, Any]:
    ctype = response.headers.get("content-type", "").lower()
    candidates: list[Any] = []
    try:
        if "text/event-stream" in ctype:
            for event in re.split(r"\r?\n\r?\n", data.decode("utf-8")):
                payload = "\n".join(
                    ln[5:].lstrip(" ")
                    for ln in event.splitlines()
                    if ln.startswith("data:")
                )
                if payload:
                    candidates.append(json.loads(payload))
        else:
            candidates.append(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        raise PortunusError("upstream_crash", "malformed response body") from None
    flat: list[Any] = []
    for c in candidates:
        flat.extend(c if isinstance(c, list) else [c])
    for item in flat:
        if isinstance(item, dict) and item.get("id") == mid and ("result" in item or "error" in item):
            return item
    raise PortunusError("upstream_crash", "no response for request")


# ---- vault-backed proxy ------------------------------------------------------------------


class RemoteProxy:
    """Probe / invoke registered ``remote`` servers; vault entries are the request headers."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        vault: Vault,
        *,
        policy: RemotePolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        refresh_oauth: Callable[[uuid.UUID], Any] | None = None,
    ) -> None:
        self._sf = session_factory
        self.vault = vault
        self.policy = policy or RemotePolicy.from_env()
        self._transport = transport
        self._resolver = resolver
        # Optional: ``async (server_id) -> bool`` — on-demand refresh after upstream 401.
        self.refresh_oauth = refresh_oauth

    def bind_refresh(self, refresh_oauth: Callable[[uuid.UUID], Any]) -> None:
        self.refresh_oauth = refresh_oauth

    async def _client(self, server_id: uuid.UUID) -> RemoteClient | Any:
        async with self._sf() as s:
            server = await s.get(Server, server_id)
            if server is None:
                raise RegistryError(404, "not_found", "server not found")
            if server.kind != "remote" or not server.url:
                raise RegistryError(400, "unsupported", "not a remote server")
            url = server.url
            rows = (
                (await s.execute(select(VaultEntry).where(VaultEntry.server_id == server_id)))
                .scalars()
                .all()
            )
            headers: dict[str, str] = {}
            try:
                for row in rows:
                    # Reserved ``portunus.*`` vault names are not HTTP headers (e.g. oauth secret).
                    if row.name.lower().startswith("portunus."):
                        continue
                    plain = self.vault.open(
                        bytes(row.ciphertext),
                        vault_entry_id=str(row.id),
                        key_version=row.key_version,
                    )
                    value = plain.decode()
                    # Linear (and similar) reject lowercase ``bearer``; normalize on read
                    # so vault entries sealed before canonicalize_authorization still work.
                    if row.name.lower() == AUTH_HEADER_NAME.lower():
                        value = canonicalize_authorization(value)
                    headers[row.name] = value
            except (VaultError, UnicodeDecodeError):
                raise RegistryError(500, "vault_error", "cannot decrypt server headers") from None
        from .graph_builtin import GraphBuiltinClient, is_graph_builtin_url
        from .jmap_builtin import JmapBuiltinClient, is_jmap_builtin_url

        if is_graph_builtin_url(url):
            return GraphBuiltinClient(
                url,
                headers=headers,
                timeout_s=float(self.policy.timeout_s),
            )
        if is_jmap_builtin_url(url):
            return JmapBuiltinClient(
                url,
                headers=headers,
                timeout_s=float(self.policy.timeout_s),
            )
        return RemoteClient(
            url,
            headers=headers,
            policy=self.policy,
            transport=self._transport,
            resolver=self._resolver,
        )

    async def _with_auth_retry(self, server_id: uuid.UUID, op: Callable[[], Any]) -> Any:
        try:
            return await op()
        except PortunusError as e:
            if e.code != "auth_required" or self.refresh_oauth is None:
                raise
            refreshed = await self.refresh_oauth(server_id)
            ok = refreshed[0] if isinstance(refreshed, tuple) else bool(refreshed)
            if not ok:
                raise
            return await op()

    async def probe(self, server_id: uuid.UUID) -> list[dict[str, Any]]:
        async def once() -> list[dict[str, Any]]:
            return await (await self._client(server_id)).list_tools()

        return await self._with_auth_retry(server_id, once)

    async def invoke(
        self,
        server_id: uuid.UUID,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
        input_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke one tool; ``input_schema`` (stored at probe time) drives ``Mcp-Param-*`` mirroring."""

        async def once() -> dict[str, Any]:
            return await (await self._client(server_id)).call_tool(
                tool, arguments, timeout_s=timeout_s, input_schema=input_schema
            )

        return await self._with_auth_retry(server_id, once)
