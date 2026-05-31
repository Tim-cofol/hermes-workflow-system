from pathlib import Path

from instantiator.hermes_client import InMemoryHermesClient
from instantiator.state_index import WorkflowRunIndex
from router.app import WorkflowRouterApp
from router.cli import parse_wf_command, run_wf_command
from router.feishu import route_feishu_command
from router.github import route_github_label_event
from router.cron import route_cron_trigger
from router.triage import route_kanban_triage_task
from router.triage_monitor import route_triage_tasks_once, run_triage_monitor_loop


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PROFILES = ["orchestrator", "architect", "dev-codex", "reviewer", "shipper"]


def make_app(tmp_path: Path) -> WorkflowRouterApp:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = InMemoryHermesClient(available_profiles=REQUIRED_PROFILES)
    index = WorkflowRunIndex(tmp_path / "workflow-runs.json")
    return WorkflowRouterApp(
        workflow_dir=ROOT / "workflows",
        profile_registry_path=ROOT / "profiles/registry.yaml",
        index=index,
        hermes=client,
        aliases={"dev-feature": "dev-feature-v3"},
        default_repo=str(repo),
    )


def test_cli_wf_command_parses_alias_and_key_value_inputs():
    command = "/wf dev-feature repo=/repo issue=123 board=edn-agent expected_output=local_commit_only"

    request = parse_wf_command(command, aliases={"dev-feature": "dev-feature-v3"})

    assert request.workflow_id == "dev-feature-v3"
    assert request.inputs["repo"] == "/repo"
    assert request.inputs["issue"] == "123"
    assert request.inputs["board"] == "edn-agent"
    assert request.inputs["expected_output"] == "local_commit_only"
    assert request.source["type"] == "cli"


def test_cli_route_creates_run_through_instantiator(tmp_path):
    app = make_app(tmp_path)

    result = run_wf_command("/wf dev-feature issue=123 board=edn-agent", app=app, requested_by="tim")

    assert result["workflow_id"] == "dev-feature-v3"
    assert result["instantiation_status"] == "created"
    assert result["board"] == "edn-agent"


def test_github_label_event_routes_workflow_label_to_same_instantiator(tmp_path):
    app = make_app(tmp_path)
    payload = {
        "action": "labeled",
        "repository": {"full_name": "tim/edn-agent"},
        "issue": {"number": 123},
        "label": {"name": "workflow:dev-feature"},
        "sender": {"login": "tim"},
    }

    result = route_github_label_event(payload, app=app)

    assert result["workflow_id"] == "dev-feature-v3"
    assert result["source"]["type"] == "github_issue"
    assert result["inputs"]["issue"] == "123"


def test_feishu_command_routes_wf_text(tmp_path):
    app = make_app(tmp_path)

    result = route_feishu_command("@Hermes /wf dev-feature issue=abc board=adds", app=app, requested_by="tim")

    assert result["workflow_id"] == "dev-feature-v3"
    assert result["source"]["type"] == "feishu"
    assert result["board"] == "adds"


def test_http_style_run_creation_keeps_router_out_of_runtime_state(tmp_path):
    app = make_app(tmp_path)

    result = app.create_run(
        "dev-feature-v3",
        {
            "inputs": {"issue": "456", "board": "adds"},
            "source": {"type": "web_ui", "project": "adds", "issue": "456"},
            "requested_by": "tim",
        },
    )

    assert result["workflow_id"] == "dev-feature-v3"
    assert result["source"]["type"] == "web_ui"
    assert "workflow_runtime_status" not in result


def test_cron_trigger_routes_scheduled_workflow(tmp_path):
    app = make_app(tmp_path)

    result = route_cron_trigger(
        "dev-feature",
        app=app,
        schedule_id="nightly-bubble-sort",
        inputs={"issue": "cron-bubble-sort", "board": "adds"},
    )

    assert result["workflow_id"] == "dev-feature-v3"
    assert result["source"]["type"] == "cron"
    assert result["source"]["schedule_id"] == "nightly-bubble-sort"


