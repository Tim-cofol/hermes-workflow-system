"""Feishu command adapter for `/wf` triggers."""

from __future__ import annotations

from router.app import WorkflowRouterApp
from router.cli import parse_wf_command


def route_feishu_command(
    text: str,
    *,
    app: WorkflowRouterApp,
    requested_by: str | None = None,
) -> dict[str, object]:
    request = parse_wf_command(
        text,
        aliases=app.aliases,
        source_type="feishu",
        requested_by=requested_by,
    )
    return app.create_run(
        request.workflow_id,
        {
            "inputs": request.inputs,
            "source": request.source,
            "requested_by": requested_by,
        },
    )
