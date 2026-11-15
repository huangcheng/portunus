"""Temporal worker entrypoint: ``portunus-worker``.

Probe activities that need the sole-client RunnerPool run inside the gateway
process (see ``run_embedded_probe_worker``). The standalone worker handles
workflows plus OAuth / fleet / connect helpers.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import fleet as fleet_acts
from .activities import oauth as oauth_acts
from .activities import probe as probe_acts
from .settings import (
    temporal_host,
    temporal_namespace,
    temporal_probe_task_queue,
    temporal_task_queue,
)
from .workflows.connect_ensure import ConnectEnsureWorkflow
from .workflows.fleet_skills import FleetSkillsSyncWorkflow
from .workflows.oauth_refresh import OAuthRefreshWorkflow
from .workflows.server_probe import ServerProbeWorkflow

log = logging.getLogger("portunus.worker")


def _probe_activities():
    """Activities that must share the gateway's RunnerPool (sole client)."""
    return [
        probe_acts.probe_server,
        probe_acts.enable_server_and_tools,
    ]


def _standalone_activities():
    return [
        oauth_acts.list_oauth_refresh_plan,
        oauth_acts.refresh_oauth,
        oauth_acts.mark_auth_required,
        probe_acts.list_probe_plan,
        probe_acts.ensure_full_grants,
        probe_acts.start_connect_children,
        fleet_acts.sync_skills_host,
    ]


async def run_embedded_probe_worker() -> None:
    """Run inside the gateway event loop: probe activities + shared Registry."""
    host = temporal_host()
    if not host:
        log.info("TEMPORAL_HOST unset; embedded probe worker not started")
        return
    client = await Client.connect(host, namespace=temporal_namespace())
    queue = temporal_probe_task_queue()
    worker = Worker(
        client,
        task_queue=queue,
        workflows=[],  # workflows stay on the standalone worker
        activities=_probe_activities(),
    )
    # warning: gateway uvicorn often hides INFO from third-party loggers
    log.warning(
        "gateway embedded probe worker listening queue=%s namespace=%s",
        queue,
        temporal_namespace(),
    )
    await worker.run()


async def run() -> None:
    host = temporal_host()
    if not host:
        log.error("TEMPORAL_HOST is required")
        sys.exit(2)
    client = await Client.connect(host, namespace=temporal_namespace())
    worker = Worker(
        client,
        task_queue=temporal_task_queue(),
        workflows=[
            OAuthRefreshWorkflow,
            ServerProbeWorkflow,
            ConnectEnsureWorkflow,
            FleetSkillsSyncWorkflow,
        ],
        activities=_standalone_activities(),
    )
    log.info(
        "portunus-worker listening queue=%s namespace=%s host=%s",
        temporal_task_queue(),
        temporal_namespace(),
        host,
    )
    await worker.run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
