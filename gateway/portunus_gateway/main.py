"""Process entrypoint: ``python -m portunus_gateway``.

Wires the long-lived pieces together (env -> vault/pepper/operator auth -> RunnerClient ->
SlotManager -> Registry -> Omni -> FastAPI app) and serves it with uvicorn (single worker: the
authz snapshot, slots and step-up tokens are in-process state).

Environment (see README): ``PORTUNUS_SECRETS_KEY``, ``PORTUNUS_PEPPER_FILE``,
``PORTUNUS_OPERATOR_PASSWORD`` (optional once an operator account exists), ``PORTUNUS_SOCKET``,
``PORTUNUS_TOKEN_FILE`` (or
``PORTUNUS_TOKEN`` with ``PORTUNUS_DEV=1``), ``PORTUNUS_POLICY``, ``PORTUNUS_DATABASE_URL``,
``PORTUNUS_BIND``, ``PORTUNUS_DASHBOARD_DIR``, ``PORTUNUS_RUNNER_UID``, ``PORTUNUS_DEV``.

Fleet (optional): ``PORTUNUS_RUNNERS`` replaces ``PORTUNUS_SOCKET``/``PORTUNUS_TOKEN_FILE`` with a
comma-separated list of ``[site=]socket_path:token_file`` links, one per runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pwd
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from portunus_shared import DEFAULTS

from . import authz
from .app import build_app
from .config import load_keyring, load_pepper
from .db.session import async_session
from .omni import Omni, ServerStateCache
from .operator_auth import INSECURE_COOKIE_ENV, OperatorAuth
from .registry import Registry
from .remote import RemoteProxy
from .runner_client import RunnerClient, is_network_address
from .runner_pool import RunnerPool
from . import __version__ as gateway_version
from .slots import SlotManager
from .telemetry import setup_telemetry
from .vault import Vault

log = logging.getLogger("portunus.gateway")

DEFAULT_SOCKET = "/run/portunus/runner.sock"
DEFAULT_POLICY = "/etc/portunus/runner-policy.json"
DEFAULT_BIND = "127.0.0.1:8080"
RUNNER_USER = "portunus-runner"


class ConfigError(SystemExit):
    pass


def is_dev() -> bool:
    return os.environ.get("PORTUNUS_DEV") == "1"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else float(default)


def load_runner_token(*, dev: bool) -> str:
    path = os.environ.get("PORTUNUS_TOKEN_FILE")
    if path:
        try:
            token = Path(path).read_text().strip()
        except OSError as e:
            raise ConfigError(f"cannot read PORTUNUS_TOKEN_FILE: {e.strerror}") from None
    elif dev:
        token = os.environ.get("PORTUNUS_TOKEN", "").strip()
    else:
        token = ""
    if not token:
        raise ConfigError("PORTUNUS_TOKEN_FILE is required (PORTUNUS_TOKEN is accepted in dev only)")
    return token


def parse_runner_links(raw: str) -> list[tuple[str | None, str, str]]:
    """Parse ``PORTUNUS_RUNNERS``: comma-separated ``[site=]address:token_file`` entries.

    ``address`` is a Unix socket path or a network URL (``tls://host:port`` / ``tcp://host:port``
    for remote fleet sites; the URL is separated from the token file at the *last* colon since
    URLs contain colons of their own). Returns ``(pinned_site | None, address, token_file)``.
    Without ``site=`` the link's site key is whatever the runner advertises on hello. Paths may
    not contain ``,`` or ``:``.
    """
    out: list[tuple[str | None, str, str]] = []
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        site: str | None = None
        if "=" in part:
            site, _, part = part.partition("=")
            site = site.strip()
            if not site:
                raise ConfigError("PORTUNUS_RUNNERS: empty site name before '='")
            part = part.strip()  # tolerate whitespace after `=` for network URLs too
        try:
            network_link = is_network_address(part)
        except ValueError:  # urlparse: malformed bracketed host, e.g. tls://[::1:9000:/tok
            raise ConfigError(
                "PORTUNUS_RUNNERS: malformed runner address (invalid IPv6 host?)"
            ) from None
        if network_link:
            # token_file is everything after the LAST colon, so it cannot itself contain one.
            addr, sep, token_file = part.rpartition(":")
            try:
                parsed = urlparse(addr)
                ok = bool(
                    sep
                    and token_file.strip()
                    and parsed.hostname
                    and parsed.port  # truthy: present and non-zero (>65535 already raises)
                )
            except ValueError:  # urlparse: port present but not numeric
                ok = False
            if not ok:
                raise ConfigError(
                    "PORTUNUS_RUNNERS network links must look like "
                    "[site=]tls://host:port:token_file (or tcp://host:port)"
                )
            out.append((site, addr.strip(), token_file.strip()))
            continue
        sock, sep, token_file = part.partition(":")
        if not sep or not sock.strip() or not token_file.strip() or ":" in token_file:
            raise ConfigError("PORTUNUS_RUNNERS entries must look like [site=]socket_path:token_file")
        out.append((site, sock.strip(), token_file.strip()))
    if not out:
        raise ConfigError("PORTUNUS_RUNNERS is set but lists no runners")
    return out


def _read_token_file(path: str) -> str:
    try:
        token = Path(path).read_text().strip()
    except OSError as e:
        raise ConfigError(f"cannot read runner token file: {e.strerror}") from None
    if not token:
        raise ConfigError("runner token file is empty")
    return token


def build_runner_pool(*, dev: bool, heartbeat_s: float) -> tuple[RunnerPool, RunnerClient]:
    """Pool from ``PORTUNUS_RUNNERS`` (fleet) or the single ``PORTUNUS_SOCKET`` (default).

    Returns the pool and its first client (kept as ``app.state.runner`` for v1 callers).
    The SO_PEERCRED uid gate applies to Unix links only — network (``tls://``) links
    authenticate with TLS + the runner token, so an all-remote fleet needs no local
    ``portunus-runner`` user.
    """
    raw = os.environ.get("PORTUNUS_RUNNERS", "").strip()
    pool = RunnerPool()
    if not raw:
        sock = os.environ.get("PORTUNUS_SOCKET", DEFAULT_SOCKET)
        try:
            sock_is_network = is_network_address(sock)
        except ValueError:  # urlparse: malformed bracketed host
            raise ConfigError("PORTUNUS_SOCKET: malformed runner address") from None
        if sock_is_network:
            # The single-link default is a Unix path by contract; a remote link belongs in
            # PORTUNUS_RUNNERS where the transport-aware uid gate and site pinning apply.
            raise ConfigError(
                "PORTUNUS_SOCKET is a Unix socket path; list remote runners in PORTUNUS_RUNNERS"
            )
        uid = expected_runner_uid(dev=dev)  # unix-only link; computed pre-try for a clean
        # blame when PORTUNUS_RUNNER_UID itself is malformed
        try:
            client = RunnerClient(
                sock,
                load_runner_token(dev=dev),
                heartbeat_s=heartbeat_s,
                expected_uid=uid,
                dev=dev,
            )
        except ValueError as e:
            raise ConfigError(f"PORTUNUS_SOCKET: {e}") from e
        pool.add(client)
        return pool, client
    links = parse_runner_links(raw)
    any_unix = any(not is_network_address(addr) for _, addr, _ in links)
    uid = expected_runner_uid(dev=dev) if any_unix else None
    first: RunnerClient | None = None
    for site, address, token_file in links:
        try:
            client = RunnerClient(
                address,
                _read_token_file(token_file),
                heartbeat_s=heartbeat_s,
                expected_uid=None if is_network_address(address) else uid,
                dev=dev,
            )
            pool.add(client, site_id=site)
        except ValueError as e:
            raise ConfigError(f"PORTUNUS_RUNNERS: {e}") from e
        first = first or client
    assert first is not None
    return pool, first


def load_policy_lists(path: str) -> tuple[list[str], list[str]]:
    """``(trusted_path, binary_allowlist)`` from the policy file shared with the runner.

    The runner re-validates against its own copy before every exec and stays authoritative;
    the gateway only uses this to reject bad servers early (spec §6).
    """
    try:
        data = json.loads(Path(path).read_text())
        trusted, allow = data["trusted_path"], data["binary_allowlist"]
        if not all(isinstance(x, str) for x in [*trusted, *allow]):
            raise ValueError("entries must be strings")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ConfigError(f"cannot load PORTUNUS_POLICY {path!r}: {type(e).__name__}") from None
    return list(trusted), list(allow)


def expected_runner_uid(*, dev: bool) -> int | None:
    """Peer uid the runner socket must belong to (spec §3). Dev mode skips the check."""
    raw = os.environ.get("PORTUNUS_RUNNER_UID")
    if raw:
        return int(raw)
    try:
        return pwd.getpwnam(RUNNER_USER).pw_uid
    except KeyError:
        if dev:
            return None
        raise ConfigError(
            f"set PORTUNUS_RUNNER_UID (user {RUNNER_USER!r} not found); refusing to trust any peer"
        ) from None


def default_dashboard_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "dashboard" / "dist"


def create_gateway_app() -> FastAPI:
    """Build the fully wired gateway app from the environment."""
    dev = is_dev()
    vault = Vault.from_keyring(load_keyring())
    load_pepper()  # fail fast: agent keys cannot be verified without it
    # Env password is optional: login accepts it only until /api/auth/setup creates
    # the operator account. After that the account password (and TOTP/passkey) is the path.
    # Plain-http dev setups cannot hold a Secure cookie.
    auth = OperatorAuth.from_env(
        secure_cookie=not (dev or os.environ.get(INSECURE_COOKIE_ENV) == "1")
    )
    trusted_path, binary_allowlist = load_policy_lists(
        os.environ.get("PORTUNUS_POLICY", DEFAULT_POLICY)
    )

    heartbeat_s = _env_float("PORTUNUS_HEARTBEAT_S", DEFAULTS.heartbeat_s)
    pool, client = build_runner_pool(dev=dev, heartbeat_s=heartbeat_s)

    # The Omni gate verifies keys through the module-level store (keys.verify_agent_token);
    # SlotManager/Omni get the same object injected so revokes/grants cannot diverge.
    store = authz.authz_store
    cache = ServerStateCache()
    # `registry` is bound a few lines below; the lambda resolves it at call time so
    # placement always consults the learned (server, site) health (auto failover).
    slots = SlotManager(
        pool,
        store,
        server_enabled=cache.enabled,
        vault_version=cache.vault_version,
        unhealthy_sites=lambda sid: registry.unhealthy_sites(sid),
        site_latencies=lambda sid: registry.site_latencies(sid),
    )
    slots.attach(pool)
    remote = RemoteProxy(async_session, vault)
    registry = Registry(
        async_session,
        vault,
        pool,
        trusted_path=trusted_path,
        binary_allowlist=binary_allowlist,
        slots=slots,
        remote=remote,
    )
    remote.bind_refresh(registry.refresh_oauth_access_token)
    omni = Omni(
        session_factory=async_session,
        registry=registry,
        slots=slots,
        runner=pool,
        authz=store,
        state_cache=cache,
    )
    assert omni.authz is authz.authz_store and slots.authz is authz.authz_store, "authz store mismatch"

    app = build_app(registry=registry, omni=omni, auth=auth)
    app.state.runner = client
    app.state.runner_pool = pool
    inner_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: FastAPI) -> AsyncIterator[None]:
        from datetime import datetime, timezone

        from .retention import retention_loop

        setup_telemetry(service_version=gateway_version)
        a.state.started_at = datetime.now(timezone.utc)
        await store.reload_from_db()  # authz is rebuilt from the DB on start (spec §3)
        # Single-operator: every active key may use every server.
        await authz.ensure_full_grants()
        seeded = await a.state.catalog.ensure_defaults()
        if seeded:
            log.info("seeded %d catalog apps", seeded)
        from .toml_catalog import load_apps_dir, resolve_apps_dir, set_loaded

        apps_dir = resolve_apps_dir()
        if apps_dir is not None:
            try:
                loaded = load_apps_dir(apps_dir)
                set_loaded(loaded)
                enabled = sum(1 for r in loaded.recipes if r.enabled)
                log.info(
                    "toml catalog %s digest=%s recipes=%d enabled=%d",
                    apps_dir,
                    loaded.digest[:12],
                    len(loaded.recipes),
                    enabled,
                )
                a.state.toml_catalog = loaded
            except Exception:
                log.exception("toml catalog load failed from %s", apps_dir)
                set_loaded(None)
        else:
            set_loaded(None)
        pool.start()
        stop = asyncio.Event()
        retention_task = asyncio.create_task(retention_loop(stop), name="portunus-retention")
        try:
            from portunus_worker.client import ensure_fleet_skills_schedule

            await ensure_fleet_skills_schedule()
        except Exception:  # noqa: BLE001 — Temporal optional
            log.debug("temporal fleet schedule skipped", exc_info=True)
        try:
            async with inner_lifespan(a):
                yield
        finally:
            stop.set()
            retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retention_task
            await pool.close()

    app.router.lifespan_context = lifespan

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, object]:
        # Public: no fleet inventory. Operator UI uses GET /api/status (review #8).
        return {
            "ok": True,
            "runner_connected": pool.any_connected(),
        }

    dist = Path(os.environ.get("PORTUNUS_DASHBOARD_DIR") or default_dashboard_dir())
    if (dist / "index.html").is_file():
        # Last: /api, /mcp and /healthz match first.
        app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")
    else:
        log.warning("dashboard not built (%s); serving API only", dist)
    return app


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    if not ini.is_file():
        raise ConfigError(f"alembic.ini not found at {ini}")
    command.upgrade(Config(str(ini)), "head")


def cli() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    if os.environ.get("PORTUNUS_SKIP_MIGRATE") != "1":
        run_migrations()
    app = create_gateway_app()
    host, _, port = os.environ.get("PORTUNUS_BIND", DEFAULT_BIND).rpartition(":")
    uvicorn.run(app, host=host or "127.0.0.1", port=int(port), workers=1, log_level="info")


if __name__ == "__main__":
    sys.exit(cli())
