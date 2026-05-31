"""CLI-style `/wf` command parsing."""

from __future__ import annotations

from dataclasses import dataclass
import shlex
from typing import Any

from router.app import WorkflowRouterApp


@dataclass(frozen=True)
class WorkflowRequest:
    workflow_id: str
    inputs: dict[str, Any]
    source: dict[str, Any]
    requested_by: str | None = None


def _tokens_after_wf(command: str) -> list[str]:
    tokens = shlex.split(command)
    for index, token in enumerate(tokens):
        if token == "/wf":
            return tokens[index + 1 :]
    raise ValueError("Command must contain /wf")


def parse_wf_command(
    command: str,
    *,
    aliases: dict[str, str] | None = None,
    source_type: str = "cli",
    requested_by: str | None = None,
) -> WorkflowRequest:
    aliases = aliases or {"dev-feature": "dev-feature-v3"}
    tokens = _tokens_after_wf(command)
    if not tokens:
        raise ValueError("/wf command requires a workflow id")
    workflow_id = aliases.get(tokens[0], tokens[0])
    inputs: dict[str, Any] = {}
    for token in tokens[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        inputs[key] = value
    if "issue" not in inputs and tokens[0] != workflow_id:
        inputs.setdefault("workflow_alias", tokens[0])
    source = {"type": source_type}
    if "issue" in inputs:
        source["issue"] = inputs["issue"]
    if "repo" in inputs:
        source["repo"] = inputs["repo"]
    return WorkflowRequest(workflow_id=workflow_id, inputs=inputs, source=source, requested_by=requested_by)


def run_wf_command(command: str, *, app: WorkflowRouterApp, requested_by: str | None = None) -> dict[str, Any]:
    request = parse_wf_command(
        command,
        aliases=app.aliases,
        source_type="cli",
        requested_by=requested_by,
    )
    payload = {
        "inputs": request.inputs,
        "source": request.source,
        "requested_by": requested_by or request.requested_by,
    }
    return app.create_run(request.workflow_id, payload)
