"""Temporal workers.

  python -m orchestrator.worker orchestration   # control server: workflows + light activities
  python -m orchestrator.worker agents          # worker server: sandboxed agent runs

`orchestration` also creates the 5-minute reconciler schedule if it does not exist yet.
"""

import asyncio
import logging
import os
import sys
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.worker import Worker

from core.settings import get_settings
from orchestrator.activities import github
from orchestrator.activities.agent_run import run_agent_activity
from orchestrator.supervisor.reconciler import ReconcileActivities, ReconcilerWorkflow
from orchestrator.types import AGENT_QUEUE, ORCHESTRATION_QUEUE
from orchestrator.workflows.review import ReviewWorkflow
from orchestrator.workflows.ticket import TicketWorkflow

WORKFLOWS = [TicketWorkflow, ReviewWorkflow, ReconcilerWorkflow]
RECONCILER_SCHEDULE_ID = "reconciler"


async def ensure_schedules(client: Client) -> None:
    try:
        await client.create_schedule(
            RECONCILER_SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    ReconcilerWorkflow.run, id="reconciler", task_queue=ORCHESTRATION_QUEUE
                ),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(minutes=5))]),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            ),
        )
    except ScheduleAlreadyRunningError:
        pass


async def main(kind: str) -> None:
    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    if kind == "orchestration":
        await ensure_schedules(client)
        worker = Worker(
            client,
            task_queue=ORCHESTRATION_QUEUE,
            workflows=WORKFLOWS,
            activities=[*github.ALL, ReconcileActivities(client).reconcile],
        )
    elif kind == "agents":
        # One sandbox per concurrent activity: 8 vCPU / 32 GB fits about 4 to 6.
        worker = Worker(
            client,
            task_queue=AGENT_QUEUE,
            activities=[run_agent_activity],
            max_concurrent_activities=int(os.environ.get("MAX_SANDBOXES", "4")),
        )
    else:
        raise SystemExit(f"unknown worker kind {kind!r}; use 'orchestration' or 'agents'")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "orchestration"))
