"""Fleet inventory for ``GET /api/fleet`` (runners + children + gateway strip)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portunus_shared import package_fingerprint

from . import __version__ as gateway_version
from .runner_pool import RunnerPool
from .slots import SlotManager


def _iso_from_mono(mono: float | None, *, now_mono: float, now_wall: datetime) -> str | None:
    if mono is None:
        return None
    wall = now_wall.timestamp() - (now_mono - mono)
    return datetime.fromtimestamp(wall, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_GATEWAY_FPS: dict[str, str] | None = None


def gateway_fingerprints() -> dict[str, str]:
    global _GATEWAY_FPS
    if _GATEWAY_FPS is not None:
        return dict(_GATEWAY_FPS)
    out: dict[str, str] = {}
    try:
        import portunus_shared

        out["portunus_shared"] = package_fingerprint(
            Path(portunus_shared.__file__).resolve().parent
        )
    except OSError:
        pass
    try:
        import portunus_skill_bridge

        out["portunus_skill_bridge"] = package_fingerprint(
            Path(portunus_skill_bridge.__file__).resolve().parent
        )
    except OSError:
        pass
    _GATEWAY_FPS = dict(out)
    return out


def db_backend_label(database_url: str) -> str:
    url = database_url.casefold()
    if url.startswith("postgresql") or "+asyncpg" in url or "+psycopg" in url:
        return "postgresql"
    if url.startswith("sqlite"):
        return "sqlite"
    return "unknown"


def build_fleet(
    *,
    pool: RunnerPool,
    slots: SlotManager | None,
    started_at: datetime,
    database_url: str,
    server_slug: dict[Any, str] | None = None,
) -> dict[str, Any]:
    now_mono = time.monotonic()
    now_wall = datetime.now(timezone.utc)
    gw_fps = gateway_fingerprints()
    child_counts: dict[str, int] = {}
    children: list[dict[str, Any]] = []
    if slots is not None:
        for child_id, slot in slots.slots.items():
            site = slot.site_id or "local"
            child_counts[site] = child_counts.get(site, 0) + 1
            sid = slot.server_id
            slug = None
            if sid is not None and server_slug:
                slug = server_slug.get(sid)
            children.append(
                {
                    "child_id": child_id,
                    "server": {
                        "id": str(sid) if sid is not None else None,
                        "slug": slug,
                    },
                    "key_id": slot.key_id,
                    "site_id": site,
                    "state": slot.state.value,
                    "preferred_miss": bool(slot.preferred_miss),
                    "pending_stop": bool(slot.pending_stop),
                }
            )

    runners: list[dict[str, Any]] = []
    for entry in pool._entries:  # noqa: SLF001 — fleet needs every configured site
        client = entry.client
        fps = getattr(client, "fingerprints", None)
        drift = None
        if isinstance(fps, dict):
            drift = [
                name
                for name in ("portunus_shared", "portunus_skill_bridge")
                if name in gw_fps and name in fps and fps[name] != gw_fps[name]
            ]
        site = entry.site_id
        runners.append(
            {
                "site_id": site,
                "regions": list(entry.regions),
                "egress_class": getattr(client, "egress_class", None),
                "capacity": getattr(client, "capacity", None),
                "connected": bool(entry.connected),
                "connected_since": _iso_from_mono(
                    getattr(client, "connected_since", None),
                    now_mono=now_mono,
                    now_wall=now_wall,
                )
                if entry.connected
                else None,
                "last_heartbeat_at": _iso_from_mono(
                    getattr(client, "last_heartbeat_at", None),
                    now_mono=now_mono,
                    now_wall=now_wall,
                ),
                "last_disconnected_at": _iso_from_mono(
                    getattr(client, "last_disconnected_at", None),
                    now_mono=now_mono,
                    now_wall=now_wall,
                ),
                "reconnects": int(getattr(client, "reconnects", 0) or 0),
                "children": child_counts.get(site, 0),
                "fingerprints": fps,
                "drift": drift,
            }
        )

    trace_tmpl = (os.environ.get("PORTUNUS_TRACE_URL_TEMPLATE") or "").strip() or None
    return {
        "gateway": {
            "version": gateway_version,
            "started_at": started_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "db_backend": db_backend_label(database_url),
            "otel_export": bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")),
            "fingerprints": gw_fps,
            "trace_url_template": trace_tmpl,
        },
        "runners": runners,
        "children": children,
    }
