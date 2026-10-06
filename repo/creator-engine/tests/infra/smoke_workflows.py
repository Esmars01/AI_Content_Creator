"""Workflow and activity used by the Temporal smoke test.

Kept in its own module with only workflow-safe imports, because the Temporal workflow
sandbox re-imports the defining module and rejects modules that pull in non-deterministic
libraries (urllib, boto3, ...). Production workflows follow the same rule (§9).
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel
from temporalio import activity, workflow


class Greeting(BaseModel):
    name: str
    excited: bool = False


@activity.defn
async def compose_greeting(greeting: Greeting) -> str:
    return f"Hello, {greeting.name}{'!' if greeting.excited else '.'}"


@workflow.defn
class SmokeWorkflow:
    @workflow.run
    async def run(self, greeting: Greeting) -> str:
        return await workflow.execute_activity(
            compose_greeting, greeting, start_to_close_timeout=timedelta(seconds=10)
        )
