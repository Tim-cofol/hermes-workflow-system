"""Metrics derived from the workflow run index."""

from __future__ import annotations

from collections import Counter
from typing import Any

from instantiator.state_index import WorkflowRunIndex


def compute_metrics(index: WorkflowRunIndex) -> dict[str, Any]:
    runs = index.all_runs()
    actions = index.operator_actions()
    return {
        "total_runs": len(runs),
        "by_workflow": dict(Counter(str(run.get("workflow_id")) for run in runs)),
        "by_board": dict(Counter(str(run.get("board")) for run in runs)),
        "by_instantiation_status": dict(Counter(str(run.get("instantiation_status")) for run in runs)),
        "operator_actions": dict(Counter(str(action.get("action")) for action in actions)),
    }
