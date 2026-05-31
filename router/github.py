"""GitHub webhook adapters for workflow labels and comments."""

from __future__ import annotations

from typing import Any

from router.app import WorkflowRouterApp


def route_github_label_event(payload: dict[str, Any], *, app: WorkflowRouterApp) -> dict[str, Any]:
    if payload.get("action") != "labeled":
        raise ValueError("Only labeled events can trigger workflow runs")
    label = str((payload.get("label") or {}).get("name", ""))
    if not label.startswith("workflow:"):
        raise ValueError("GitHub label is not a workflow trigger")

    workflow_alias = label.split(":", 1)[1]
    repository = payload.get("repository") or {}
    issue = payload.get("issue") or {}
    sender = payload.get("sender") or {}
    issue_number = str(issue.get("number"))
    full_name = str(repository.get("full_name", ""))
    inputs = {
        "issue": issue_number,
        "board": full_name.split("/")[-1] if full_name else "default",
    }
    if app.default_repo:
        inputs["repo"] = app.default_repo

    return app.create_run(
        workflow_alias,
        {
            "inputs": inputs,
            "source": {"type": "github_issue", "repo": full_name, "issue": issue_number},
            "requested_by": sender.get("login"),
        },
    )
