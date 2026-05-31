"""Hermes Kanban client adapters used by the workflow instantiator."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any


class HermesClientError(RuntimeError):
    """Raised when Hermes cannot create or update a Kanban task."""


class InMemoryHermesClient:
    """Test double with Hermes-like idempotent Kanban task behavior."""

    def __init__(
        self,
        *,
        available_profiles: list[str] | None = None,
        fail_after_new_tasks: int | None = None,
    ) -> None:
        self.available_profiles = available_profiles or []
        self.fail_after_new_tasks = fail_after_new_tasks
        self.tasks: dict[str, dict[str, Any]] = {}
        self.tasks_by_idempotency_key: dict[str, str] = {}
        self._created_new_tasks = 0

    def discover_profiles(self) -> list[str]:
        return list(self.available_profiles)

    def create_or_reuse_task(
        self,
        *,
        title: str,
        assignee: str,
        skills: list[str],
        body: str,
        idempotency_key: str,
        parents: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        workspace: dict[str, Any] | str | None = None,
        board: str = "default",
        tenant: str = "default",
        priority: int | None = None,
    ) -> str:
        if idempotency_key in self.tasks_by_idempotency_key:
            task_id = self.tasks_by_idempotency_key[idempotency_key]
            task = self.tasks[task_id]
            task["parents"] = list(dict.fromkeys(parents or task.get("parents", [])))
            task["metadata"].update(metadata or {})
            return task_id

        if self.fail_after_new_tasks is not None and self._created_new_tasks >= self.fail_after_new_tasks:
            raise HermesClientError(f"Injected create failure after {self.fail_after_new_tasks} task(s)")

        task_id = f"t_{len(self.tasks) + 1:03d}"
        self._created_new_tasks += 1
        self.tasks_by_idempotency_key[idempotency_key] = task_id
        self.tasks[task_id] = {
            "id": task_id,
            "title": title,
            "assignee": assignee,
            "skills": list(skills),
            "body": body,
            "idempotency_key": idempotency_key,
            "parents": list(parents or []),
            "metadata": dict(metadata or {}),
            "workspace": workspace,
            "workspace_path": workspace.get("path") if isinstance(workspace, dict) else None,
            "board": board,
            "tenant": tenant,
            "priority": priority,
            "status": "todo",
            "block_reason": None,
            "comments": [],
            "unblocked_reason": None,
        }
        return task_id

    def complete_task(self, task_id: str, *, summary: str, metadata: dict[str, Any] | None = None) -> None:
        task = self.tasks[task_id]
        task["status"] = "done"
        task["summary"] = summary
        task["metadata"].update(metadata or {})

    def block_task(self, task_id: str, *, reason: str, metadata: dict[str, Any] | None = None) -> None:
        task = self.tasks[task_id]
        task["status"] = "blocked"
        task["block_reason"] = reason
        task["metadata"].update(metadata or {})

    def unblock_task(self, task_id: str, *, reason: str | None = None) -> None:
        task = self.tasks[task_id]
        task["status"] = "ready"
        task["unblocked_reason"] = reason

    def comment_task(self, task_id: str, text: str, *, author: str = "workflow-system") -> None:
        self.tasks[task_id].setdefault("comments", []).append({"author": author, "body": text})

    def list_tasks(
        self,
        *,
        board: str,
        status: str | None = None,
        assignee: str | None = None,
    ) -> list[dict[str, Any]]:
        tasks = []
        for task in self.tasks.values():
            if task.get("board") != board:
                continue
            if status is not None and task.get("status") != status:
                continue
            if assignee is not None and task.get("assignee") != assignee:
                continue
            tasks.append(dict(task))
        return tasks


class CliHermesClient:
    """Thin adapter around the Hermes CLI for real local use."""

    def __init__(self, hermes_bin: str | None = None) -> None:
        self.hermes_bin = hermes_bin or str(Path.home() / ".local/bin/hermes")
        self.task_boards: dict[str, str] = {}

    def discover_profiles(self) -> list[str]:
        result = subprocess.run(
            [self.hermes_bin, "profile", "list"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise HermesClientError(result.stderr or result.stdout)
        profiles: list[str] = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("Profile") or stripped.startswith("─"):
                continue
            token = stripped.split()[0].lstrip("◆")
            if token:
                profiles.append(token)
        return profiles

    def create_or_reuse_task(
        self,
        *,
        title: str,
        assignee: str,
        skills: list[str],
        body: str,
        idempotency_key: str,
        parents: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        workspace: dict[str, Any] | str | None = None,
        board: str = "default",
        tenant: str = "default",
        priority: int | None = None,
    ) -> str:
        cmd = [
            self.hermes_bin,
            "kanban",
            "--board",
            board,
            "create",
            title,
            "--assignee",
            assignee,
            "--tenant",
            tenant,
            "--idempotency-key",
            idempotency_key,
            "--created-by",
            "workflow-instantiator",
            "--json",
            "--body",
            body,
        ]
        if priority is not None:
            cmd.extend(["--priority", str(priority)])
        if workspace is not None:
            if isinstance(workspace, dict):
                workspace_type = workspace.get("type", "dir")
                workspace_path = workspace.get("path") or workspace.get("repo") or ""
                if workspace_type == "scratch":
                    cmd.extend(["--workspace", "scratch"])
                else:
                    cmd.extend(["--workspace", f"{workspace_type}:{workspace_path}"])
                if workspace_type == "worktree" and workspace.get("branch"):
                    cmd.extend(["--branch", str(workspace["branch"])])
            else:
                cmd.extend(["--workspace", workspace])
        for skill in skills:
            cmd.extend(["--skill", skill])
        for parent in parents or []:
            cmd.extend(["--parent", parent])

        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise HermesClientError(result.stderr or result.stdout)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise HermesClientError(result.stderr or result.stdout or str(exc)) from exc
        task_id = payload.get("id")
        if not task_id:
            raise HermesClientError(f"Missing task id in Hermes output: {result.stdout}")
        task_id = str(task_id)
        self.task_boards[task_id] = board
        return task_id

    def complete_task(self, task_id: str, *, summary: str, metadata: dict[str, Any] | None = None) -> None:
        cmd = [self.hermes_bin, "kanban"]
        board = self.task_boards.get(task_id)
        if board:
            cmd.extend(["--board", board])
        cmd.extend(["complete", task_id, "--summary", summary])
        if metadata:
            cmd.extend(["--metadata", json.dumps(metadata, sort_keys=True)])
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise HermesClientError(result.stderr or result.stdout)

    def block_task(self, task_id: str, *, reason: str, metadata: dict[str, Any] | None = None) -> None:
        cmd = [self.hermes_bin, "kanban"]
        board = self.task_boards.get(task_id)
        if board:
            cmd.extend(["--board", board])
        cmd.extend(["block", task_id, reason])
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise HermesClientError(result.stderr or result.stdout)

    def unblock_task(self, task_id: str, *, reason: str | None = None) -> None:
        cmd = [self.hermes_bin, "kanban"]
        board = self.task_boards.get(task_id)
        if board:
            cmd.extend(["--board", board])
        cmd.extend(["unblock"])
        if reason:
            cmd.extend(["--reason", reason])
        cmd.append(task_id)
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise HermesClientError(result.stderr or result.stdout)

    def comment_task(self, task_id: str, text: str, *, author: str = "workflow-system") -> None:
        cmd = [self.hermes_bin, "kanban"]
        board = self.task_boards.get(task_id)
        if board:
            cmd.extend(["--board", board])
        cmd.extend(["comment", task_id, text, "--author", author])
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise HermesClientError(result.stderr or result.stdout)

    def list_tasks(
        self,
        *,
        board: str,
        status: str | None = None,
        assignee: str | None = None,
    ) -> list[dict[str, Any]]:
        cmd = [self.hermes_bin, "kanban", "--board", board, "list", "--json"]
        if status:
            cmd.extend(["--status", status])
        if assignee:
            cmd.extend(["--assignee", assignee])
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise HermesClientError(result.stderr or result.stdout)
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise HermesClientError(result.stderr or result.stdout or str(exc)) from exc
        if isinstance(payload, dict):
            payload = payload.get("tasks", [])
        tasks = [dict(task) for task in payload if isinstance(task, dict)]
        for task in tasks:
            if task.get("id"):
                self.task_boards[str(task["id"])] = board
        return tasks
