#!/usr/bin/env python3
"""Validate Hermes v1.3 workflow artifacts in this repo and optionally ~/.hermes."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ID = "dev-feature-v3"
REQUIRED_PROFILES = ["orchestrator", "architect", "dev-codex", "reviewer", "shipper"]
REQUIRED_BUNDLES = ["ai-architect", "ai-dev-codex", "ai-reviewer", "ai-shipper"]
PROFILE_LOCAL_SKILLS = ["workflow-orchestrator"]
REQUIRED_SCRIPTS = ["expand_hermes_dev_graph.py"]
REQUIRED_MODULE_FILES = [
    "instantiator/instantiate.py",
    "instantiator/hermes_client.py",
    "instantiator/state_index.py",
    "instantiator/validator.py",
    "instantiator/renderer.py",
    "router/app.py",
    "router/cli.py",
    "router/cron.py",
    "router/github.py",
    "router/feishu.py",
    "router/triage.py",
    "router/triage_monitor.py",
    "operations/catalog.py",
    "operations/run_history.py",
    "operations/controls.py",
    "operations/approval.py",
    "operations/metrics.py",
]
REQUIRED_NODES = [
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


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a YAML object")
    return data


def require(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(path)


def validate_tree(root: Path, *, installed: bool = False) -> list[str]:
    errors: list[str] = []

    def check(fn, *args):
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - validation report should continue.
            errors.append(str(exc))

    workflow_path = root / "workflows/dev-feature-v3.yaml"
    check(require, workflow_path)
    if workflow_path.exists():
        workflow = load_yaml(workflow_path)
        if workflow.get("id") != WORKFLOW_ID:
            errors.append(f"{workflow_path}: id is not {WORKFLOW_ID}")
        if workflow.get("version") != "1.3.0":
            errors.append(f"{workflow_path}: version is not 1.3.0")
        if workflow.get("runtime", {}).get("max_parallel_dev_workers") != 1:
            errors.append(f"{workflow_path}: max_parallel_dev_workers must be 1")
        nodes = [node.get("id") for node in workflow.get("nodes", [])]
        if nodes != REQUIRED_NODES:
            errors.append(f"{workflow_path}: unexpected nodes {nodes}")

    for profile in REQUIRED_PROFILES:
        check(require, root / f"profiles/{profile}/profile.yaml")
    for bundle in REQUIRED_BUNDLES:
        check(require, root / f"skill-bundles/{bundle}.yaml")
    for script in REQUIRED_SCRIPTS:
        check(require, root / f"scripts/{script}")
    module_root = root / "workflow-system" if installed else root
    for module_file in REQUIRED_MODULE_FILES:
        check(require, module_root / module_file)
    check(require, root / "skills/workflow-orchestrator/SKILL.md")
    check(require, root / "skills/workflow-orchestrator/references/workflows/dev-feature-v3.yaml")
    reference_workflow_path = root / "skills/workflow-orchestrator/references/workflows/dev-feature-v3.yaml"
    if workflow_path.exists() and reference_workflow_path.exists():
        if workflow_path.read_text(encoding="utf-8") != reference_workflow_path.read_text(encoding="utf-8"):
            errors.append(f"{reference_workflow_path}: must match {workflow_path}")
    if installed:
        for profile in REQUIRED_PROFILES:
            for skill in PROFILE_LOCAL_SKILLS:
                check(require, root / f"profiles/{profile}/skills/{skill}/SKILL.md")
                check(require, root / f"profiles/{profile}/skills/{skill}/references/workflows/dev-feature-v3.yaml")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed", action="store_true", help="also validate installed HERMES_HOME artifacts")
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    args = parser.parse_args()

    checks = [(ROOT, "repo", False)]
    if args.installed:
        checks.append((args.hermes_home, str(args.hermes_home), True))

    failed = False
    for root, label, installed in checks:
        errors = validate_tree(root, installed=installed)
        if errors:
            failed = True
            print(f"[FAIL] {label}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"[OK] {label}: {WORKFLOW_ID} artifacts are valid")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
