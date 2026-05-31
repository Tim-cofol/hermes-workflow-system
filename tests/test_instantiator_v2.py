from pathlib import Path

import pytest

from instantiator.hermes_client import InMemoryHermesClient
from instantiator.instantiate import InstantiationError, ProfileResolutionError, instantiate_workflow
from instantiator.state_index import WorkflowRunIndex


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PROFILES = ["orchestrator", "architect", "dev-codex", "reviewer", "shipper"]


def make_index(tmp_path: Path) -> WorkflowRunIndex:
    return WorkflowRunIndex(tmp_path / "workflow-runs.json")


def instantiate_demo(tmp_path: Path, client: InMemoryHermesClient, index: WorkflowRunIndex):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return instantiate_workflow(
        workflow_id="dev-feature-v3",
        inputs={
            "repo": str(repo),
            "issue": "123",
            "board": "edn-agent",
            "tenant": "github:tim/edn-agent",
            "expected_output": "pull_request",
        },
        source={"type": "github_issue", "repo": "tim/edn-agent", "issue": "123"},
        requested_by="tim",
        workflow_dir=ROOT / "workflows",
        profile_registry_path=ROOT / "profiles/registry.yaml",
        index=index,
        hermes=client,
    )


def test_instantiates_dev_feature_v3_as_deterministic_kanban_dag(tmp_path):
    client = InMemoryHermesClient(available_profiles=REQUIRED_PROFILES)
    index = make_index(tmp_path)

    result = instantiate_demo(tmp_path, client, index)

    assert result["instantiation_status"] == "created"
    assert result["workflow_id"] == "dev-feature-v3"
    assert result["board"] == "edn-agent"
    assert result["tenant"] == "github:tim/edn-agent"
    assert set(result["node_to_task_id"]) == {
        "run_record",
        "clarify",
        "architect_plan",
        "expand_dev_graph",
    }
    assert result["deferred_template_nodes"] == [
        "dev_impl",
        "automated_check",
        "revise_impl",
        "commit_impl",
        "pr",
    ]

    root = client.tasks[result["root_task_id"]]
    assert root["status"] == "done"
    assert root["assignee"] == "orchestrator"
    assert root["metadata"]["node_to_task_id"] == result["node_to_task_id"]

    tasks = {node_id: client.tasks[task_id] for node_id, task_id in result["node_to_task_id"].items()}
    assert tasks["run_record"]["parents"] == [result["root_task_id"]]
    assert tasks["clarify"]["parents"] == [result["node_to_task_id"]["run_record"]]
    assert tasks["architect_plan"]["parents"] == [result["node_to_task_id"]["clarify"]]
    assert tasks["expand_dev_graph"]["parents"] == [result["node_to_task_id"]["architect_plan"]]
    assert result["workflow_run_id"] in tasks["run_record"]["workspace"]["path"]

    stored = index.get_by_id(result["workflow_run_id"])
    assert stored is not None
    assert stored["root_task_id"] == result["root_task_id"]
    assert stored["node_to_task_id"] == result["node_to_task_id"]


def test_repeated_trigger_returns_existing_run_without_duplicate_tasks(tmp_path):
    client = InMemoryHermesClient(available_profiles=REQUIRED_PROFILES)
    index = make_index(tmp_path)

    first = instantiate_demo(tmp_path, client, index)
    second = instantiate_demo(tmp_path, client, index)

    assert second["workflow_run_id"] == first["workflow_run_id"]
    assert second["root_task_id"] == first["root_task_id"]
    assert second["node_to_task_id"] == first["node_to_task_id"]
    assert len(client.tasks) == 5
    assert len(client.tasks_by_idempotency_key) == 5


def test_partial_failure_resume_reuses_created_nodes_and_repairs_run(tmp_path):
    client = InMemoryHermesClient(available_profiles=REQUIRED_PROFILES, fail_after_new_tasks=4)
    index = make_index(tmp_path)

    with pytest.raises(InstantiationError):
        instantiate_demo(tmp_path, client, index)

    partial_task_ids = set(client.tasks)
    assert len(partial_task_ids) == 4
    partial_run = next(iter(index.all_runs()))
    assert partial_run["instantiation_status"] == "failed_partial"

    client.fail_after_new_tasks = None
    resumed = instantiate_demo(tmp_path, client, index)

    assert resumed["instantiation_status"] == "created"
    assert partial_task_ids.issubset(set(client.tasks))
    assert len(client.tasks) == 5
    assert index.get_by_id(resumed["workflow_run_id"])["instantiation_status"] == "created"


def test_missing_required_profile_rejects_instantiation_without_fallback(tmp_path):
    client = InMemoryHermesClient(available_profiles=["orchestrator", "dev-claude"])
    index = make_index(tmp_path)

    with pytest.raises(ProfileResolutionError) as exc:
        instantiate_demo(tmp_path, client, index)

    assert "architect" in str(exc.value)
    assert "dev-codex" in str(exc.value)
    assert not client.tasks
    assert list(index.all_runs()) == []
