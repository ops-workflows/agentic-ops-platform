"""Workflow configuration helpers shared across Session Manager modules."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from shared.lib.workflow_paths import find_workflow_package

logger = logging.getLogger(__name__)


def load_agent_yaml(workflow: str) -> dict[str, Any]:
    """Load agent.yaml for a given workflow from configured workflow roots."""
    package = find_workflow_package(workflow)
    return package.config if package else {}


def workflow_package_path(workflow: str) -> Path | None:
    package = find_workflow_package(workflow)
    return package.path if package else None
