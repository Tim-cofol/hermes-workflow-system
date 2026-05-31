import json
from pathlib import Path

from instantiator.hermes_client import InMemoryHermesClient
from instantiator.instantiate import instantiate_workflow
from instantiator.state_index import WorkflowRunIndex
from operations.approval import ensure_approval_gate, requires_approval
from operations.catalog import WorkflowCatalog
from operations.controls import cancel_run, rerun_workflow, retry_run
from operations.metrics import compute_metrics
from operations.pr_reconciler import PullRequestStatus, reconcile_pr_merge_gates
from operations.run_history import RunHistory


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PROFILES = ["orchestrator", "architect", "dev-codex", "reviewer", "shipper"]


def create_run(tmp_path: Path, *, issue: str = "123", board: str = "edn-agent"):
    repo = tmp_path / f"repo-{issue}"
    repo.mkdir()
    client = InMemoryHermesClient(available_profiles=REQUIRED_PROFILES)
    index = WorkflowRunIndex(tmp_path / "workflow-runs.json")
    result = instantiate_workflow(
        workflow_id="dev-feature-v3",
        inputs={"repo": str(repo), "issue": issue, "board": board, "tenant": f"github:tim/{board}"},
        source={"type": "github_issue", "repo": f"tim/{board}", "issue": issue},
        requested_by="tim",
        workflow_dir=ROOT / "workflows",
        profile_registry_path=ROOT / "profiles/registry.yaml",
        index=index,
        hermes=client,
    )
    return client, index, result


def test_catalog_lists_workflows_with_versions_and_entrypoints():
    catalog = WorkflowCatalog(ROOT / "workflows")

    workflows = catalog.list_workflows()

    dev_feature = next(item for item in workflows if item["id"] == "dev-feature-v3")
    assert dev_feature["version"] == "1.3.0"
    assert dev_feature["schema_family"] == "workflow-template"
    assert "repo" in dev_feature["required_inputs"]
    assert "issue" in dev_feature["required_inputs"]


def test_run_history_filters_by_board_and_replays_node_mapping(tmp_path):
    client, index, first = create_run(tmp_path, issue="123", board="edn-agent")
    create_run(tmp_path, issue="456", board="adds")
    history = RunHistory(index=index, hermes=client)

    edn_runs = history.list_runs(board="edn-agent")
    replay = history.replay_mapping(first["workflow_run_id"])

    assert [run["workflow_run_id"] for run in edn_runs] == [first["workflow_run_id"]]
    assert replay["root_task_id"] == first["root_task_id"]
    assert replay["node_to_task_id"] == first["node_to_task_id"]
    assert history.render_dashboard_html(board="edn-agent").count("dev-feature-v3") == 1


def test_cancel_retry_and_rerun_record_operator_actions_without_runtime_status(tmp_path):
    client, index, result = create_run(tmp_path)

    cancelled = cancel_run(index=index, hermes=client, workflow_run_id=result["workflow_run_id"], reason="bad input")
    retry = retry_run(index=index, workflow_run_id=result["workflow_run_id"], requested_by="tim")
    rerun = rerun_workflow(
        index=index,
        hermes=client,
        original_run_id=result["workflow_run_id"],
        requested_by="tim",
        rerun_nonce="manual-1",
        workflow_dir=ROOT / "workflows",
        profile_registry_path=ROOT / "profiles/registry.yaml",
    )

    stored = index.get_by_id(result["workflow_run_id"])
    assert cancelled["action"] == "cancel"
    assert retry["action"] == "retry"
    assert client.tasks[result["root_task_id"]]["status"] == "blocked"
    assert stored["instantiation_status"] == "created"
    assert "workflow_runtime_status" not in stored
    assert rerun["workflow_run_id"] != result["workflow_run_id"]


def test_approval_gate_for_protected_paths_creates_manual_gate_task(tmp_path):
    client, index, result = create_run(tmp_path)

    decision = requires_approval(["src/app.py", ".env"], protected_paths=[".env", "**/secrets/**"])
    gate = ensure_approval_gate(
        index=index,
        hermes=client,
        workflow_run_id=result["workflow_run_id"],
        reason=decision.reason,
        approvers=["tim"],
    )

    assert decision.required is True
    assert gate["metadata"]["mode"] == "manual_gate"
    assert gate["metadata"]["approvers"] == ["tim"]
    assert gate["parents"] == [result["root_task_id"]]


def test_metrics_aggregate_runs_by_workflow_board_and_operator_actions(tmp_path):
    client, index, result = create_run(tmp_path, issue="123", board="edn-agent")
    create_run(tmp_path, issue="456", board="adds")
    cancel_run(index=index, hermes=client, workflow_run_id=result["workflow_run_id"], reason="duplicate")

    metrics = compute_metrics(index)

    assert metrics["total_runs"] == 2
    assert metrics["by_workflow"]["dev-feature-v3"] == 2
    assert metrics["by_board"]["edn-agent"] == 1
    assert metrics["by_board"]["adds"] == 1
    assert metrics["operator_actions"]["cancel"] == 1


def test_pr_merge_reconciler_completes_review_required_shipper_gate(tmp_path):
    run_dir = tmp_path / ".hermes/workflow-runs/wf_merged"
    run_dir.mkdir(parents=True)
    final_path = run_dir / "final.json"
    final_path.write_text(
        json.dumps(
            {
                "workflow_run_id": "wf_merged",
                "pr_number": 3,
                "pr_url": "https://github.com/Tim-cofol/adds/pull/3",
                "pr_state": "open",
                "merge_blocked_reason": "review-required",
                "merge_recommended": False,
            }
        ),
        encoding="utf-8",
    )
    client = InMemoryHermesClient(available_profiles=REQUIRED_PROFILES)
    task_id = client.create_or_reuse_task(
        title="Create PR and check CI for issue #t_req",
        assignee="shipper",
        skills=["github-pr-workflow"],
        body=(
            "workflow_id: dev-feature-v3\n"
            "step: pr\n"
            "workflow_run_id: wf_merged\n"
            f"run_record_dir: {run_dir}\n"
        ),
        idempotency_key="wf_merged:pr",
        board="adds",
        tenant="default",
    )
    client.block_task(task_id, reason="review-required: PR #3 opened")

    class StaticPrStatusProvider:
        def get_status(self, final):
            return PullRequestStatus(
                number=3,
                url=final["pr_url"],
                state="closed",
                merged=True,
                merged_at="2026-05-30T23:28:03Z",
                merge_commit_sha="da12bf79f71ee73c222b2ec42202c9a79a4e6b06",
            )

    reconciled = reconcile_pr_merge_gates(
        hermes=client,
        board="adds",
        pr_status_provider=StaticPrStatusProvider(),
        now="2026-05-31T00:00:00Z",
    )

    stored_final = json.loads(final_path.read_text(encoding="utf-8"))
    assert reconciled == [
        {
            "task_id": task_id,
            "workflow_run_id": "wf_merged",
            "pr_number": 3,
            "action": "completed",
        }
    ]
    assert client.tasks[task_id]["status"] == "done"
    assert client.tasks[task_id]["unblocked_reason"].startswith("PR #3 merged")
    assert "PR #3 merged" in client.tasks[task_id]["comments"][0]["body"]
    assert stored_final["pr_state"] == "merged"
    assert stored_final["merge_blocked_reason"] is None
    assert stored_final["merge_commit_sha"] == "da12bf79f71ee73c222b2ec42202c9a79a4e6b06"
    assert stored_final["review_gate_status"] == "satisfied"