def test_dashboard_triage_task_routes_bubble_sort_requirement(tmp_path):
    app = make_app(tmp_path)
    triage_task = {
        "id": "triage-bubble-sort",
        "title": "Implement bubble sort",
        "body": "Create a simple bubble_sort function with tests.",
        "status": "triage",
        "board": "adds",
        "tenant": "default",
    }

    result = route_kanban_triage_task(triage_task, app=app, workflow_alias="dev-feature", requested_by="dashboard")

    assert result["workflow_id"] == "dev-feature-v3"
    assert result["source"]["type"] == "hermes_dashboard_triage"
    assert result["source"]["task_id"] == "triage-bubble-sort"
    assert result["inputs"]["issue"] == "triage-bubble-sort"
    assert "bubble_sort" in result["inputs"]["requirement_text"]


def test_dashboard_triage_monitor_routes_all_triage_cards_once(tmp_path):
    app = make_app(tmp_path)
    triage_tasks = [
        {
            "id": "triage-bubble-sort",
            "title": "Implement bubble sort",
            "body": "Create bubble_sort.py with tests.",
            "status": "triage",
            "board": "adds",
        },
        {
            "id": "triage-selection-sort",
            "title": "Implement selection sort",
            "body": "Create selection_sort.py with tests.",
            "status": "triage",
            "board": "adds",
        },
    ]

    first = route_triage_tasks_once(tasks=triage_tasks, app=app, workflow_alias="dev-feature")
    second = route_triage_tasks_once(tasks=triage_tasks, app=app, workflow_alias="dev-feature")

    assert [run["source"]["task_id"] for run in first] == ["triage-bubble-sort", "triage-selection-sort"]
    assert [run["workflow_run_id"] for run in second] == [run["workflow_run_id"] for run in first]
    assert len(app.hermes.tasks) == 10


def test_dashboard_triage_monitor_loop_polls_for_new_cards(tmp_path):
    app = make_app(tmp_path)

    class FakeTriageSource:
        def __init__(self):
            self.calls = 0
            self.acknowledged = []

        def list_triage_tasks(self, *, board: str):
            self.calls += 1
            if self.calls == 1:
                return []
            return [
                {
                    "id": "triage-selection-sort",
                    "title": "设计选择排序的算法",
                    "body": "请实现 selection_sort 并完成自动验收。",
                    "status": "triage",
                    "board": board,
                }
            ]

        def acknowledge_task(self, task, result):
            self.acknowledged.append((task["id"], result["workflow_run_id"]))

    source = FakeTriageSource()

    routed = run_triage_monitor_loop(
        app=app,
        triage_source=source,
        board="adds",
        workflow_alias="dev-feature",
        interval_seconds=0,
        max_iterations=2,
    )

    assert source.calls == 2
    assert len(routed) == 1
    assert source.acknowledged == [("triage-selection-sort", routed[0]["workflow_run_id"])]
    assert routed[0]["inputs"]["issue"] == "triage-selection-sort"
    assert routed[0]["deferred_template_nodes"] == [
        "dev_impl",
        "automated_check",
        "revise_impl",
        "commit_impl",
        "pr",
    ]


def test_dashboard_triage_monitor_loop_runs_pr_reconciler_each_iteration(tmp_path):
    app = make_app(tmp_path)

    class EmptyTriageSource:
        def list_triage_tasks(self, *, board: str):
            return []

    class FakePrReconciler:
        def __init__(self):
            self.boards = []

        def reconcile_board(self, board: str):
            self.boards.append(board)
            return []

    reconciler = FakePrReconciler()

    routed = run_triage_monitor_loop(
        app=app,
        triage_source=EmptyTriageSource(),
        board="adds",
        workflow_alias="dev-feature",
        interval_seconds=0,
        max_iterations=2,
        pr_reconciler=reconciler,
    )

    assert routed == []
    assert reconciler.boards == ["adds", "adds"]
