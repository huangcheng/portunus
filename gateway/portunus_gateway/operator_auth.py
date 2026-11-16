"""Operator authentication: argon2id password, opaque sessions, CSRF, step-up, login lockout.

* Legacy boot password: ``PORTUNUS_OPERATOR_PASSWORD`` (optional). Only its argon2id hash is
  kept in memory. Once an ``operator_account`` row exists, that env password is not a login
  path — username/password, TOTP, and passkeys on the account are.
* Sessions are opaque 256-bit random tokens in a ``Secure; HttpOnly; SameSite=Strict`` cookie.
  The database stores only ``SHA-256(token)`` plus the per-session CSRF token. No JWT.
* Every non-safe method needs ``X-CSRF-Token`` equal to the session's CSRF token.
* Step-up: ``POST /api/auth/step-up`` re-verifies the password and returns a single-use token
  (5-minute max age), bound to the session, that sensitive routes require in
  ``X-Step-Up-Token``. Tokens live in process memory (single worker) and die with the session.
* TOTP challenges are the same shape: opaque in-memory tokens, 5-minute TTL, not JWTs.
* Env-password lockout is in-process (5 failures, 15 minutes). Account login lockout is
  counted in ``auth_events``. ``X-Forwarded-For`` is honoured only when the TCP peer is in
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
CHALLENGE_TTL_SECONDS = 5 * 60
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
        if ip not in self._state:
            self._make_room(now)
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

    def _make_room(self, now: float) -> None:
        """Keep ``_state`` at or under ``_MAX_TRACKED_IPS`` before inserting a new IP.

        Prune at most once per new IP (not on every attempt). If every remaining
        entry is still live, evict the oldest unlocked window, then the lockout
        that expires soonest.
        """
        if len(self._state) < _MAX_TRACKED_IPS:
            return
        self._prune(now)
        while len(self._state) >= _MAX_TRACKED_IPS:
            victim = min(self._state, key=lambda item: self._evict_rank(item, now))
            del self._state[victim]

    def _evict_rank(self, ip: str, now: float) -> tuple[int, float]:
        _count, start, locked_until = self._state[ip]
        if locked_until > now:
            return (1, locked_until)
        return (0, start)

    def _prune(self, now: float) -> None:
        self._state = {
            ip: e
            for ip, e in self._state.items()
            if e[2] > now or (not e[2] and now - e[1] < LOCKOUT_SECONDS)
        }


class OperatorAuth:
    def __init__(
        self,
        password_hash: str | None,
        *,
        session_factory: Callable[[], AsyncSession],
        hasher: PasswordHasher | None = None,
        clock: Callable[[], float] = time.monotonic,
        secure_cookie: bool = True,
        trusted_proxies: list[str] | None = None,
    ) -> None:
        self.password_hash = password_hash or None
        self._hasher = hasher or PasswordHasher()  # argon2id by default
        self.session_factory = session_factory
        self._sf = session_factory
        self._clock = clock
        self.secure_cookie = secure_cookie
        self.throttle = LoginThrottle(clock)
        self._proxies = [ipaddress.ip_network(p, strict=False) for p in (trusted_proxies or [])]
        # sha256(step-up token) -> (session id, expiry on ``clock``)
        self._step_ups: dict[bytes, tuple[uuid.UUID, float]] = {}
        # sha256(challenge token) -> (username, expiry on ``clock``)
        self._challenges: dict[bytes, tuple[str, float]] = {}

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
        """Build auth from the environment. A missing password is allowed: login then
        requires an ``operator_account`` row (or setup)."""
        password = os.environ.get(PASSWORD_ENV)
        if session_factory is None:
            from .db.session import async_session as session_factory
        proxies = [p.strip() for p in os.environ.get(TRUSTED_PROXIES_ENV, "").split(",") if p.strip()]
        kw.setdefault("trusted_proxies", proxies)
        kw.setdefault("secure_cookie", os.environ.get(INSECURE_COOKIE_ENV) != "1")
        if password:
            return cls.from_password(password, session_factory=session_factory, **kw)
        return cls(None, session_factory=session_factory, **kw)

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

    async def hash_password(self, password: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, password)

    async def verify_password_hash(self, password_hash: str | None, password: str) -> bool:
        if not password_hash:
            return False

        def check() -> bool:
            try:
                return self._hasher.verify(password_hash, password)
            except (VerificationError, InvalidHashError):
                return False

        return await asyncio.to_thread(check)

    async def _password_ok(self, password: str) -> bool:
        return await self.verify_password_hash(self.password_hash, password)

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
        """Validate and consume a step-up token (single-use)."""
        if not token:
            return False
        digest = _sha256(token)
        entry = self._step_ups.pop(digest, None)
        if entry is None:
            return False
        sid, expires = entry
        if expires <= self._clock():
            return False
        if sid != session_id:
            return False
        return True

    # ---- TOTP challenge (opaque, in-memory, not a session) ---------------------------

    def issue_login_challenge(self, username: str) -> str:
        now = self._clock()
        self._challenges = {h: v for h, v in self._challenges.items() if v[1] > now}
        token = secrets.token_urlsafe(32)
        self._challenges[_sha256(token)] = (username, now + CHALLENGE_TTL_SECONDS)
        return token

    def login_challenge_username(self, token: str | None) -> str | None:
        """Return the username if the challenge is live. Does not consume it (retries)."""
        if not token:
            return None
        digest = _sha256(token)
        entry = self._challenges.get(digest)
        if entry is None:
            return None
        username, expires = entry
        if expires <= self._clock():
            self._challenges.pop(digest, None)
            return None
        return username

    def consume_login_challenge(self, token: str) -> bool:
        """Consume a live challenge. Returns False if it was already spent or missing."""
        return self._challenges.pop(_sha256(token), None) is not None

    # ---- audit -----------------------------------------------------------------------

    async def audit(self, actor: str, action: str, detail: dict | None = None) -> None:
        async with self._sf() as s:
            s.add(AuditEvent(actor=actor, action=action, target=None, detail=detail))
            await s.commit()


class CsrfError(Exception):
    """Valid session, missing/wrong CSRF token."""
