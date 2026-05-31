"""Router facade for workflow run creation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from instantiator.hermes_client import CliHermesClient
from instantiator.instantiate import instantiate_workflow
from instantiator.state_index import WorkflowRunIndex


class WorkflowRouterApp:
    """Small dependency-injected app surface for CLI, HTTP, and webhooks."""

    def __init__(
        self,
        *,
        workflow_dir: Path = Path("workflows"),
        profile_registry_path: Path = Path("profiles/registry.yaml"),
        index: WorkflowRunIndex | None = None,
        hermes: Any | None = None,
        aliases: dict[str, str] | None = None,
        default_repo: str | None = None,
    ) -> None:
        self.workflow_dir = workflow_dir
        self.profile_registry_path = profile_registry_path
        self.index = index
        self.hermes = hermes or CliHermesClient()
        self.aliases = aliases or {"dev-feature": "dev-feature-v3"}
        self.default_repo = default_repo

    def resolve_workflow_id(self, workflow_id: str) -> str:
        return self.aliases.get(workflow_id, workflow_id)

    def create_run(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        resolved_workflow_id = self.resolve_workflow_id(workflow_id)
        inputs = dict(payload.get("inputs") or {})
        if "repo" not in inputs and self.default_repo:
            inputs["repo"] = self.default_repo
        source = dict(payload.get("source") or {"type": "web_ui"})
        requested_by = payload.get("requested_by")

        index = self.index
        if index is None:
            index = WorkflowRunIndex(Path(inputs["repo"]) / ".hermes/workflow-runs/index.json")

        result = instantiate_workflow(
            workflow_id=resolved_workflow_id,
            inputs=inputs,
            source=source,
            requested_by=requested_by,
            workflow_dir=self.workflow_dir,
            profile_registry_path=self.profile_registry_path,
            index=index,
            hermes=self.hermes,
        )
        result["source"] = source
        result["inputs"] = inputs
        return result
