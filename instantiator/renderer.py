"""Small template renderer for workflow YAML values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RenderContext(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_string(value: str, context: Mapping[str, Any]) -> str:
    return value.format_map(RenderContext({key: str(val) for key, val in context.items()}))


def render_value(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return render_string(value, context)
    if isinstance(value, list):
        return [render_value(item, context) for item in value]
    if isinstance(value, dict):
        return {key: render_value(item, context) for key, item in value.items()}
    return value
