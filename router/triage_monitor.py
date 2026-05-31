"""One-pass monitor for routing Hermes dashboard triage cards into workflows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from instantiator.hermes_client import CliHermesClient, HermesClientError
from instantiator.state_index import WorkflowRunIndex
from operations.pr_reconciler import PrMergeReconciler
from router.app import WorkflowRouterApp
from router.triage import route_kanban_triage_task


class CliKanbanTriageSource:
    def __init__(self, hermes_bin: str | None = None) -> None:
        self.hermes_bin = hermes_bin or str(Path.home() / ".local/bin/hermes")

    def list_triage_tasks(self, *, board: str) -> list[dict[str, Any]]:
        result = subprocess.run(
            [
                self.hermes_bin,
                "kanban",
                "--board",
                board,
                "list",
                "--status",
                "triage",
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise HermesClientError(result.stderr or result.stdout)
        payload = json.loads(result.stdout or "[]")
        if isinstance(payload, dict):
            payload = payload.get("tasks", [])
        tasks: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            task = dict(item)
            task.setdefault("board", board)
            task.setdefault("status", "triage")
            tasks.append(task)
        return tasks

    def acknowledge_task(self, task: dict[str, Any], result: dict[str, Any]) -> None:
        task_id = str(task["id"])
        workflow_run_id = str(result["workflow_run_id"])
        root_task_id = str(result["root_task_id"])
        subprocess.run(
            [
                self.hermes_bin,
                "kanban",
                "--board",
                str(task.get("board", "default")),
                "comment",
                task_id,
                f"Routed to workflow_run_id={workflow_run_id}, root_task_id={root_task_id}. Source triage card archived.",
                "--author",
                "workflow-triage-monitor",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        subprocess.run(
            [
                self.hermes_bin,
                "kanban",
                "--board",
                str(task.get("board", "default")),
                "archive",
                task_id,
            ],
            text=True,
            capture_output=True,
            check=False,
        )


def route_triage_tasks_once(
    *,
    app: WorkflowRouterApp,
    workflow_alias: str = "dev-feature",
    tasks: list[dict[str, Any]] | None = None,
    triage_source: CliKanbanTriageSource | None = None,
    board: str | None = None,
    requested_by: str | None = "dashboard",
    pr_reconciler: Any | None = None,
) -> list[dict[str, Any]]:
    if tasks is None:
        if board is None:
            raise ValueError("board is required when tasks are not provided")
        triage_source = triage_source or CliKanbanTriageSource()
        tasks = triage_source.list_triage_tasks(board=board)
    results = []
    for task in tasks:
        if str(task.get("status", "triage")) != "triage":
            continue
        result = route_kanban_triage_task(
            task,
            app=app,
            workflow_alias=workflow_alias,
            requested_by=requested_by,
        )
        if triage_source is not None and hasattr(triage_source, "acknowledge_task"):
            triage_source.acknowledge_task(task, result)
        results.append(result)
    if pr_reconciler is not None and board is not None:
        try:
            pr_reconciler.reconcile_board(board)
        except Exception as exc:  # pragma: no cover - defensive service boundary
            print(f"PR merge reconciliation failed for board {board}: {exc}", file=sys.stderr)
    return results


def run_triage_monitor_loop(
    *,
    app: WorkflowRouterApp,
    triage_source: CliKanbanTriageSource,
    board: str,
    workflow_alias: str = "dev-feature",
    requested_by: str | None = "dashboard",
    interval_seconds: float = 30.0,
    max_iterations: int | None = None,
    pr_reconciler: Any | None = None,
) -> list[dict[str, Any]]:
    routed: list[dict[str, Any]] = []
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        routed.extend(
            route_triage_tasks_once(
                app=app,
                workflow_alias=workflow_alias,
                triage_source=triage_source,
                board=board,
                requested_by=requested_by,
                pr_reconciler=pr_reconciler,
            )
        )
        if max_iterations is not None and iteration >= max_iterations:
            break
        if interval_seconds > 0:
            time.sleep(interval_seconds)
    return routed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workflow", default="dev-feature")
    parser.add_argument("--workflow-dir", type=Path, default=Path("workflows"))
    parser.add_argument("--profile-registry", type=Path, default=Path("profiles/registry.yaml"))
    parser.add_argument("--index", type=Path)
    parser.add_argument("--hermes-bin", default=str(Path.home() / ".local/bin/hermes"))
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--disable-pr-reconcile", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    index = WorkflowRunIndex(args.index or Path(args.repo) / ".hermes/workflow-runs/index.json")
    app = WorkflowRouterApp(
        workflow_dir=args.workflow_dir,
        profile_registry_path=args.profile_registry,
        index=index,
        hermes=CliHermesClient(args.hermes_bin),
        default_repo=args.repo,
    )
    source = CliKanbanTriageSource(args.hermes_bin)
    pr_reconciler = None if args.disable_pr_reconcile else PrMergeReconciler(hermes=CliHermesClient(args.hermes_bin))
    if args.watch:
        results = run_triage_monitor_loop(
            app=app,
            workflow_alias=args.workflow,
            triage_source=source,
            board=args.board,
            requested_by="dashboard",
            interval_seconds=args.interval_seconds,
            max_iterations=args.max_iterations,
            pr_reconciler=pr_reconciler,
        )
    else:
        results = route_triage_tasks_once(
            app=app,
            workflow_alias=args.workflow,
            triage_source=source,
            board=args.board,
            requested_by="dashboard",
            pr_reconciler=pr_reconciler,
        )
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
