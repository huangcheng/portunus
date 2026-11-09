"""Operator authentication: argon2id password, opaque sessions, CSRF, step-up, login lockout.

* One operator; the password comes from ``PORTUNUS_OPERATOR_PASSWORD`` and only its argon2id
  hash is kept in memory.
* Sessions are opaque 256-bit random tokens in a ``Secure; HttpOnly; SameSite=Strict`` cookie.
  The database stores only ``SHA-256(token)`` plus the per-session CSRF token.
* Every non-safe method needs ``X-CSRF-Token`` equal to the session's CSRF token.
* Step-up: ``POST /api/auth/step-up`` re-verifies the password and returns a 5-minute token,
  bound to the session, that sensitive routes require in ``X-Step-Up-Token``. Tokens live in
  process memory (single worker) and die with the session.
* Lockout: 5 failed password checks (login or step-up) from one client IP lock that IP out of
  password checks for 15 minutes. ``X-Forwarded-For`` is honoured only when the TCP peer is in
  ``trusted_proxies``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import AuditEvent, OperatorSession

PASSWORD_ENV = "PORTUNUS_OPERATOR_PASSWORD"
TRUSTED_PROXIES_ENV = "PORTUNUS_TRUSTED_PROXIES"
INSECURE_COOKIE_ENV = "PORTUNUS_INSECURE_COOKIE"
SESSION_COOKIE = "portunus_session"
CSRF_HEADER = "x-csrf-token"
STEP_UP_HEADER = "x-step-up-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
STEP_UP_TTL_SECONDS = 5 * 60
OPERATOR = "operator"
_MAX_TRACKED_IPS = 10_000


def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


class LoginThrottle:
    """Per-IP failed-password counter with a fixed lockout. ``clock`` is monotonic seconds."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        # ip -> [failure_count, window_start, locked_until]
        self._state: dict[str, list[float]] = {}

    def retry_after(self, ip: str) -> int:
        """Seconds until ``ip`` may try again; 0 when not locked."""
        entry = self._state.get(ip)
        if entry is None or entry[2] <= self._clock():
            return 0
        return max(1, int(entry[2] - self._clock() + 0.999))

    def record_failure(self, ip: str) -> bool:
        """Count a failure; return True if this failure locked the IP out."""
        now = self._clock()
        if len(self._state) >= _MAX_TRACKED_IPS:
            self._prune(now)
        count, start, locked_until = self._state.get(ip, [0, now, 0.0])
        if locked_until and locked_until <= now:
            count, start, locked_until = 0, now, 0.0  # lock served; start fresh
        elif not locked_until and now - start >= LOCKOUT_SECONDS:
            count, start = 0, now  # stale failures age out
        count += 1
        newly_locked = False
        if count >= MAX_FAILURES and not locked_until:
            locked_until = now + LOCKOUT_SECONDS
            newly_locked = True
        self._state[ip] = [count, start, locked_until]
        return newly_locked

    def record_success(self, ip: str) -> None:
        self._state.pop(ip, None)

    def _prune(self, now: float) -> None:
        self._state = {
            ip: e
            for ip, e in self._state.items()
            if e[2] > now or (not e[2] and now - e[1] < LOCKOUT_SECONDS)
        }


