"""Hermes dashboard triage-card adapter."""

from __future__ import annotations

from typing import Any

from router.app import WorkflowRouterApp


def route_kanban_triage_task(
    task: dict[str, Any],
    *,
    app: WorkflowRouterApp,
    workflow_alias: str = "dev-feature",
    requested_by: str | None = None,
) -> dict[str, Any]:
    status = str(task.get("status", "triage"))
    if status != "triage":
        raise ValueError("Only triage tasks can be routed into a workflow")
    task_id = str(task.get("id"))
    title = str(task.get("title", ""))
    body = str(task.get("body", ""))
    board = str(task.get("board", "default"))
    tenant = str(task.get("tenant", "default"))
    requirement_text = "\n\n".join(part for part in [title, body] if part)
    inputs: dict[str, Any] = {
        "issue": task_id,
        "board": board,
        "tenant": tenant,
        "requirement_text": requirement_text,
    }
    if app.default_repo:
        inputs["repo"] = app.default_repo
    return app.create_run(
        workflow_alias,
        {
            "inputs": inputs,
            "source": {
                "type": "hermes_dashboard_triage",
                "task_id": task_id,
                "board": board,
                "tenant": tenant,
            },
            "requested_by": requested_by,
        },
    )
