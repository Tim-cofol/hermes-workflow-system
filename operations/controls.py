"""Operator controls for cancel, retry, and rerun."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from instantiator.instantiate import instantiate_workflow
from instantiator.state_index import WorkflowRunIndex


def cancel_run(
    *,
    index: WorkflowRunIndex,
    hermes: Any,
    workflow_run_id: str,
    reason: str,
    requested_by: str | None = None,
) -> dict[str, Any]:
    run = index.get_by_id(workflow_run_id)
    if not run:
        raise KeyError(workflow_run_id)
    root_task_id = run.get("root_task_id")
    if root_task_id:
        hermes.block_task(
            root_task_id,
            reason=reason,
            metadata={"operator_action": "cancel", "workflow_run_id": workflow_run_id},
        )
    return index.append_action(
        workflow_run_id=workflow_run_id,
        action="cancel",
        requested_by=requested_by,
        reason=reason,
    )


def retry_run(
    *,
    index: WorkflowRunIndex,
    workflow_run_id: str,
    requested_by: str | None = None,
) -> dict[str, Any]:
    if not index.get_by_id(workflow_run_id):
        raise KeyError(workflow_run_id)
    return index.append_action(
        workflow_run_id=workflow_run_id,
        action="retry",
        requested_by=requested_by,
        reason="operator requested retry",
    )


def rerun_workflow(
    *,
    index: WorkflowRunIndex,
    hermes: Any,
    original_run_id: str,
    requested_by: str | None,
    rerun_nonce: str,
    workflow_dir: Path,
    profile_registry_path: Path,
) -> dict[str, Any]:
    original = index.get_by_id(original_run_id)
    if not original:
        raise KeyError(original_run_id)
    result = instantiate_workflow(
        workflow_id=original["workflow_id"],
        inputs=dict(original.get("inputs") or {}),
        source={**dict(original.get("source") or {}), "rerun_of": original_run_id},
        requested_by=requested_by,
        workflow_dir=workflow_dir,
        profile_registry_path=profile_registry_path,
        index=index,
        hermes=hermes,
        idempotency_suffix=rerun_nonce,
    )
    index.append_action(
        workflow_run_id=original_run_id,
        action="rerun",
        requested_by=requested_by,
        reason="operator requested rerun",
        metadata={"new_workflow_run_id": result["workflow_run_id"], "rerun_nonce": rerun_nonce},
    )
    return result
