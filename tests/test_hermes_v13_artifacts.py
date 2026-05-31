from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: str):
    with (ROOT / path).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_dev_feature_v3_workflow_shape():
    workflow = load_yaml("workflows/dev-feature-v3.yaml")

    assert workflow["id"] == "dev-feature-v3"
    assert workflow["version"] == "1.3.0"
    assert workflow["runtime"]["max_parallel_dev_workers"] == 1

    node_ids = [node["id"] for node in workflow["nodes"]]
    assert node_ids == [
        "run_record",
        "clarify",
        "architect_plan",
        "expand_dev_graph",
        "dev_impl",
        "automated_check",
        "revise_impl",
        "commit_impl",
        "pr",
    ]
    assert "lane_a_impl" not in node_ids
    assert "lane_b_impl" not in node_ids
    assert "integrate_impl" not in node_ids


def test_architect_and_serial_dev_contracts():
    workflow = load_yaml("workflows/dev-feature-v3.yaml")
    nodes = {node["id"]: node for node in workflow["nodes"]}

    assert nodes["architect_plan"]["assignee"] == "architect"
    assert nodes["architect_plan"]["parents"] == ["clarify"]
    assert nodes["architect_plan"]["workspace"]["type"] == "dir"

    expander = nodes["expand_dev_graph"]
    assert expander["assignee"] == "orchestrator"
    assert expander["parents"] == ["architect_plan"]
    assert "development_plan.json" in expander["body"]
    assert "expand_hermes_dev_graph.py" in expander["body"]

    dev = nodes["dev_impl"]
    assert dev["assignee"] == "dev-codex"
    assert dev["mode"] == "dynamic_serial_template"
    assert dev["expand_from"] == "architect_plan.development_tasks"
    assert dev["serial_group"] == "development_tasks"
    assert dev["parents"][0] == "expand_dev_graph"
    assert dev["workspace"]["type"] == "worktree"
    assert dev["workspace"]["branch"] == "{branch_prefix}/issue-{issue}"

    check = nodes["automated_check"]
    assert check["parents"] == ["dev_impl"]
    assert check["workspace"]["type"] == "scratch"
    assert check["check_policy"]["fail_behavior"]["create_revise_task"] is True

    commit = nodes["commit_impl"]
    assert commit["parents"] == ["automated_check"]
    assert "automated_check.check_result is PASS" in commit["body"]


def test_profiles_and_bundles_are_present_and_codex_serial():
    expected_profiles = {
        "orchestrator",
        "architect",
        "dev-codex",
        "reviewer",
        "shipper",
    }
    for name in expected_profiles:
        profile = load_yaml(f"profiles/{name}/profile.yaml")
        assert profile["description"]

    dev_bundle = load_yaml("skill-bundles/ai-dev-codex.yaml")
    assert dev_bundle["name"] == "ai-dev-codex"
    assert "codex" in dev_bundle["skills"]
    assert "serial implementation worker" in dev_bundle["description"]
    assert "two" not in dev_bundle["instruction"].lower()

    architect_bundle = load_yaml("skill-bundles/ai-architect.yaml")
    assert architect_bundle["name"] == "ai-architect"
    assert "1000 changed lines" in architect_bundle["instruction"]


def test_workflow_orchestrator_skill_references_v3_only():
    skill = (ROOT / "skills/workflow-orchestrator/SKILL.md").read_text(encoding="utf-8")
    assert "dev-feature-v3" in skill
    assert "architect_plan" in skill
    assert "development_tasks" in skill
    assert "automated_check" in skill
    assert "assignee `reviewer`" in skill
    assert "__pycache__" in skill
    assert "lane_a_impl" not in skill
    assert "dev-feature-v2" not in skill


def test_install_validate_scripts_and_handoff_exist():
    installer = (ROOT / "scripts/install_hermes_v13_artifacts.py").read_text(encoding="utf-8")
    validator = (ROOT / "scripts/validate_hermes_v13_artifacts.py").read_text(encoding="utf-8")
    handoff = (ROOT / "docs/hermes-v13-implementation-handoff.md").read_text(encoding="utf-8")

    assert "dev-feature-v3" in installer
    assert "max_in_progress_per_profile" in installer
    assert "auto_decompose" in installer
    assert "workflow-orchestrator" in installer
    assert 'profiles" / profile / "skills"' in installer
    assert "expand_hermes_dev_graph.py" in installer
    assert "dev-feature-v3" in validator
    assert "PROFILE_LOCAL_SKILLS" in validator
    assert "REQUIRED_SCRIPTS" in validator
    assert "REQUIRED_MODULE_FILES" in validator
    assert "architect" in validator
    assert "instantiator/instantiate.py" in validator
    assert "router/app.py" in validator
    assert "router/cron.py" in validator
    assert "router/triage_monitor.py" in validator
    assert "operations/metrics.py" in validator
    assert "PACKAGE_DIRS" in installer
    assert "workflow-system" in installer
    triage_service_installer = (
        ROOT / "scripts/install_hermes_triage_monitor_service.py"
    ).read_text(encoding="utf-8")
    assert "router.triage_monitor" in triage_service_installer
    assert "systemctl --user" in triage_service_installer
    assert "--watch" in triage_service_installer
    assert (ROOT / "scripts/expand_hermes_dev_graph.py").exists()
    assert "context window" in handoff.lower()
    assert "dev-feature-v3" in handoff
    assert "expand_hermes_dev_graph.py" in handoff
    assert "V2 Router / Instantiator" in handoff
    assert "V3 Engineering Operations" in handoff
    assert "tests/test_operations_v3.py" in handoff


def test_workflow_orchestrator_reference_matches_source_workflow():
    source = (ROOT / "workflows/dev-feature-v3.yaml").read_text(encoding="utf-8")
    reference = (
        ROOT / "skills/workflow-orchestrator/references/workflows/dev-feature-v3.yaml"
    ).read_text(encoding="utf-8")
    assert reference == source
