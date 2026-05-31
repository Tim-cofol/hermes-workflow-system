#!/usr/bin/env python3
"""Install Hermes v1.3 workflow artifacts into HERMES_HOME."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ID = "dev-feature-v3"
PROFILE_NAMES = ["orchestrator", "architect", "dev-codex", "reviewer", "shipper"]
BUNDLE_NAMES = ["ai-architect", "ai-dev-codex", "ai-reviewer", "ai-shipper"]
PROFILE_LOCAL_SKILLS = ["workflow-orchestrator"]
SCRIPT_NAMES = ["expand_hermes_dev_graph.py"]
PACKAGE_DIRS = ["instantiator", "router", "operations"]


def copy_file(src: Path, dst: Path, *, dry_run: bool) -> None:
    print(f"{'DRY ' if dry_run else ''}copy {src.relative_to(ROOT)} -> {dst}")
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path, *, dry_run: bool) -> None:
    print(f"{'DRY ' if dry_run else ''}copy tree {src.relative_to(ROOT)} -> {dst}")
    if dry_run:
        return
    shutil.copytree(
        src,
        dst,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def update_config(hermes_home: Path, *, dry_run: bool) -> None:
    config_path = hermes_home / "config.yaml"
    print(
        f"{'DRY ' if dry_run else ''}set kanban.max_in_progress_per_profile=1 "
        f"and kanban.auto_decompose=false in {config_path}"
    )
    if dry_run:
        return

    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(config_path, config_path.with_suffix(f".yaml.bak.v13.{stamp}"))
    else:
        config = {}

    if not isinstance(config, dict):
        raise ValueError(f"{config_path} is not a YAML object")
    kanban = config.setdefault("kanban", {})
    if not isinstance(kanban, dict):
        raise ValueError(f"{config_path}: kanban must be a YAML object")
    kanban["max_in_progress_per_profile"] = 1
    kanban["auto_decompose"] = False

    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)


def ensure_profile_runtime(hermes_home: Path, profile: str, *, dry_run: bool) -> None:
    profile_dir = hermes_home / "profiles" / profile
    print(f"{'DRY ' if dry_run else ''}ensure profile runtime {profile_dir}")
    if dry_run:
        return
    profile_dir.mkdir(parents=True, exist_ok=True)
    for name in ["config.yaml", ".env", "SOUL.md"]:
        src = hermes_home / name
        dst = profile_dir / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)


def install_profile_local_skill(hermes_home: Path, profile: str, skill: str, *, dry_run: bool) -> None:
    profile_skill_dir = hermes_home / "profiles" / profile / "skills" / skill
    copy_file(
        ROOT / f"skills/{skill}/SKILL.md",
        profile_skill_dir / "SKILL.md",
        dry_run=dry_run,
    )
    copy_file(
        ROOT / "workflows/dev-feature-v3.yaml",
        profile_skill_dir / "references/workflows/dev-feature-v3.yaml",
        dry_run=dry_run,
    )


def install(hermes_home: Path, *, dry_run: bool) -> None:
    copy_file(ROOT / "workflows/dev-feature-v3.yaml", hermes_home / "workflows/dev-feature-v3.yaml", dry_run=dry_run)
    copy_file(
        ROOT / "workflows/dev-feature-v3.yaml",
        hermes_home / "skills/workflow-orchestrator/references/workflows/dev-feature-v3.yaml",
        dry_run=dry_run,
    )
    copy_file(
        ROOT / "skills/workflow-orchestrator/SKILL.md",
        hermes_home / "skills/workflow-orchestrator/SKILL.md",
        dry_run=dry_run,
    )
    for script in SCRIPT_NAMES:
        copy_file(ROOT / f"scripts/{script}", hermes_home / f"scripts/{script}", dry_run=dry_run)
    for package in PACKAGE_DIRS:
        copy_tree(ROOT / package, hermes_home / "workflow-system" / package, dry_run=dry_run)
    for bundle in BUNDLE_NAMES:
        copy_file(ROOT / f"skill-bundles/{bundle}.yaml", hermes_home / f"skill-bundles/{bundle}.yaml", dry_run=dry_run)
    for profile in PROFILE_NAMES:
        ensure_profile_runtime(hermes_home, profile, dry_run=dry_run)
        copy_file(
            ROOT / f"profiles/{profile}/profile.yaml",
            hermes_home / f"profiles/{profile}/profile.yaml",
            dry_run=dry_run,
        )
        for skill in PROFILE_LOCAL_SKILLS:
            install_profile_local_skill(hermes_home, profile, skill, dry_run=dry_run)
    update_config(hermes_home, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    install(args.hermes_home, dry_run=args.dry_run)
    print(f"{WORKFLOW_ID} artifacts {'would be ' if args.dry_run else ''}installed in {args.hermes_home}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
