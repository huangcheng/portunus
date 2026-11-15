"""Temporal worker entrypoint: ``portunus-worker``."""

from __future__ import annotations

import asyncio
import logging
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import fleet as fleet_acts
from .activities import oauth as oauth_acts
from .activities import probe as probe_acts
from .settings import temporal_host, temporal_namespace, temporal_task_queue
from .workflows.connect_ensure import ConnectEnsureWorkflow
from .workflows.fleet_skills import FleetSkillsSyncWorkflow
from .workflows.oauth_refresh import OAuthRefreshWorkflow
from .workflows.server_probe import ServerProbeWorkflow

log = logging.getLogger("portunus.worker")


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
        activities=[
            oauth_acts.refresh_oauth,
            oauth_acts.mark_auth_required,
            probe_acts.probe_server,
            probe_acts.enable_server_and_tools,
            probe_acts.ensure_full_grants,
            probe_acts.start_connect_children,
            fleet_acts.sync_skills_host,
        ],
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
