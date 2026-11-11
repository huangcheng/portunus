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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from portunus_shared import DEFAULTS, MAX_FRAME_BODY, PortunusError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Server, VaultEntry
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
_CROSS_ORIGIN_KEEP = frozenset(
    {"accept", "content-type", "mcp-protocol-version", "mcp-method", "mcp-name"}
)
_METHODS_WITH_NAME = frozenset({"tools/call", "resources/read", "prompts/get"})

Resolver = Callable[[str, int], Awaitable[list[str]]]


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
                    return tools
            raise PortunusError("upstream_crash", "tools/list pagination too long")

        return await self._run(op)

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        async def op(s: _Session) -> dict[str, Any]:
            return await s.rpc("tools/call", {"name": name, "arguments": arguments})

        return await self._run(op, timeout_s=timeout_s)

    # ---- session ----

    async def _run(
        self, op: Callable[[_Session], Awaitable[Any]], *, timeout_s: float | None = None
    ) -> Any:
        self._parse_start()  # fail fast on malformed URLs before opening anything
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

    def _parse_start(self) -> httpx.URL:
        try:
            url = httpx.URL(self.url)
        except (httpx.InvalidURL, ValueError):
            raise SsrfError("invalid url") from None
        return url

    # ---- one hop-by-hop request ----

    async def _target(self, url: httpx.URL, *, prev: _Target | None, start_loopback: bool | None):
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
        may_use_loopback = allowlisted and (start_loopback is None or start_loopback)
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
    ) -> tuple[httpx.Response, bytes]:
        """Send with manual, fully re-validated redirects. Returns the final response + body."""
        url = self._parse_start()
        hdrs = dict(headers)
        target = await self._target(url, prev=None, start_loopback=None)
        start_loopback = target.loopback
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
            nxt = await self._target(next_url, prev=target, start_loopback=start_loopback)
            if nxt.origin != target.origin:
                # Never forward caller credentials across origins, on this or any later hop.
                hdrs = {k: v for k, v in hdrs.items() if k in _CROSS_ORIGIN_KEEP}
            target = nxt

    async def _send_pinned(
        self,
        http: httpx.AsyncClient,
        method: str,
        target: _Target,
        headers: dict[str, str],
        body: bytes | None,
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
            try:
                data = await self._read_capped(response)
            finally:
                await response.aclose()
            return response, data
        raise PortunusError(
            "upstream_crash", f"cannot connect: {type(last).__name__ if last else 'no address'}"
        )

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

    def _headers(self, *, method: str | None = None, params: dict[str, Any] | None = None) -> dict[str, str]:
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
                    h["mcp-name"] = name
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
        """HTTP 4xx on the discover probe that means 'peer does not speak 2026-07-28'.

        Auth / size errors still hard-fail. Spec-legal peers may reject an unknown protocol
        version with plain 400 rather than JSON-RPC -32601.
        """
        return 400 <= code < 500 and code not in (401, 403, 413)

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
        self, message: dict[str, Any], *, method: str | None = None, params: dict[str, Any] | None = None
    ) -> tuple[httpx.Response, bytes]:
        body = json.dumps(message, separators=(",", ":")).encode()
        response, data = await self.c.send(
            self.http, "POST", body=body, headers=self._headers(method=method, params=params)
        )
        self._check_status(response, data, method=method, phase="rpc")
        return response, data

    async def connect(self) -> None:
        """Prefer ``server/discover`` (2026-07-28); fall back to initialize handshake."""
        if await self._try_modern_discover():
            return
        await self._legacy_initialize()

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
            # Pre-2026 peers often answer unknown protocol with plain HTTP 4xx.
            if self._is_premodern_http(code):
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
        self.protocol = HANDSHAKE_PROTOCOL_VERSION
        result = await self.rpc(
            "initialize",
            {
                "protocolVersion": HANDSHAKE_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            },
        )
        proto = result.get("protocolVersion")
        if isinstance(proto, str) and proto:
            self.protocol = proto
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
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
        )
        if method == "initialize":
            sid = response.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
        msg = _extract_response(response, data, mid)
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise PortunusError("upstream_crash", f"upstream JSON-RPC error {err.get('code')}")
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
    ) -> None:
        self._sf = session_factory
        self.vault = vault
        self.policy = policy or RemotePolicy.from_env()
        self._transport = transport
        self._resolver = resolver

    async def _client(self, server_id: uuid.UUID) -> RemoteClient:
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
                    headers[row.name] = plain.decode()
            except (VaultError, UnicodeDecodeError):
                raise RegistryError(500, "vault_error", "cannot decrypt server headers") from None
        return RemoteClient(
            url,
            headers=headers,
            policy=self.policy,
            transport=self._transport,
            resolver=self._resolver,
        )

    async def probe(self, server_id: uuid.UUID) -> list[dict[str, Any]]:
        return await (await self._client(server_id)).list_tools()

    async def invoke(
        self,
        server_id: uuid.UUID,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        return await (await self._client(server_id)).call_tool(
            tool, arguments, timeout_s=timeout_s
        )
