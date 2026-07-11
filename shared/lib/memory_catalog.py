"""Shared memory catalog metadata used by both gateway and MCP services."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml

from shared.lib.config import settings

BANK_INCIDENT_RCA = "incident-rca"
BANK_WORKFLOW_LEARNING = "workflow-learning"
WorkflowBankKind = Literal["business", "learning"]
WORKFLOW_BANKS: dict[str, dict[str, str]] = {"business": {}, "learning": {}}


def load_workflow_banks(platform_file: str | None = None) -> dict[str, dict[str, str]]:
    """Load workflow-to-memory-bank routing from platform-config.yaml."""
    path_value = (
        platform_file if platform_file is not None else settings.platform_config_file or settings.platform_secrets_file
    )
    banks = {kind: dict(mapping) for kind, mapping in WORKFLOW_BANKS.items()}
    if not path_value:
        return banks

    path = Path(path_value)
    if not path.exists():
        return banks

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return banks

    memory = data.get("memory") or {}
    configured_banks = memory.get("banks") if isinstance(memory, dict) else None
    if not isinstance(configured_banks, dict):
        return banks

    for kind in ("business", "learning"):
        mapping = configured_banks.get(kind) or {}
        if not isinstance(mapping, dict):
            continue
        banks[kind] = {str(workflow): str(bank_id) for workflow, bank_id in mapping.items() if workflow and bank_id}
    return banks
