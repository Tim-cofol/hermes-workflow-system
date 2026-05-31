"""Compile workflow templates into Hermes Kanban DAG tasks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from instantiator.hermes_client import CliHermesClient, HermesClientError
from instantiator.renderer import render_value
from instantiator.state_index import WorkflowRunIndex
from instantiator.validator import (
    ProfileResolutionError,
    WorkflowValidationError,
    apply_input_defaults,
    load_profile_registry,
    load_yaml,
    resolve_assignees,
    template_parent_node_ids,
    topological_nodes,
    validate_workflow,
)


class InstantiationError(RuntimeError):
    """Raised when workflow DAG creation fails after partial transaction work."""


def make_root_idempotency_key(
    *,
    workflow_id: str,
    workflow_version: str,
    inputs: dict[str, Any],
    source: dict[str, Any],
    idempotency_suffix: str | None = None,
) -> str:
    source_type = str(source.get("type") or "manual")
    source_repo = str(source.get("repo") or source.get("project") or inputs.get("repo") or "local")
    source_issue = str(source.get("issue") or inputs.get("issue") or "unknown")
    key = f"{source_type}:{source_repo}:{source_issue}:workflow:{workflow_id}:version:{workflow_version}"
    if idempotency_suffix:
        key = f"{key}:rerun:{idempotency_suffix}"
    return key


def make_node_idempotency_key(root_key: str, node_id: str) -> str:
    return f"{root_key}:node:{node_id}"


def build_root_body(
    *,
    workflow_id: str,
    workflow_version: str,
    inputs: dict[str, Any],
    source: dict[str, Any],
    requested_by: str | None,
) -> str:
    body = {
        "workflow_id": workflow_id,
        "workflow_version": workflow_version,
        "inputs": inputs,
        "source": source,
        "requested_by": requested_by,
    }
    return json.dumps(body, indent=2, sort_keys=True)


def _initial_run_record(
    *,
    workflow: dict[str, Any],
    inputs: dict[str, Any],
    source: dict[str, Any],
    requested_by: str | None,
    idempotency_key: str,
    workflow_run_id: str,
    root_task_id: str | None = None,
    node_to_task_id: dict[str, str] | None = None,
    status: str = "creating",
) -> dict[str, Any]:
    return {
        "workflow_run_id": workflow_run_id,
        "workflow_id": workflow["id"],
        "workflow_version": workflow["version"],
        "board": str(inputs.get("board", "default")),
        "tenant": str(inputs.get("tenant", "default")),
        "root_task_id": root_task_id,
        "node_to_task_id": node_to_task_id or {},
        "deferred_template_nodes": [],
        "inputs": dict(inputs),
        "source": dict(source),
        "requested_by": requested_by,
        "idempotency_key": idempotency_key,
        "instantiation_status": status,
    }


def is_deferred_template_node(node: dict[str, Any]) -> bool:
    mode = str(node.get("mode", "auto"))
    if mode.startswith("dynamic_"):
        return True
    for parent in node.get("parents") or []:
        if isinstance(parent, dict) and ("last" in parent or "previous" in parent):
            return True
    return False


def _result_from_run(run: dict[str, Any]) -> dict[str, Any]:
    return dict(run)


def instantiate_workflow(
    *,
    workflow_id: str,
    inputs: dict[str, Any],
    source: dict[str, Any] | None = None,
    requested_by: str | None = None,
    workflow_dir: Path = Path("workflows"),
    profile_registry_path: Path = Path("profiles/registry.yaml"),
    index: WorkflowRunIndex | None = None,
    hermes: Any | None = None,
    idempotency_suffix: str | None = None,
) -> dict[str, Any]:
    source = source or {"type": "manual"}
    workflow = load_yaml(workflow_dir / f"{workflow_id}.yaml")
    validate_workflow(workflow)
    resolved_inputs = apply_input_defaults(workflow, inputs)

    hermes = hermes or CliHermesClient()
    profile_registry = load_profile_registry(profile_registry_path)
    assignee_map = resolve_assignees(workflow, profile_registry, hermes.discover_profiles())

    root_key = make_root_idempotency_key(
        workflow_id=workflow["id"],
        workflow_version=workflow["version"],
        inputs=resolved_inputs,
        source=source,
        idempotency_suffix=idempotency_suffix,
    )
    index = index or WorkflowRunIndex(Path(resolved_inputs["repo"]) / ".hermes/workflow-runs/index.json")
    existing = index.get_by_idempotency_key(root_key)
    if existing and existing.get("instantiation_status") == "created":
        return _result_from_run(existing)

    workflow_run_id = existing["workflow_run_id"] if existing else index.new_workflow_run_id()
    root_task_id = existing.get("root_task_id") if existing else None
    node_to_task_id = dict(existing.get("node_to_task_id", {})) if existing else {}
    deferred_template_nodes = list(existing.get("deferred_template_nodes", [])) if existing else []
    run = _initial_run_record(
        workflow=workflow,
        inputs=resolved_inputs,
        source=source,
        requested_by=requested_by,
        idempotency_key=root_key,
        workflow_run_id=workflow_run_id,
        root_task_id=root_task_id,
        node_to_task_id=node_to_task_id,
        status="creating",
    )
    index.upsert_run(run)

    try:
        context: dict[str, Any] = {
            **resolved_inputs,
            "workflow_run_id": workflow_run_id,
            "workflow_id": workflow["id"],
            "workflow_version": workflow["version"],
        }
        board = str(resolved_inputs.get("board", "default"))
        tenant = str(resolved_inputs.get("tenant", "default"))
        if not root_task_id:
            root_task_id = hermes.create_or_reuse_task(
                title=f"[WF] {workflow['id']}: issue #{resolved_inputs.get('issue', 'unknown')}",
                assignee=assignee_map[workflow["entry"]["assignee"]],
                skills=list(workflow["entry"].get("skills", [])),
                body=build_root_body(
                    workflow_id=workflow["id"],
                    workflow_version=workflow["version"],
                    inputs=resolved_inputs,
                    source=source,
                    requested_by=requested_by,
                ),
                idempotency_key=root_key,
                parents=[],
                metadata={
                    "workflow_run_id": workflow_run_id,
                    "workflow_id": workflow["id"],
                    "workflow_version": workflow["version"],
                    "source": source,
                },
                workspace={"type": "dir", "repo": resolved_inputs.get("repo"), "path": resolved_inputs.get("repo")},
                board=board,
                tenant=tenant,
                priority=70,
            )
            run["root_task_id"] = root_task_id
            index.upsert_run(run)

        for node in topological_nodes(workflow["nodes"]):
            node_id = str(node["id"])
            if is_deferred_template_node(node):
                if node_id not in deferred_template_nodes:
                    deferred_template_nodes.append(node_id)
                run["deferred_template_nodes"] = list(deferred_template_nodes)
                index.upsert_run(run)
                continue
            context["node_id"] = node_id
            parent_node_ids = template_parent_node_ids(node)
            if parent_node_ids:
                parent_task_ids = [node_to_task_id[parent_id] for parent_id in parent_node_ids]
            else:
                parent_task_ids = [root_task_id]

            task_id = hermes.create_or_reuse_task(
                title=str(render_value(node["title"], context)),
                assignee=assignee_map[node["assignee"]],
                skills=list(node.get("skills", [])),
                body=str(render_value(node["body"], context)),
                idempotency_key=make_node_idempotency_key(root_key, node_id),
                parents=parent_task_ids,
                metadata={
                    "workflow_run_id": workflow_run_id,
                    "workflow_id": workflow["id"],
                    "workflow_version": workflow["version"],
                    "node_id": node_id,
                    "workflow_assignee": node["assignee"],
                    "resolved_assignee": assignee_map[node["assignee"]],
                    "output_contract": node.get("output_contract", {}),
                    "mode": node.get("mode", "auto"),
                    "manual_gate": node.get("manual_gate"),
                    "review_policy": node.get("review_policy"),
                    "check_policy": node.get("check_policy"),
                },
                workspace=render_value(node.get("workspace"), context),
                board=board,
                tenant=tenant,
                priority=60,
            )
            node_to_task_id[node_id] = task_id
            run["node_to_task_id"] = dict(node_to_task_id)
            index.upsert_run(run)

        run["instantiation_status"] = "created"
        run["root_task_id"] = root_task_id
        run["node_to_task_id"] = node_to_task_id
        run["deferred_template_nodes"] = deferred_template_nodes
        saved = index.upsert_run(run)
        hermes.complete_task(
            root_task_id,
            summary="Workflow DAG instantiated successfully.",
            metadata={
                "workflow_run_id": workflow_run_id,
                "node_to_task_id": node_to_task_id,
            },
        )
        return _result_from_run(saved)
    except HermesClientError as exc:
        run["root_task_id"] = root_task_id
        run["node_to_task_id"] = node_to_task_id
        run["instantiation_status"] = "failed_partial"
        run["instantiation_error"] = str(exc)
        index.upsert_run(run)
        raise InstantiationError(str(exc)) from exc
    except Exception as exc:
        if isinstance(exc, (ProfileResolutionError, WorkflowValidationError)):
            raise
        run["root_task_id"] = root_task_id
        run["node_to_task_id"] = node_to_task_id
        run["instantiation_status"] = "failed_partial"
        run["instantiation_error"] = str(exc)
        index.upsert_run(run)
        raise InstantiationError(str(exc)) from exc


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--board", default="default")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--branch-prefix", default="ai/dev-feature")
    parser.add_argument("--source-type", default="cli")
    parser.add_argument("--source-repo")
    parser.add_argument("--requested-by")
    parser.add_argument("--workflow-dir", type=Path, default=Path("workflows"))
    parser.add_argument("--profile-registry", type=Path, default=Path("profiles/registry.yaml"))
    parser.add_argument("--index", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    index_path = args.index or Path(args.repo) / ".hermes/workflow-runs/index.json"
    result = instantiate_workflow(
        workflow_id=args.workflow,
        inputs={
            "repo": args.repo,
            "issue": args.issue,
            "board": args.board,
            "tenant": args.tenant,
            "base_branch": args.base_branch,
            "branch_prefix": args.branch_prefix,
        },
        source={"type": args.source_type, "repo": args.source_repo or args.repo, "issue": args.issue},
        requested_by=args.requested_by,
        workflow_dir=args.workflow_dir,
        profile_registry_path=args.profile_registry,
        index=WorkflowRunIndex(index_path),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
