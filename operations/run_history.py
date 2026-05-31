"""Run history and lightweight dashboard rendering."""

from __future__ import annotations

from html import escape
from typing import Any

from instantiator.state_index import WorkflowRunIndex


class RunHistory:
    def __init__(self, *, index: WorkflowRunIndex, hermes: Any | None = None) -> None:
        self.index = index
        self.hermes = hermes

    def list_runs(
        self,
        *,
        board: str | None = None,
        workflow_id: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        runs = self.index.all_runs()
        if board is not None:
            runs = [run for run in runs if run.get("board") == board]
        if workflow_id is not None:
            runs = [run for run in runs if run.get("workflow_id") == workflow_id]
        if project is not None:
            runs = [
                run
                for run in runs
                if run.get("board") == project
                or run.get("source", {}).get("project") == project
                or str(run.get("source", {}).get("repo", "")).endswith("/" + project)
            ]
        return sorted(runs, key=lambda run: run.get("created_at", ""))

    def replay_mapping(self, workflow_run_id: str) -> dict[str, Any]:
        run = self.index.get_by_id(workflow_run_id)
        if not run:
            raise KeyError(workflow_run_id)
        return {
            "workflow_run_id": workflow_run_id,
            "root_task_id": run.get("root_task_id"),
            "node_to_task_id": run.get("node_to_task_id", {}),
            "idempotency_key": run.get("idempotency_key"),
        }

    def render_dashboard_html(self, *, board: str | None = None) -> str:
        rows = []
        for run in self.list_runs(board=board):
            rows.append(
                "<tr>"
                f"<td>{escape(str(run.get('workflow_run_id')))}</td>"
                f"<td>{escape(str(run.get('workflow_id')))}</td>"
                f"<td>{escape(str(run.get('board')))}</td>"
                f"<td>{escape(str(run.get('instantiation_status')))}</td>"
                f"<td>{escape(str(run.get('root_task_id')))}</td>"
                "</tr>"
            )
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>Hermes Workflow Runs</title></head>"
            "<body><h1>Hermes Workflow Runs</h1><table>"
            "<thead><tr><th>Run</th><th>Workflow</th><th>Board</th><th>Instantiation</th><th>Root</th></tr></thead>"
            "<tbody>"
            + "".join(rows)
            + "</tbody></table></body></html>"
        )