class OperatorAuth:
    def __init__(
        self,
        password_hash: str,
        *,
        session_factory: Callable[[], AsyncSession],
        hasher: PasswordHasher | None = None,
        clock: Callable[[], float] = time.monotonic,
        secure_cookie: bool = True,
        trusted_proxies: list[str] | None = None,
    ) -> None:
        self.password_hash = password_hash
        self._hasher = hasher or PasswordHasher()  # argon2id by default
        self._sf = session_factory
        self._clock = clock
        self.secure_cookie = secure_cookie
        self.throttle = LoginThrottle(clock)
        self._proxies = [ipaddress.ip_network(p, strict=False) for p in (trusted_proxies or [])]
        # sha256(step-up token) -> (session id, expiry on ``clock``)
        self._step_ups: dict[bytes, tuple[uuid.UUID, float]] = {}

    @classmethod
    def from_password(
        cls, password: str, *, hasher: PasswordHasher | None = None, **kw
    ) -> OperatorAuth:
        if not password:
            raise RuntimeError("operator password is empty")
        h = hasher or PasswordHasher()
        return cls(h.hash(password), hasher=h, **kw)

    @classmethod
    def from_env(
        cls, *, session_factory: Callable[[], AsyncSession] | None = None, **kw
    ) -> OperatorAuth:
        password = os.environ.get(PASSWORD_ENV)
        if not password:
            raise RuntimeError(f"{PASSWORD_ENV} is not set")
        if session_factory is None:
            from .db.session import async_session as session_factory
        proxies = [p.strip() for p in os.environ.get(TRUSTED_PROXIES_ENV, "").split(",") if p.strip()]
        kw.setdefault("trusted_proxies", proxies)
        kw.setdefault("secure_cookie", os.environ.get(INSECURE_COOKIE_ENV) != "1")
        return cls.from_password(password, session_factory=session_factory, **kw)

    # ---- client address --------------------------------------------------------------

    def _is_trusted(self, addr: str) -> bool:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return any(ip in net for net in self._proxies)

    def client_ip(self, request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        if not self._proxies or not self._is_trusted(peer):
            return peer
        # Walk X-Forwarded-For right to left past trusted hops; the first untrusted hop is the
        # client (anything further left is attacker-controlled).
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        for hop in reversed(hops):
            if not self._is_trusted(hop):
                return hop
        return peer

    # ---- password --------------------------------------------------------------------

    async def _password_ok(self, password: str) -> bool:
        def check() -> bool:
            try:
                return self._hasher.verify(self.password_hash, password)
            except (VerificationError, InvalidHashError):
                return False

        return await asyncio.to_thread(check)

    async def check_password(self, ip: str, password: str) -> tuple[bool, int]:
        """Throttled password check. Returns ``(ok, retry_after)``; ``retry_after > 0`` means
        the IP is locked out and the password was not examined."""
        # Check + reserve with no ``await`` in between (atomic on the event loop): the attempt is
        # counted as a failure *before* the slow argon2 verify, so concurrent requests cannot all
        # pass the lock check. A correct password clears the reservation.
        wait = self.throttle.retry_after(ip)
        if wait:
            return False, wait
        newly_locked = self.throttle.record_failure(ip)
        if await self._password_ok(password):
            self.throttle.record_success(ip)
            return True, 0
        await self.audit(
            "anonymous", "operator.lockout" if newly_locked else "operator.login_failed", {"ip": ip}
        )
        return False, 0

    # ---- sessions --------------------------------------------------------------------

    async def create_session(self) -> tuple[str, str, uuid.UUID]:
        """Return ``(cookie_token, csrf_token, session_id)``."""
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        row = OperatorSession(token_hash=_sha256(token), csrf_token=csrf)
        async with self._sf() as s:
            await s.execute(
                delete(OperatorSession).where(OperatorSession.expires_at <= datetime.now(timezone.utc))
            )
            s.add(row)
            await s.commit()
        return token, csrf, row.id

    async def lookup_session(self, token: str | None) -> OperatorSession | None:
        if not token or len(token) > 128:
            return None
        async with self._sf() as s:
            row = (
                await s.execute(
                    select(OperatorSession).where(OperatorSession.token_hash == _sha256(token))
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            expires = row.expires_at
            if expires.tzinfo is None:  # SQLite returns naive UTC
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                await s.delete(row)
                await s.commit()
                return None
            return row

    async def end_session(self, session_id: uuid.UUID) -> None:
        async with self._sf() as s:
            await s.execute(delete(OperatorSession).where(OperatorSession.id == session_id))
            await s.commit()
        self._step_ups = {h: v for h, v in self._step_ups.items() if v[0] != session_id}

    async def authenticate(self, request: Request) -> OperatorSession | None:
        """Session for the request cookie, enforcing CSRF on non-safe methods.

        Raises :class:`CsrfError` when the session is valid but the CSRF token is wrong."""
        session = await self.lookup_session(request.cookies.get(SESSION_COOKIE))
        if session is None:
            return None
        if request.method not in SAFE_METHODS:
            supplied = request.headers.get(CSRF_HEADER, "")
            if not hmac.compare_digest(supplied.encode(), session.csrf_token.encode()):
                raise CsrfError
        return session

    # ---- step-up ---------------------------------------------------------------------

    def issue_step_up(self, session_id: uuid.UUID) -> str:
        now = self._clock()
        self._step_ups = {h: v for h, v in self._step_ups.items() if v[1] > now}
        token = secrets.token_urlsafe(32)
        self._step_ups[_sha256(token)] = (session_id, now + STEP_UP_TTL_SECONDS)
        return token

    def verify_step_up(self, session_id: uuid.UUID, token: str | None) -> bool:
        if not token:
            return False
        entry = self._step_ups.get(_sha256(token))
        if entry is None:
            return False
        sid, expires = entry
        if expires <= self._clock():
            self._step_ups.pop(_sha256(token), None)
            return False
        return sid == session_id

    # ---- audit -----------------------------------------------------------------------

    async def audit(self, actor: str, action: str, detail: dict | None = None) -> None:
        async with self._sf() as s:
            s.add(AuditEvent(actor=actor, action=action, target=None, detail=detail))
            await s.commit()


class CsrfError(Exception):
    """Valid session, missing/wrong CSRF token."""
