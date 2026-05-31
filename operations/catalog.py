"""Workflow catalog and schema-version listing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class WorkflowCatalog:
    def __init__(self, workflow_dir: Path) -> None:
        self.workflow_dir = workflow_dir

    def list_workflows(self) -> list[dict[str, Any]]:
        workflows: list[dict[str, Any]] = []
        for path in sorted(self.workflow_dir.glob("*.yaml")):
            with path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            inputs = data.get("inputs") or {}
            workflows.append(
                {
                    "id": data.get("id"),
                    "version": data.get("version"),
                    "description": data.get("description"),
                    "schema_family": "workflow-template",
                    "path": str(path),
                    "required_inputs": [
                        name for name, spec in inputs.items() if isinstance(spec, dict) and spec.get("required")
                    ],
                    "optional_inputs": [
                        name for name, spec in inputs.items() if not (isinstance(spec, dict) and spec.get("required"))
                    ],
                    "entry_assignee": (data.get("entry") or {}).get("assignee"),
                }
            )
        return workflows
