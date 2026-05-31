import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/expand_hermes_dev_graph.py"


def load_expander():
    assert SCRIPT.exists(), "expand_hermes_dev_graph.py should exist"
    import importlib.util

    spec = importlib.util.spec_from_file_location("expand_hermes_dev_graph", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sample_plan():
    return {
        "development_tasks": [
            {
                "id": "task-1",
                "title": "Implement bubble_sort.py",
                "scope": "Create bubble_sort.py.",
                "files_expected": ["bubble_sort.py"],
                "check_command": "python3 -c \"from bubble_sort import bubble_sort\"",
                "acceptance_checks": ["bubble_sort.py exists"],
                "risk_notes": ["copy input before sorting"],
                "estimated_changed_lines": 20,
            },
            {
                "id": "task-2",
                "title": "Add tests",
                "scope": "Create pytest coverage.",
                "files_expected": ["tests/test_bubble_sort.py"],
                "check_command": "python3 -m pytest -q tests/test_bubble_sort.py",
                "acceptance_checks": ["six tests pass"],
                "risk_notes": ["no generated files"],
                "estimated_changed_lines": 50,
            },
        ]
    }


def test_builds_serial_dev_check_commit_graph():
    module = load_expander()
    config = module.ExpandConfig(
        board="demo",
        repo="/repo",
        issue="bubble-sort",
        workflow_run_id="run123",
        run_record_dir="/repo/.hermes/workflow-runs/run123",
        base_branch="main",
        branch_prefix="ai/dev-feature",
        tenant="default",
        parent_task_id="t_expand",
        expected_output="local_commit_only",
        stop_after_commit=True,
    )

    tasks = module.build_task_specs(config, sample_plan())

    assert [task["step"] for task in tasks] == [
        "dev_impl",
        "automated_check",
        "commit_impl",
        "dev_impl",
        "automated_check",
        "commit_impl",
    ]
    assert tasks[0]["parents"] == ["t_expand"]
    assert tasks[1]["parents"] == [tasks[0]["ref"]]
    assert tasks[2]["parents"] == [tasks[1]["ref"]]
    assert tasks[3]["parents"] == [tasks[2]["ref"]]
    assert tasks[1]["assignee"] == "reviewer"
    assert tasks[1]["skills"] == ["requesting-code-review"]
    assert tasks[4]["assignee"] == "reviewer"
    assert "__pycache__" in tasks[1]["body"]
    assert ".pyc" in tasks[4]["body"]
    assert not any(task["step"] == "pr" for task in tasks)


def test_dry_run_cli_outputs_task_graph(tmp_path):
    plan_path = tmp_path / "development_plan.json"
    plan_path.write_text(json.dumps(sample_plan()), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dry-run",
            "--json",
            "--board",
            "demo",
            "--repo",
            "/repo",
            "--issue",
            "bubble-sort",
            "--workflow-run-id",
            "run123",
            "--run-record-dir",
            "/repo/.hermes/workflow-runs/run123",
            "--development-plan",
            str(plan_path),
            "--base-branch",
            "main",
            "--branch-prefix",
            "ai/dev-feature",
            "--parent-task-id",
            "t_expand",
            "--expected-output",
            "local_commit_only",
            "--stop-after-commit",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert len(payload["tasks"]) == 6
    assert payload["tasks"][0]["idempotency_key"] == "dev-feature-v3:run123:task-1:dev_impl"


def test_ensure_git_worktree_creates_real_worktree_before_tasks(tmp_path):
    module = load_expander()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

    run_record_dir = repo / ".hermes/workflow-runs/run123"
    config = module.ExpandConfig(
        board="demo",
        repo=str(repo),
        issue="selection-sort",
        workflow_run_id="run123",
        run_record_dir=str(run_record_dir),
        base_branch="master",
        branch_prefix="ai/dev-feature",
        parent_task_id="t_expand",
        expected_output="local_commit_only",
        stop_after_commit=True,
    )

    module.ensure_git_worktree(config)

    worktree = Path(config.resolved_worktree_path)
    assert (worktree / ".git").exists()
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=worktree, text=True).strip()
    assert branch == "ai/dev-feature/issue-selection-sort"
