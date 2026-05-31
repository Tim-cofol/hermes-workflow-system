"""Workflow template validation and profile resolution."""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import yaml


class WorkflowValidationError(ValueError):
    """Raised when a workflow template is invalid."""


class ProfileResolutionError(ValueError):
    """Raised when workflow assignees cannot be resolved to Hermes profiles."""


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise WorkflowValidationError(f"{path} must contain a YAML object")
    return data


def apply_input_defaults(workflow: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(inputs)
    for name, spec in workflow.get("inputs", {}).items():
        if name not in resolved and isinstance(spec, dict) and "default" in spec:
            resolved[name] = spec["default"]
        if isinstance(spec, dict) and spec.get("required") and name not in resolved:
            raise WorkflowValidationError(f"Missing required input: {name}")
    return resolved


def _declared_parent_ids(node: dict[str, Any]) -> list[str]:
    parents: list[str] = []
    for parent in node.get("parents") or []:
        if isinstance(parent, str):
            parents.append(parent)
        elif isinstance(parent, dict):
            if "last" in parent:
                parents.append(str(parent["last"]))
            # "previous" is a dynamic serial expansion placeholder, not a
            # template-level parent during root DAG creation.
    return parents


def validate_output_contract(contract: Any, *, node_id: str) -> None:
    if contract is None:
        return
    if not isinstance(contract, dict):
        raise WorkflowValidationError(f"{node_id}: output_contract must be an object")
    if contract.get("type") != "object":
        raise WorkflowValidationError(f"{node_id}: output_contract.type must be object")
    if "required" in contract and not isinstance(contract["required"], list):
        raise WorkflowValidationError(f"{node_id}: output_contract.required must be a list")
    if "properties" in contract and not isinstance(contract["properties"], dict):
        raise WorkflowValidationError(f"{node_id}: output_contract.properties must be an object")


def validate_workflow(workflow: dict[str, Any]) -> None:
    for field in ["id", "version", "inputs", "entry", "nodes"]:
        if field not in workflow:
            raise WorkflowValidationError(f"Workflow missing required field: {field}")
    if not isinstance(workflow["nodes"], list) or not workflow["nodes"]:
        raise WorkflowValidationError("Workflow nodes must be a non-empty list")

    ids: list[str] = []
    for node in workflow["nodes"]:
        if not isinstance(node, dict):
            raise WorkflowValidationError("Each workflow node must be an object")
        for field in ["id", "title", "assignee", "body"]:
            if field not in node:
                raise WorkflowValidationError(f"Workflow node missing required field: {field}")
        ids.append(str(node["id"]))
        workspace = node.get("workspace")
        if workspace is not None and not isinstance(workspace, dict):
            raise WorkflowValidationError(f"{node['id']}: workspace must be an object")
        validate_output_contract(node.get("output_contract"), node_id=str(node["id"]))

    if len(ids) != len(set(ids)):
        raise WorkflowValidationError("Workflow node ids must be unique")

    valid_ids = set(ids)
    for node in workflow["nodes"]:
        for parent in _declared_parent_ids(node):
            if parent not in valid_ids:
                raise WorkflowValidationError(f"{node['id']}: unknown parent {parent}")

    topological_nodes(workflow["nodes"])


def topological_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(node["id"]): node for node in nodes}
    children: dict[str, list[str]] = defaultdict(list)
    in_degree = {node_id: 0 for node_id in by_id}

    for node in nodes:
        node_id = str(node["id"])
        for parent in _declared_parent_ids(node):
            children[parent].append(node_id)
            in_degree[node_id] += 1

    queue = deque([node_id for node_id, degree in in_degree.items() if degree == 0])
    ordered_ids: list[str] = []
    while queue:
        node_id = queue.popleft()
        ordered_ids.append(node_id)
        for child in children[node_id]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(ordered_ids) != len(nodes):
        raise WorkflowValidationError("Workflow DAG must not contain cycles")
    return [by_id[node_id] for node_id in ordered_ids]


def template_parent_node_ids(node: dict[str, Any]) -> list[str]:
    return _declared_parent_ids(node)


def load_profile_registry(path: Path) -> dict[str, Any]:
    data = load_yaml(path)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        raise WorkflowValidationError(f"{path}: profiles must be an object")
    return profiles


def resolve_assignees(
    workflow: dict[str, Any],
    profile_registry: dict[str, Any],
    available_profiles: list[str],
) -> dict[str, str]:
    available = set(available_profiles)
    logical_names = {workflow["entry"]["assignee"]}
    logical_names.update(str(node["assignee"]) for node in workflow["nodes"])
    missing: list[str] = []
    resolved: dict[str, str] = {}

    for name in sorted(logical_names):
        registry_entry = profile_registry.get(name)
        hermes_profile = registry_entry.get("hermes_profile") if isinstance(registry_entry, dict) else name
        required = True if not isinstance(registry_entry, dict) else registry_entry.get("required", True)
        if hermes_profile in available:
            resolved[name] = str(hermes_profile)
        elif required:
            missing.append(name)

    if missing:
        raise ProfileResolutionError("Missing required Hermes profiles: " + ", ".join(missing))
    return resolved
