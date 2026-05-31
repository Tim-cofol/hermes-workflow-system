"""Cron trigger adapter for scheduled workflow runs."""

from __future__ import annotations

from typing import Any

from router.app import WorkflowRouterApp


def route_cron_trigger(
    workflow_id: str,
    *,
    app: WorkflowRouterApp,
    schedule_id: str,
    inputs: dict[str, Any],
    requested_by: str | None = "cron",
) -> dict[str, Any]:
    return app.create_run(
        workflow_id,
        {
            "inputs": dict(inputs),
            "source": {"type": "cron", "schedule_id": schedule_id, "issue": inputs.get("issue")},
            "requested_by": requested_by,
        },
    )
