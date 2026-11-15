from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from temporalio import activity

log = logging.getLogger("portunus.worker.activities.fleet")


@activity.defn(name="sync_skills_host")
async def sync_skills_host(host: str) -> dict:
    """Run deploy/fleet-skills.sh for a single host (rsync + state dirs)."""
    # Prefer PORTUNUS_REPO_ROOT; then the image layout (/app); never a host-specific path.
    root = Path(os.environ.get("PORTUNUS_REPO_ROOT") or "/app")
    script = root / "deploy" / "fleet-skills.sh"
    if not script.is_file():
        alt = Path("/app/deploy/fleet-skills.sh")
        script = alt if alt.is_file() else script
    if not script.is_file():
        return {"host": host, "ok": False, "error": f"fleet-skills.sh not found ({script})"}
    proc = await asyncio.create_subprocess_exec(
        "bash",
        str(script),
        host,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(script.parent.parent),
    )
    out_b, err_b = await proc.communicate()
    out = (out_b or b"").decode("utf-8", errors="replace")[-4000:]
    err = (err_b or b"").decode("utf-8", errors="replace")[-4000:]
    ok = proc.returncode == 0
    log.info("sync_skills_host host=%s ok=%s code=%s", host, ok, proc.returncode)
    return {"host": host, "ok": ok, "code": proc.returncode, "stdout": out, "stderr": err}
