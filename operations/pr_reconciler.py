"""Reconcile human PR review gates back into Hermes Kanban."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib import error, parse, request

from instantiator.hermes_client import CliHermesClient, HermesClientError
from instantiator.state_index import utc_now


@dataclass(frozen=True)
class PullRequestStatus:
    number: int
    url: str
    state: str
    merged: bool
    merged_at: str | None = None
    merge_commit_sha: str | None = None


def parse_github_pr_url(url: str) -> tuple[str, str, int]:
    parsed = parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc in {"github.com", "www.github.com"} and len(parts) >= 4 and parts[2] == "pull":
        return parts[0], parts[1], int(parts[3])
    if parsed.netloc == "api.github.com" and len(parts) >= 5 and parts[0] == "repos" and parts[3] == "pulls":
        return parts[1], parts[2], int(parts[4])
    raise ValueError(f"Unsupported GitHub PR URL: {url}")


class GitHubPullRequestStatusProvider:
    """Small GitHub REST API client for PR merge status."""

    def __init__(self, *, token: str | None = None, timeout_seconds: float = 20.0) -> None:
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.timeout_seconds = timeout_seconds

    def get_status(self, final: dict[str, Any]) -> PullRequestStatus:
        pr_url = str(final.get("pr_url") or "")
        if not pr_url:
            raise ValueError("final.json does not contain pr_url")
        owner, repo, number = parse_github_pr_url(pr_url)
        api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "hermes-pr-reconciler",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = request.Request(api_url, headers=headers)
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise HermesClientError(f"GitHub PR status request failed: HTTP {exc.code}: {detail}") from exc
        return PullRequestStatus(
            number=int(payload.get("number") or number),
            url=str(payload.get("html_url") or pr_url),
            state=str(payload.get("state") or "unknown"),
            merged=bool(payload.get("merged")),
            merged_at=payload.get("merged_at"),
            merge_commit_sha=payload.get("merge_commit_sha"),
        )


def parse_task_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key and all(char.isalnum() or char == "_" for char in key):
            fields[key] = value.strip()
    return fields


def final_path_for_task(task: dict[str, Any]) -> tuple[Path | None, dict[str, str]]:
    fields = parse_task_fields(str(task.get("body") or ""))
    run_record_dir = fields.get("run_record_dir") or task.get("workspace_path")
    if not run_record_dir:
        return None, fields
    return Path(str(run_record_dir)) / "final.json", fields


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def save_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp_path.replace(path)


def merged_final_payload(final: dict[str, Any], status: PullRequestStatus, *, now: str) -> dict[str, Any]:
    merged = dict(final)
    merged["pr_state"] = "merged"
    merged["merged"] = True
    merged["merged_at"] = status.merged_at
    merged["merge_commit_sha"] = status.merge_commit_sha
    merged["merge_recommended"] = True
    merged["merge_blocked_reason"] = None
    merged["review_gate_status"] = "satisfied"
    merged["review_gate_completed_at"] = now
    merged["review_gate_completed_by"] = "workflow-pr-reconciler"
    return merged


def short_sha(sha: str | None) -> str:
    return sha[:12] if sha else "unknown"


def reconcile_pr_merge_gates(
    *,
    hermes: Any,
    board: str,
    pr_status_provider: Any,
    now: str | None = None,
    author: str = "workflow-pr-reconciler",
) -> list[dict[str, Any]]:
    reconciled: list[dict[str, Any]] = []
    completed_at = now or utc_now()
    tasks = hermes.list_tasks(board=board, status="blocked", assignee="shipper")
    for task in tasks:
        final_path, fields = final_path_for_task(task)
        if fields.get("step") != "pr" or final_path is None or not final_path.exists():
            continue
        final = load_json(final_path)
        if not final.get("pr_url"):
            continue
        status = pr_status_provider.get_status(final)
        if not status.merged:
            continue

        updated_final = merged_final_payload(final, status, now=completed_at)
        save_json(final_path, updated_final)

        task_id = str(task["id"])
        workflow_run_id = str(fields.get("workflow_run_id") or final.get("workflow_run_id") or "")
        summary = (
            f"PR #{status.number} merged at {status.merged_at or 'unknown time'} "
            f"as {short_sha(status.merge_commit_sha)}; review-required gate satisfied."
        )
        metadata = {
            "workflow_run_id": workflow_run_id,
            "pr_number": status.number,
            "pr_url": status.url,
            "pr_state": "merged",
            "merged_at": status.merged_at,
            "merge_commit_sha": status.merge_commit_sha,
            "final_json": str(final_path),
        }
        hermes.comment_task(
            task_id,
            (
                f"PR #{status.number} merged on GitHub.\n\n"
                f"- merged_at: {status.merged_at or 'unknown'}\n"
                f"- merge_commit: {status.merge_commit_sha or 'unknown'}\n"
                f"- final.json updated: {final_path}\n"
                "- action: unblocked and completed the shipper review gate"
            ),
            author=author,
        )
        hermes.unblock_task(task_id, reason=summary)
        hermes.complete_task(task_id, summary=summary, metadata=metadata)
        reconciled.append(
            {
                "task_id": task_id,
                "workflow_run_id": workflow_run_id,
                "pr_number": status.number,
                "action": "completed",
            }
        )
    return reconciled


class PrMergeReconciler:
    def __init__(self, *, hermes: Any, pr_status_provider: Any | None = None) -> None:
        self.hermes = hermes
        self.pr_status_provider = pr_status_provider or GitHubPullRequestStatusProvider()

    def reconcile_board(self, board: str) -> list[dict[str, Any]]:
        return reconcile_pr_merge_gates(
            hermes=self.hermes,
            board=board,
            pr_status_provider=self.pr_status_provider,
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", required=True)
    parser.add_argument("--hermes-bin", default=str(Path.home() / ".local/bin/hermes"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    reconciler = PrMergeReconciler(hermes=CliHermesClient(args.hermes_bin))
    print(json.dumps(reconciler.reconcile_board(args.board), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
