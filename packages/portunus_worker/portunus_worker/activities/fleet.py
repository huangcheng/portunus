from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from temporalio import activity

from portunus_worker.settings import FLEET_DEFAULT_COMPONENTS

log = logging.getLogger("portunus.worker.activities.fleet")

_COMPONENT_STATUS_RE = re.compile(
    r"^COMPONENT_STATUS\s+(.+)$",
    re.MULTILINE,
)


def _repo_root() -> Path:
    root = Path(os.environ.get("PORTUNUS_REPO_ROOT") or "/app")
    if (root / "deploy").is_dir():
        return root
    alt = Path("/app")
    return alt if (alt / "deploy").is_dir() else root


def _deploy_script(name: str) -> Path | None:
    root = _repo_root()
    script = root / "deploy" / name
    if script.is_file():
        return script
    alt = Path("/app/deploy") / name
    return alt if alt.is_file() else None


def parse_component_status(stdout: str) -> dict[str, str]:
    """Parse ``COMPONENT_STATUS rclone=ok skills=fail …`` from script stdout."""
    out: dict[str, str] = {}
    for match in _COMPONENT_STATUS_RE.finditer(stdout or ""):
        for part in match.group(1).split():
            if "=" not in part:
                continue
            key, _, val = part.partition("=")
            key = key.strip()
            val = val.strip()
            if key and val:
                out[key] = val
    return out


async def _run_script(script: Path, *args: str) -> dict:
    env = os.environ.copy()
    # Writable rclone cache when deploy/ is bind-mounted :ro.
    env.setdefault("PORTUNUS_RCLONE_CACHE", "/var/lib/portunus/cache/rclone")
    env.setdefault("PORTUNUS_VENDOR_CLI_CACHE", "/var/lib/portunus/cache/vendor-clis")
    proc = await asyncio.create_subprocess_exec(
        "bash",
        str(script),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(script.parent.parent),
        env=env,
    )
    out_b, err_b = await proc.communicate()
    out = (out_b or b"").decode("utf-8", errors="replace")[-4000:]
    err = (err_b or b"").decode("utf-8", errors="replace")[-4000:]
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": out,
        "stderr": err,
        "script": script.name,
        "component_status": parse_component_status(out),
    }


@activity.defn(name="sync_skills_host")
async def sync_skills_host(host: str) -> dict:
    """Run deploy/fleet-skills.sh for a single host (rsync + state dirs)."""
    script = _deploy_script("fleet-skills.sh")
    if script is None:
        return {"host": host, "ok": False, "error": "fleet-skills.sh not found"}
    result = await _run_script(script, host)
    log.info("sync_skills_host host=%s ok=%s code=%s", host, result["ok"], result["code"])
    return {"host": host, **result}


@activity.defn(name="update_providers_host")
async def update_providers_host(host: str, components: list[str] | None = None) -> dict:
    """Run deploy/fleet-providers-update.sh for rclone / skills / clis / skill_bridge."""
    script = _deploy_script("fleet-providers-update.sh")
    if script is None:
        return {
            "host": host,
            "ok": False,
            "error": "fleet-providers-update.sh not found",
        }
    comps = [c for c in (components or []) if isinstance(c, str) and c.strip()]
    args: list[str] = []
    if comps:
        args.extend(["--components", ",".join(comps)])
    args.append(host)
    result = await _run_script(script, *args)
    status = result.get("component_status") or {}
    log.info(
        "update_providers_host host=%s ok=%s code=%s components=%s status=%s",
        host,
        result["ok"],
        result["code"],
        comps or "default",
        status,
    )
    return {
        "host": host,
        "components": comps or list(FLEET_DEFAULT_COMPONENTS),
        **result,
    }
