#!/usr/bin/env python3
"""Deterministically expand a Hermes dev-feature-v3 development plan."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


WORKFLOW_ID = "dev-feature-v3"


@dataclass(frozen=True)
class ExpandConfig:
    board: str
    repo: str
    issue: str
    workflow_run_id: str
    run_record_dir: str
    base_branch: str
    branch_prefix: str
    tenant: str = "default"
    parent_task_id: str | None = None
    expected_output: str = "pull_request"
    stop_after_commit: bool = False
    worktree_path: str | None = None
    hermes_bin: str | None = None

    @property
    def branch_name(self) -> str:
        return f"{self.branch_prefix}/issue-{self.issue}"

    @property
    def resolved_worktree_path(self) -> str:
        if self.worktree_path:
            return self.worktree_path
        return str(Path(self.run_record_dir) / "worktree")

    @property
    def should_create_pr(self) -> bool:
        return self.expected_output != "local_commit_only" and not self.stop_after_commit

    @property
    def resolved_hermes_bin(self) -> str:
        if self.hermes_bin:
            return self.hermes_bin
        return str(Path.home() / ".local/bin/hermes")


def load_development_plan(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    tasks = data.get("development_tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"{path} must contain a non-empty development_tasks array")
    return data


def run_git(args: list[str], *, cwd: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def is_git_worktree(path: Path) -> bool:
    if not path.exists():
        return False
    result = run_git(["rev-parse", "--is-inside-work-tree"], cwd=path)
    return result.returncode == 0 and result.stdout.strip() == "true"


def branch_exists(repo: Path, branch: str) -> bool:
    result = run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo)
    return result.returncode == 0


def ensure_git_worktree(config: ExpandConfig) -> None:
    repo = Path(config.repo).resolve()
    worktree = Path(config.resolved_worktree_path).resolve()
    if is_git_worktree(worktree):
        return

    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        if any(worktree.iterdir()):
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            backup = worktree.with_name(f"{worktree.name}.plain-backup-{stamp}")
            shutil.move(str(worktree), str(backup))
        else:
            worktree.rmdir()

    if branch_exists(repo, config.branch_name):
        args = ["worktree", "add", str(worktree), config.branch_name]
    else:
        args = ["worktree", "add", "-b", config.branch_name, str(worktree), config.base_branch]
    result = run_git(args, cwd=repo)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def body_lines_for_task(task: dict[str, Any]) -> list[str]:
    lines = [
        f"Task scope: {task.get('scope', '')}",
        "",
        f"Files expected: {', '.join(as_list(task.get('files_expected')))}",
        "",
        "Acceptance checks:",
    ]
    lines.extend(f"- {item}" for item in as_list(task.get("acceptance_checks")))
    lines.extend(["", "Risk notes:"])
    lines.extend(f"- {item}" for item in as_list(task.get("risk_notes")))
    return lines


def build_dev_body(config: ExpandConfig, task: dict[str, Any]) -> str:
    plan_item_id = str(task["id"])
    lines = [
        f"workflow_id: {WORKFLOW_ID}",
        "workflow_version: 1.3.1",
        "step: dev_impl",
        f"plan_item_id: {plan_item_id}",
        f"workflow_run_id: {config.workflow_run_id}",
        f"repo: {config.repo}",
        f"base_branch: {config.base_branch}",
        f"branch_prefix: {config.branch_prefix}",
        f"run_record_dir: {config.run_record_dir}",
        f"tenant: {config.tenant}",
        "",
        *body_lines_for_task(task),
        "",
        f"Check command: {task.get('check_command', 'auto')}",
        "",
        f"Estimated changed lines: {task.get('estimated_changed_lines', 'unknown')}",
        "",
        f"Work in this git worktree: {config.resolved_worktree_path}",
        f"Use branch {config.branch_name} from base {config.base_branch}.",
        "Implement the code and leave it uncommitted; commit_impl will commit after checks pass.",
    ]
    return "\n".join(lines)


def build_check_body(config: ExpandConfig, task: dict[str, Any]) -> str:
    plan_item_id = str(task["id"])
    check_command = str(task.get("check_command", "auto"))
    lines = [
        f"workflow_id: {WORKFLOW_ID}",
        "workflow_version: 1.3.1",
        "step: automated_check",
        f"plan_item_id: {plan_item_id}",
        f"workflow_run_id: {config.workflow_run_id}",
        f"repo: {config.repo}",
        f"run_record_dir: {config.run_record_dir}",
        f"tenant: {config.tenant}",
        "",
        f"Run checks from this worktree: {config.resolved_worktree_path}",
        "",
        f"Primary check command: {check_command}",
        "",
        "Also run this source hygiene check:",
        "python3 - <<'PY'",
        "import subprocess",
        "files = subprocess.check_output(['git', 'ls-files'], text=True).splitlines()",
        "bad = [f for f in files if f.endswith('.pyc') or '/__pycache__/' in f or f.startswith('__pycache__/')]",
        "assert not bad, bad",
        "print('OK hygiene')",
        "PY",
        "",
        "Run git diff --check.",
        "If all checks pass, complete with result PASS. If any fail, block or create a revise_impl task with exact failure output.",
    ]
    return "\n".join(lines)


def build_commit_body(config: ExpandConfig, task: dict[str, Any]) -> str:
    plan_item_id = str(task["id"])
    title = str(task.get("title", plan_item_id))
    lines = [
        f"workflow_id: {WORKFLOW_ID}",
        "workflow_version: 1.3.1",
        "step: commit_impl",
        f"plan_item_id: {plan_item_id}",
        f"workflow_run_id: {config.workflow_run_id}",
        f"repo: {config.repo}",
        f"run_record_dir: {config.run_record_dir}",
        f"tenant: {config.tenant}",
        "",
        "Commit only after automated_check completed with PASS.",
        f"Worktree: {config.resolved_worktree_path}",
        "",
        f"Commit message: feat: {title} ({plan_item_id})",
        "",
        "Do not push. Local commit only unless a downstream shipper task is created.",
    ]
    return "\n".join(lines)


def build_pr_body(config: ExpandConfig) -> str:
    return "\n".join(
        [
            f"workflow_id: {WORKFLOW_ID}",
            "workflow_version: 1.3.1",
            "step: pr",
            f"workflow_run_id: {config.workflow_run_id}",
            f"repo: {config.repo}",
            f"run_record_dir: {config.run_record_dir}",
            f"tenant: {config.tenant}",
            "",
            f"Push branch {config.branch_name}, create a PR, check CI, and write final.json.",
            "Do not merge automatically unless project policy explicitly allows it.",
        ]
    )


def task_spec(
    *,
    ref: str,
    step: str,
    title: str,
    body: str,
    assignee: str,
    skills: list[str],
    workspace: str,
    parents: list[str],
    idempotency_key: str,
    priority: int,
    branch: str | None = None,
) -> dict[str, Any]:
    return {
        "ref": ref,
        "step": step,
        "title": title,
        "body": body,
        "assignee": assignee,
        "skills": skills,
        "workspace": workspace,
        "parents": parents,
        "idempotency_key": idempotency_key,
        "priority": priority,
        "branch": branch,
    }


def build_task_specs(config: ExpandConfig, plan: dict[str, Any]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    previous_commit_ref: str | None = None
    worktree_workspace = f"worktree:{config.resolved_worktree_path}"

    for item in plan["development_tasks"]:
        plan_item_id = str(item["id"])
        title = str(item.get("title", plan_item_id))
        dev_ref = f"{plan_item_id}:dev_impl"
        check_ref = f"{plan_item_id}:automated_check"
        commit_ref = f"{plan_item_id}:commit_impl"
        dev_parents = [previous_commit_ref] if previous_commit_ref else []
        if not dev_parents and config.parent_task_id:
            dev_parents = [config.parent_task_id]

        tasks.append(
            task_spec(
                ref=dev_ref,
                step="dev_impl",
                title=f"dev_impl: {plan_item_id} {title}",
                body=build_dev_body(config, item),
                assignee="dev-codex",
                skills=["codex", "kanban-codex-lane", "test-driven-development"],
                workspace=worktree_workspace,
                parents=dev_parents,
                idempotency_key=f"{WORKFLOW_ID}:{config.workflow_run_id}:{plan_item_id}:dev_impl",
                priority=60,
                branch=config.branch_name,
            )
        )
        tasks.append(
            task_spec(
                ref=check_ref,
                step="automated_check",
                title=f"automated_check: {plan_item_id} {title}",
                body=build_check_body(config, item),
                assignee="reviewer",
                skills=["requesting-code-review"],
                workspace=worktree_workspace,
                parents=[dev_ref],
                idempotency_key=f"{WORKFLOW_ID}:{config.workflow_run_id}:{plan_item_id}:automated_check",
                priority=59,
                branch=config.branch_name,
            )
        )
        tasks.append(
            task_spec(
                ref=commit_ref,
                step="commit_impl",
                title=f"commit_impl: {plan_item_id} {title}",
                body=build_commit_body(config, item),
                assignee="dev-codex",
                skills=["codex", "kanban-codex-lane"],
                workspace=worktree_workspace,
                parents=[check_ref],
                idempotency_key=f"{WORKFLOW_ID}:{config.workflow_run_id}:{plan_item_id}:commit_impl",
                priority=58,
                branch=config.branch_name,
            )
        )
        previous_commit_ref = commit_ref

    if config.should_create_pr and previous_commit_ref:
        tasks.append(
            task_spec(
                ref="pr",
                step="pr",
                title=f"Create PR and check CI for issue #{config.issue}",
                body=build_pr_body(config),
                assignee="shipper",
                skills=["github-pr-workflow"],
                workspace=f"dir:{config.run_record_dir}",
                parents=[previous_commit_ref],
                idempotency_key=f"{WORKFLOW_ID}:{config.workflow_run_id}:pr",
                priority=50,
            )
        )
    return tasks


def create_task(config: ExpandConfig, spec: dict[str, Any], parent_ids: list[str]) -> str:
    cmd = [
        config.resolved_hermes_bin,
        "kanban",
        "--board",
        config.board,
        "create",
        spec["title"],
        "--assignee",
        spec["assignee"],
        "--workspace",
        spec["workspace"],
        "--tenant",
        config.tenant,
        "--priority",
        str(spec["priority"]),
        "--idempotency-key",
        spec["idempotency_key"],
        "--max-runtime",
        "30m",
        "--created-by",
        "workflow-orchestrator",
        "--json",
        "--body",
        spec["body"],
    ]
    if spec.get("branch"):
        cmd.extend(["--branch", str(spec["branch"])])
    for skill in spec["skills"]:
        cmd.extend(["--skill", skill])
    for parent in parent_ids:
        cmd.extend(["--parent", parent])

    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    payload = json.loads(result.stdout)
    task_id = payload.get("id")
    if not task_id:
        raise RuntimeError(f"Missing task id in Hermes output: {result.stdout}")
    return str(task_id)


def create_task_graph(config: ExpandConfig, specs: list[dict[str, Any]]) -> dict[str, str]:
    ref_to_id: dict[str, str] = {}
    for spec in specs:
        parent_ids = [ref_to_id.get(parent, parent) for parent in spec["parents"]]
        task_id = create_task(config, spec, parent_ids)
        ref_to_id[spec["ref"]] = task_id
    return ref_to_id


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--run-record-dir", required=True)
    parser.add_argument("--development-plan", required=True, type=Path)
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--branch-prefix", default="ai/dev-feature")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--parent-task-id")
    parser.add_argument("--expected-output", default="pull_request")
    parser.add_argument("--stop-after-commit", action="store_true")
    parser.add_argument("--worktree-path")
    parser.add_argument("--hermes-bin")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config = ExpandConfig(
        board=args.board,
        repo=args.repo,
        issue=args.issue,
        workflow_run_id=args.workflow_run_id,
        run_record_dir=args.run_record_dir,
        base_branch=args.base_branch,
        branch_prefix=args.branch_prefix,
        tenant=args.tenant,
        parent_task_id=args.parent_task_id,
        expected_output=args.expected_output,
        stop_after_commit=args.stop_after_commit,
        worktree_path=args.worktree_path,
        hermes_bin=args.hermes_bin,
    )
    plan = load_development_plan(args.development_plan)
    if not args.dry_run:
        ensure_git_worktree(config)
    specs = build_task_specs(config, plan)

    if args.dry_run:
        payload = {"dry_run": True, "tasks": specs}
    else:
        payload = {"dry_run": False, "created": create_task_graph(config, specs), "tasks": specs}

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"{'Would create' if args.dry_run else 'Created'} {len(specs)} task(s)")
        if not args.dry_run:
            for ref, task_id in payload["created"].items():
                print(f"{ref}: {task_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
