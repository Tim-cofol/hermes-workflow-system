#!/usr/bin/env python3
"""Install a systemd user service for dashboard triage workflow intake."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HERMES_HOME = Path.home() / ".hermes"
DEFAULT_HERMES_BIN = Path.home() / ".local/bin/hermes"


def service_text(
    *,
    service_name: str,
    hermes_home: Path,
    repo: Path,
    board: str,
    workflow: str,
    interval_seconds: float,
    hermes_bin: Path,
) -> str:
    workflow_system = hermes_home / "workflow-system"
    index_path = repo / ".hermes/workflow-runs/triage-monitor-index.json"
    return f"""[Unit]
Description=Hermes dashboard triage workflow intake ({board})
After=default.target

[Service]
Type=simple
WorkingDirectory={repo}
Environment=PYTHONPATH={workflow_system}
ExecStart={sys.executable} -m router.triage_monitor --watch --board {board} --repo {repo} --workflow {workflow} --workflow-dir {repo / 'workflows'} --profile-registry {repo / 'profiles/registry.yaml'} --index {index_path} --hermes-bin {hermes_bin} --interval-seconds {interval_seconds}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def install_service(
    *,
    service_name: str,
    hermes_home: Path,
    repo: Path,
    board: str,
    workflow: str,
    interval_seconds: float,
    hermes_bin: Path,
    enable_now: bool,
    dry_run: bool,
) -> Path:
    service_path = Path.home() / ".config/systemd/user" / f"{service_name}.service"
    text = service_text(
        service_name=service_name,
        hermes_home=hermes_home,
        repo=repo,
        board=board,
        workflow=workflow,
        interval_seconds=interval_seconds,
        hermes_bin=hermes_bin,
    )
    print(f"{'DRY ' if dry_run else ''}write {service_path}")
    if dry_run:
        print(text)
        return service_path
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(text, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if enable_now:
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{service_name}.service"], check=True)
    print(f"Installed {service_path}")
    if enable_now:
        print(f"Started with: systemctl --user status {service_name}.service")
    return service_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-name", default="hermes-triage-monitor-default")
    parser.add_argument("--hermes-home", type=Path, default=DEFAULT_HERMES_HOME)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--board", default="default")
    parser.add_argument("--workflow", default="dev-feature")
    parser.add_argument("--interval-seconds", type=float, default=20.0)
    parser.add_argument("--hermes-bin", type=Path, default=DEFAULT_HERMES_BIN)
    parser.add_argument("--enable-now", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    install_service(
        service_name=args.service_name,
        hermes_home=args.hermes_home,
        repo=args.repo.resolve(),
        board=args.board,
        workflow=args.workflow,
        interval_seconds=args.interval_seconds,
        hermes_bin=args.hermes_bin,
        enable_now=args.enable_now,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
