---
name: workflow-orchestrator
description: Expand dev-feature-v3 workflow templates into Hermes Kanban DAG tasks.
version: 1.3.0
---

# Workflow Orchestrator

## Purpose

Expand root workflow tasks into deterministic Hermes Kanban task graphs. Hermes Kanban remains the only runtime state machine.

## Trigger

Use this skill when the current Kanban task body contains:

- `workflow_id: dev-feature-v3`
- `repo`
- `issue` or equivalent local requirement id
- `expected_output`

Also use it for continuation tasks whose body contains `step: expand_dev_graph`.

## Procedure

1. Call `kanban_show()` first.
2. Parse root inputs and load `references/workflows/dev-feature-v3.yaml`.
3. Validate required profiles: `orchestrator`, `architect`, `dev-codex`, `reviewer`, `shipper`.
4. Create or reuse the root run mapping by idempotency key.
5. Create the fixed front of the DAG:
   `run_record -> clarify -> architect_plan -> expand_dev_graph`.
6. Complete the root task only after all fixed nodes and links are recorded.
7. When running `expand_dev_graph`, call the deterministic expander:
   `python3 ~/.hermes/scripts/expand_hermes_dev_graph.py ...`.
   Pass the current task id as `--parent-task-id`, the board, repo, issue,
   workflow run id, run record dir, development plan path, base branch, branch
   prefix, tenant, expected output, and `--stop-after-commit` when present.
8. The expander reads `development_plan.json` and its `development_tasks` array,
   then creates each serial subgraph:
   `dev_impl -> automated_check -> commit_impl`.
9. Link each plan item after the previous plan item's `commit_impl`, so only one `dev-codex` worker runs at a time.
10. If `automated_check` fails, create `revise_impl -> automated_check` for the same `plan_item_id` until PASS or retry budget is exhausted.
11. Link `pr` after the last `commit_impl` unless `expected_output` is `local_commit_only` or `stop_after_commit` is true.

## Dynamic Task Routing

When expanding development tasks, use these exact routes:

- `dev_impl`: assignee `dev-codex`; skills `codex`, `kanban-codex-lane`, `test-driven-development`; workspace `worktree`.
- `automated_check`: assignee `reviewer`; skills `requesting-code-review`; run checks against the same worktree path and write durable check output under the run record dir.
- `revise_impl`: assignee `dev-codex`; skills `codex`, `kanban-codex-lane`, `test-driven-development`; workspace `worktree`.
- `commit_impl`: assignee `dev-codex`; skills `codex`, `kanban-codex-lane`; workspace `worktree`; commit only after `automated_check` returns PASS.

Every automated check must include a source hygiene check that no generated cache
files are tracked by git, at minimum `*.pyc` and any `__pycache__/` path.

Do not hand-create dynamic dev/check/commit tasks unless the expander script is
missing or fails. If fallback is required, block with the failure reason after
one retry instead of improvising a different topology.

## Rules

- Do not write production code.
- Do not review code.
- Do not create PRs.
- Do not create deprecated v1.2 parallel-lane nodes.
- Use `dir` workspace for run records and architect output.
- Use `scratch` only for temporary checks; durable reports must be copied to the run record dir.
- Use `worktree` for all code changes, revisions, and commits.
- Keep `dev-codex` serial with `max_in_progress_per_profile: 1`.
- Block the root task with an explicit reason if the template, required inputs, or required profiles are missing.
- For smoke runs with `local_commit_only`, stop at the final `commit_impl` task.

## Completion Metadata

Complete orchestration steps with structured metadata:

```json
{
  "workflow_id": "dev-feature-v3",
  "workflow_version": "1.3.0",
  "node_to_task_id": {},
  "run_record_dir": "",
  "expanded_plan_items": []
}
```
