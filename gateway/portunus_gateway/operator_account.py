"""Database operator account: password hash, TOTP, recovery codes, auth-event lockout.

Sessions stay opaque cookies on :class:`OperatorAuth`. TOTP challenges are
in-memory tokens on that same object (not JWTs). This module is the
account row and the tables around it.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import AuthEvent, OperatorAccount, PasskeyCredential, RecoveryCode
from .operator_auth import LOCKOUT_SECONDS, MAX_FAILURES
from .operator_crypto import (
    OperatorCryptoError,
    decrypt_totp_secret,
    encrypt_totp_secret,
    hash_recovery_code,
)

logger = logging.getLogger("portunus.gateway.operator_account")

SessionFactory = Callable[[], AsyncSession]

# Failures that share one per-IP lockout bucket. ``lockout`` rows are audit-only.
LOCKOUT_KINDS = ("login_password", "login_totp", "login_passkey", "login_recovery", "step_up")

RECOVERY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
RECOVERY_COUNT = 10
RECOVERY_RAW_LEN = 16
TOTP_ISSUER = "Portunus"


@dataclass(frozen=True)
class Account:
    username: str
    password_hash: str | None
    totp_secret: str | None
    totp_enabled: bool
    totp_last_step: int | None


@dataclass(frozen=True)
class PasskeyRow:
    credential_id: str
    public_key: str
    sign_count: int
    transports: str | None
    name: str
    created_at: datetime
    last_used_at: datetime | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _account(row: OperatorAccount) -> Account:
    return Account(
        username=row.username,
        password_hash=row.password_hash,
        totp_secret=row.totp_secret,
        totp_enabled=bool(row.totp_enabled),
        totp_last_step=row.totp_last_step,
    )


async def get_account(sf: SessionFactory) -> Account | None:
    async with sf() as s:
        row = await s.get(OperatorAccount, 1)
        return _account(row) if row is not None else None


async def count_passkeys(sf: SessionFactory) -> int:
    async with sf() as s:
        n = (await s.execute(select(func.count()).select_from(PasskeyCredential))).scalar_one()
    return int(n or 0)


async def create_account(sf: SessionFactory, username: str, password_hash: str) -> bool:
    """Insert the singleton row. False when one already exists."""
    async with sf() as s:
        if await s.get(OperatorAccount, 1) is not None:
            return False
        s.add(
            OperatorAccount(
                id=1,
                username=username,
                password_hash=password_hash,
                totp_enabled=False,
                updated_at=_now(),
            )
        )
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            return False
    return True


async def set_password_hash(sf: SessionFactory, password_hash: str) -> bool:
    async with sf() as s:
        row = await s.get(OperatorAccount, 1)
        if row is None:
            return False
        row.password_hash = password_hash
        row.updated_at = _now()
        await s.commit()
    return True


async def record_auth_event(sf: SessionFactory, kind: str, success: bool, ip: str | None, detail: str = "") -> None:
    """Best-effort insert. Auth must not 500 because the audit write failed."""
    try:
        async with sf() as s:
            s.add(
                AuthEvent(
                    kind=kind[:64],
                    success=success,
                    ip=(ip or "")[:64] or None,
                    detail=(detail or "")[:500] or None,
                )
            )
            await s.commit()
    except Exception:
        logger.warning("auth_events insert failed", exc_info=True)


def _failure_filters(ip: str, cutoff: datetime):
    return (
        AuthEvent.success.is_(False),
        AuthEvent.ip == ip,
        AuthEvent.kind.in_(LOCKOUT_KINDS),
        AuthEvent.ts >= cutoff,
    )


async def lockout_retry_after(sf: SessionFactory, ip: str) -> int:
    """Seconds until ``ip`` may try again; 0 when fewer than ``MAX_FAILURES`` in the window."""
    cutoff = _now() - timedelta(seconds=LOCKOUT_SECONDS)
    try:
        async with sf() as s:
            n = (
                await s.execute(select(func.count()).select_from(AuthEvent).where(*_failure_filters(ip, cutoff)))
            ).scalar_one()
            if n is None or int(n) < MAX_FAILURES:
                return 0
            oldest = (await s.execute(select(func.min(AuthEvent.ts)).where(*_failure_filters(ip, cutoff)))).scalar_one()
    except Exception:
        logger.warning("auth_events lockout read failed", exc_info=True)
        return 0
    if oldest is None:
        return LOCKOUT_SECONDS
    remain = LOCKOUT_SECONDS - (_now() - _aware(oldest)).total_seconds()
    return max(1, int(remain + 0.999))


def totp_provisioning_uri(secret: str, username: str) -> str:
    import pyotp

    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=TOTP_ISSUER)


def totp_verify_at(secret: str, code: str, last_step: int | None) -> int | None:
    """Verify a 6-digit code. Returns the accepted time step, or None.

    Adjacent steps cover clock drift. A step already consumed
    (``<= last_step``) is rejected. ``pyotp`` ``TOTP.at`` takes a UNIX
    timestamp in seconds, not a step index.
    """
    import pyotp

    totp = pyotp.TOTP(secret)
    now = time.time()
    for offset in (0, -1, 1):
        step = int(now // 30) + offset
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp.at(now + offset * 30), code):
            return step
    return None


def fresh_totp_secret() -> str:
    import pyotp

    return pyotp.random_base32()


def seal_totp_secret(secret: str) -> str:
    return encrypt_totp_secret(secret)


def open_totp_secret(stored: str) -> str:
    return decrypt_totp_secret(stored)


def generate_recovery_codes() -> list[dict[str, str]]:
    """``RECOVERY_COUNT`` codes: ``[{code, hash}]``. Only ``hash`` is persisted."""
    codes: list[dict[str, str]] = []
    for _ in range(RECOVERY_COUNT):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_RAW_LEN))
        display = "-".join(raw[i : i + 4] for i in range(0, RECOVERY_RAW_LEN, 4))
        codes.append({"code": display, "hash": hash_recovery_code(raw)})
    return codes


def normalize_recovery_input(raw: str) -> str:
    return "".join(ch for ch in raw.upper() if ch in RECOVERY_ALPHABET)


async def confirm_totp(sf: SessionFactory, secret: str, codes: list[dict[str, str]]) -> None:
    async with sf() as s:
        row = await s.get(OperatorAccount, 1)
        if row is None:
            raise LookupError("missing")
        row.totp_secret = seal_totp_secret(secret)
        row.totp_enabled = True
        row.totp_last_step = None
        row.updated_at = _now()
        await s.execute(delete(RecoveryCode))
        for code in codes:
            s.add(RecoveryCode(code_hash=code["hash"]))
        await s.commit()


async def set_totp_step(sf: SessionFactory, step: int) -> None:
    async with sf() as s:
        row = await s.get(OperatorAccount, 1)
        if row is None:
            return
        row.totp_last_step = step
        row.updated_at = _now()
        await s.commit()


async def disable_totp(sf: SessionFactory) -> None:
    async with sf() as s:
        row = await s.get(OperatorAccount, 1)
        if row is not None:
            row.totp_enabled = False
            row.totp_secret = None
            row.totp_last_step = None
            row.updated_at = _now()
        await s.execute(delete(RecoveryCode))
        await s.commit()


async def recovery_counts(sf: SessionFactory) -> tuple[int, int]:
    async with sf() as s:
        total = (await s.execute(select(func.count()).select_from(RecoveryCode))).scalar_one()
        remaining = (
            await s.execute(
                select(func.count()).select_from(RecoveryCode).where(RecoveryCode.used_at.is_(None))
            )
        ).scalar_one()
    return int(total or 0), int(remaining or 0)


async def replace_recovery_codes(sf: SessionFactory, codes: list[dict[str, str]]) -> None:
    async with sf() as s:
        await s.execute(delete(RecoveryCode))
        for code in codes:
            s.add(RecoveryCode(code_hash=code["hash"]))
        await s.commit()


async def consume_recovery_code(sf: SessionFactory, normalized: str) -> int | None:
    """Atomically spend a code. Returns remaining unused codes, or None if it did not match."""
    try:
        digest = hash_recovery_code(normalized)
    except OperatorCryptoError:
        return None
    async with sf() as s:
        row = (
            await s.execute(
                update(RecoveryCode)
                .where(RecoveryCode.code_hash == digest, RecoveryCode.used_at.is_(None))
                .values(used_at=_now())
                .returning(RecoveryCode.code_hash)
            )
        ).first()
        if row is None:
            await s.rollback()
            return None
        await s.commit()
    _total, remaining = await recovery_counts(sf)
    return remaining


async def list_passkeys(sf: SessionFactory) -> list[PasskeyRow]:
    async with sf() as s:
        rows = (
            (await s.execute(select(PasskeyCredential).order_by(PasskeyCredential.created_at.desc())))
            .scalars()
            .all()
        )
        return [
            PasskeyRow(
                credential_id=r.credential_id,
                public_key=r.public_key,
                sign_count=int(r.sign_count or 0),
                transports=r.transports,
                name=r.name,
                created_at=r.created_at,
                last_used_at=r.last_used_at,
            )
            for r in rows
        ]


async def passkey_allows(sf: SessionFactory) -> list[tuple[str, str | None]]:
    async with sf() as s:
        rows = (await s.execute(select(PasskeyCredential.credential_id, PasskeyCredential.transports))).all()
    return [(cid, transports) for cid, transports in rows]


async def get_passkey(sf: SessionFactory, credential_id: str) -> PasskeyRow | None:
    async with sf() as s:
        row = await s.get(PasskeyCredential, credential_id)
        if row is None:
            return None
        return PasskeyRow(
            credential_id=row.credential_id,
            public_key=row.public_key,
            sign_count=int(row.sign_count or 0),
            transports=row.transports,
            name=row.name,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
        )


async def save_passkey(
    sf: SessionFactory, *, credential_id: str, public_key: str, name: str, transports: str | None
) -> None:
    async with sf() as s:
        row = await s.get(PasskeyCredential, credential_id)
        if row is None:
            s.add(
                PasskeyCredential(
                    credential_id=credential_id,
                    public_key=public_key,
                    name=name,
                    transports=transports,
                    sign_count=0,
                )
            )
        else:
            row.public_key = public_key
            row.name = name
            row.transports = transports
        await s.commit()


async def touch_passkey(sf: SessionFactory, credential_id: str, sign_count: int) -> None:
    async with sf() as s:
        row = await s.get(PasskeyCredential, credential_id)
        if row is None:
            return
        row.sign_count = sign_count
        row.last_used_at = _now()
        await s.commit()


async def delete_passkey(sf: SessionFactory, credential_id: str) -> int:
    async with sf() as s:
        result = await s.execute(
            delete(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id)
        )
        await s.commit()
    return int(result.rowcount or 0)
