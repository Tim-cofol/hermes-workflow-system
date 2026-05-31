# Hermes v1.3 Implementation Handoff

This handoff exists because the context window may be close to full. Treat this file and `hermes_workflow_kanban_design_instruction.md` as the durable source for the next agent.

## Current Decision Set

- Workflow id: `dev-feature-v3`.
- Required profiles: `orchestrator`, `architect`, `dev-codex`, `reviewer`, `shipper`.
- Development is serial: `kanban.max_in_progress_per_profile` must be `1`.
- Dashboard triage intake owns fixed workflow routing: `kanban.auto_decompose` is set to `false` during install so Hermes native triage decomposition does not race the workflow router.
- Default human intervention happens only during clarify, unless protected paths, repeated automated failures, or external permissions require escalation.
- `architect` creates `architecture_plan.md` and `development_plan.json`; each development task should be about 1000 changed lines or less.
- `dev-codex` implements one plan item at a time and does not commit until `automated_check` passes.
- V2 Router / Instantiator is now implemented as deterministic Python modules. Router adapters normalize CLI, GitHub label, Feishu command, and HTTP-style payloads into the same instantiation call.
- V3 Engineering Operations is implemented as catalog/history/dashboard helpers plus operator controls for cancel, retry, rerun, approval gates, metrics, and PR review-gate reconciliation. These helpers record mappings and operator actions, but do not create a second workflow runtime state machine.

## Artifact Map

- `workflows/dev-feature-v3.yaml`: source workflow template.
- `skill-bundles/*.yaml`: profile skill bundles for Hermes.
- `profiles/*/profile.yaml`: local Hermes profile descriptions.
- `skills/workflow-orchestrator/SKILL.md`: orchestration rules and dynamic serial expansion behavior.
- `scripts/expand_hermes_dev_graph.py`: deterministic expander for `development_plan.json` into serial `dev/check/commit` Kanban tasks. It creates the real git worktree before creating worker tasks so commit tasks do not operate inside a plain ignored directory.
- `instantiator/instantiate.py`: V2 deterministic compiler from workflow YAML to Hermes Kanban DAG.
- `instantiator/hermes_client.py`: injectable Hermes client, including `InMemoryHermesClient` for tests and `CliHermesClient` for local use.
- `instantiator/state_index.py`: JSON workflow run index for instantiation transactions and node/task mappings.
- `router/app.py`: shared Router facade used by CLI, webhook, Feishu, and HTTP-style entrypoints.
- `router/cli.py`: `/wf` command parser and runner.
- `router/cron.py`: scheduled workflow adapter for Cron-triggered runs.
- `router/github.py`: GitHub `workflow:*` label adapter.
- `router/feishu.py`: Feishu `/wf` command adapter.
- `router/triage.py`: Hermes dashboard triage-card adapter for routing a new Kanban triage requirement into the same workflow path.
- `router/triage_monitor.py`: one-pass or `--watch` monitor that scans dashboard triage cards and routes them through the workflow adapter; after routing it comments on and archives the source triage card.
- `operations/catalog.py`: Workflow Catalog with version and required-input listing.
- `operations/run_history.py`: Run History Dashboard and node/task mapping replay.
- `operations/controls.py`: cancel / retry / rerun operator actions.
- `operations/approval.py`: protected-path approval gate policy and manual gate task creation.
- `operations/metrics.py`: metrics derived from the workflow run index.
- `operations/pr_reconciler.py`: GitHub PR merge-state reconciler. It finds blocked `shipper` PR gates, reads their `final.json`, checks GitHub, and when the PR is merged it writes the merged state back, comments, unblocks, and completes the gate.
- `scripts/install_hermes_v13_artifacts.py`: copies artifacts into `~/.hermes` and sets the serial worker cap.
- `scripts/install_hermes_triage_monitor_service.py`: installs a systemd user service for continuous dashboard triage intake on a selected board.
- `scripts/validate_hermes_v13_artifacts.py`: validates repo and installed artifacts.

## Verification Commands

```bash
python3 -m pytest -s tests/test_hermes_v13_artifacts.py -q
python3 -m pytest -s tests/test_expand_hermes_dev_graph.py -q
python3 -m pytest -s tests/test_instantiator_v2.py -q
python3 -m pytest -s tests/test_router_v2_v3.py -q
python3 -m pytest -s tests/test_operations_v3.py -q
python3 scripts/validate_hermes_v13_artifacts.py
python3 scripts/validate_hermes_v13_artifacts.py --installed
```

## V2 / V3 Operating Notes

- Use `python3 -m instantiator.instantiate --workflow dev-feature-v3 --repo <repo> --issue <issue> --board <board>` for a local deterministic run creation path.
- Use Router adapters when the trigger source is `/wf`, GitHub label, Feishu command, or a Web UI payload; the Router should only normalize and call the Instantiator.
- Use `operations.run_history.RunHistory` to view all workflow runs, filter by project board, and replay each run's `node_to_task_id` mapping.
- Use `operations.controls.cancel_run`, `retry_run`, and `rerun_workflow` for operator actions. These append audit actions and call Hermes task APIs where appropriate, without writing a separate runtime status.
- Use `operations.approval.requires_approval` and `ensure_approval_gate` for protected paths such as `.env` or secrets.
- Dashboard triage intake: install with `python3 scripts/install_hermes_triage_monitor_service.py --board <board> --repo <repo> --enable-now`. The service runs `router.triage_monitor --watch`, routes triage cards into `dev-feature-v3`, comments with the workflow run/root ids, archives the source card, and runs PR merge reconciliation each polling cycle.
- Bubble-sort or selection-sort smoke scenario: create a clear Kanban triage card in the monitored board. The expected flow is source card archived -> workflow root done -> `run_record / clarify / architect_plan / expand_dev_graph` done -> dynamic `dev_impl / automated_check / commit_impl` tasks done. Shipper may block at `review-required` if PR review is required or CI is not configured.
- Verified selection-sort E2E on board `selection-sort-e2e`: source card `t_0df2a26d` routed to workflow `wf_a700b0d75984`; dev/check/commit tasks completed; PR #3 was opened, entered `review-required`, then the PR merge reconciler observed the GitHub merge and completed the `shipper` gate.
