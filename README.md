# Hermes Workflow System

Hermes Workflow System is a Hermes plugin/workflow pack for running deterministic
multi-agent development workflows through Hermes Kanban.

The current release centers on `dev-feature-v3`: an architect-planned, serial
Codex implementation workflow with explicit check, revise, commit, and PR gates.
Hermes Kanban remains the only runtime state source; this repository supplies the
workflow templates, profile mappings, router adapters, operational helpers, and
installation tooling around that runtime.

## What Is Included

- `workflows/dev-feature-v3.yaml` - source workflow template.
- `skills/workflow-orchestrator/` - Hermes skill used to expand workflow roots
  into Kanban DAG tasks.
- `profiles/` and `skill-bundles/` - local Hermes profile definitions and skill
  bundles for orchestrator, architect, dev-codex, reviewer, and shipper.
- `instantiator/` - deterministic compiler from workflow YAML to Hermes Kanban
  tasks.
- `router/` - adapters for CLI, GitHub labels, Feishu commands, cron, HTTP-style
  payloads, and dashboard triage intake.
- `operations/` - workflow catalog, run history, operator controls, approval
  gates, metrics, and PR merge-gate reconciliation.
- `scripts/` - artifact installation, validation, graph expansion, and triage
  monitor service setup.

## Install

Install the workflow artifacts into a local Hermes home:

```bash
python3 scripts/install_hermes_v13_artifacts.py
```

Validate repository artifacts:

```bash
python3 scripts/validate_hermes_v13_artifacts.py
```

Validate installed artifacts:

```bash
python3 scripts/validate_hermes_v13_artifacts.py --installed
```

## Run A Workflow

Create a deterministic local workflow run:

```bash
python3 -m instantiator.instantiate \
  --workflow dev-feature-v3 \
  --repo /path/to/repo \
  --issue 123 \
  --board default
```

Route through the `/wf` CLI adapter:

```bash
python3 -m router.cli "/wf dev-feature repo=/path/to/repo issue=123 board=default"
```

For dashboard triage intake, install the user service:

```bash
python3 scripts/install_hermes_triage_monitor_service.py \
  --board default \
  --repo /path/to/repo \
  --enable-now
```

## Verify

Run the focused test suite:

```bash
python3 -m pytest -q
python3 scripts/validate_hermes_v13_artifacts.py
```

## Design Notes

- Development is serial by default: `dev-codex` runs with one active worker.
- Router adapters normalize external triggers, but do not become a second
  workflow runtime.
- Runtime state is derived from Hermes Kanban tasks; this system persists only
  instantiation transactions, node/task mappings, and operator audit actions.
- Human intervention defaults to clarification, protected paths, repeated
  automated failures, or external permission gates.
