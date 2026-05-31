"""Approval gate policy helpers."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from typing import Any

from instantiator.state_index import WorkflowRunIndex


@dataclass(frozen=True)
class ApprovalDecision:
    required: bool
    reason: str | None = None
    matched_path: str | None = None
    matched_pattern: str | None = None


def requires_approval(paths: list[str], *, protected_paths: list[str]) -> ApprovalDecision:
    for path in paths:
        for pattern in protected_paths:
            if fnmatch.fnmatch(path, pattern) or path == pattern:
                return ApprovalDecision(
                    required=True,
                    reason=f"protected path matched: {path}",
                    matched_path=path,
                    matched_pattern=pattern,
                )
    return ApprovalDecision(required=False)


def ensure_approval_gate(
    *,
    index: WorkflowRunIndex,
    hermes: Any,
    workflow_run_id: str,
    reason: str | None,
    approvers: list[str],
) -> dict[str, Any]:
    run = index.get_by_id(workflow_run_id)
    if not run:
        raise KeyError(workflow_run_id)
    gate_id = hermes.create_or_reuse_task(
        title=f"manual_gate: {run['workflow_id']} {workflow_run_id}",
        assignee="orchestrator",
        skills=["kanban-orchestrator"],
        body=(
            "Manual approval is required before continuing this workflow.\n"
            f"Reason: {reason or 'approval required'}\n"
            "Approve by commenting /approve or unblocking according to project policy."
        ),
        idempotency_key=f"{run['idempotency_key']}:manual_gate:{reason or 'approval'}",
        parents=[run["root_task_id"]],
        metadata={
            "workflow_run_id": workflow_run_id,
            "workflow_id": run["workflow_id"],
            "mode": "manual_gate",
            "approvers": approvers,
            "reason": reason,
        },
        workspace={"type": "dir", "repo": run.get("inputs", {}).get("repo"), "path": run.get("inputs", {}).get("repo")},
        board=run.get("board", "default"),
        tenant=run.get("tenant", "default"),
        priority=80,
    )
    action = index.append_action(
        workflow_run_id=workflow_run_id,
        action="approval_gate",
        requested_by=None,
        reason=reason,
        metadata={"gate_task_id": gate_id, "approvers": approvers},
    )
    task = dict(hermes.tasks[gate_id])
    task["operator_action_id"] = action["id"]
    return task
