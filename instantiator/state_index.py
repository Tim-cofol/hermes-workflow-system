"""Persistent workflow run transaction index.

This index records instantiation mappings and operator actions. It deliberately
does not store a separate workflow runtime status; live task state belongs to
Hermes Kanban.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class WorkflowRunIndex:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _empty(self) -> dict[str, Any]:
        return {"runs": [], "operator_actions": []}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        with self.path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("runs", [])
        data.setdefault("operator_actions", [])
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        tmp_path.replace(self.path)

    def new_workflow_run_id(self) -> str:
        return f"wf_{uuid.uuid4().hex[:12]}"

    def all_runs(self) -> list[dict[str, Any]]:
        return deepcopy(self._load()["runs"])

    def operator_actions(self) -> list[dict[str, Any]]:
        return deepcopy(self._load()["operator_actions"])

    def get_by_id(self, workflow_run_id: str) -> dict[str, Any] | None:
        for run in self._load()["runs"]:
            if run.get("workflow_run_id") == workflow_run_id:
                return deepcopy(run)
        return None

    def get_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        for run in self._load()["runs"]:
            if run.get("idempotency_key") == idempotency_key:
                return deepcopy(run)
        return None

    def upsert_run(self, run: dict[str, Any]) -> dict[str, Any]:
        data = self._load()
        now = utc_now()
        saved = deepcopy(run)
        saved.setdefault("created_at", now)
        saved["updated_at"] = now
        for index, existing in enumerate(data["runs"]):
            if existing.get("workflow_run_id") == saved.get("workflow_run_id"):
                saved["created_at"] = existing.get("created_at", saved["created_at"])
                data["runs"][index] = saved
                self._save(data)
                return deepcopy(saved)
        data["runs"].append(saved)
        self._save(data)
        return deepcopy(saved)

    def append_action(
        self,
        *,
        workflow_run_id: str,
        action: str,
        requested_by: str | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = self._load()
        record = {
            "id": f"act_{uuid.uuid4().hex[:12]}",
            "workflow_run_id": workflow_run_id,
            "action": action,
            "requested_by": requested_by,
            "reason": reason,
            "metadata": metadata or {},
            "created_at": utc_now(),
        }
        data["operator_actions"].append(record)
        self._save(data)
        return deepcopy(record)
