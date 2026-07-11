"""Layer 0 — memory catalog."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from shared.lib.memory_catalog import (  # noqa: E402
    BANK_INCIDENT_RCA,
    BANK_WORKFLOW_LEARNING,
    WORKFLOW_BANKS,
    load_workflow_banks,
)


def test_bank_constants() -> None:
    assert BANK_INCIDENT_RCA == "incident-rca"
    assert BANK_WORKFLOW_LEARNING == "workflow-learning"


def test_workflow_banks_shape() -> None:
    assert set(WORKFLOW_BANKS.keys()) == {"business", "learning"}
    for _kind, mapping in WORKFLOW_BANKS.items():
        assert isinstance(mapping, dict)
        for workflow, bank in mapping.items():
            assert isinstance(workflow, str) and workflow
            assert isinstance(bank, str) and bank


def test_workflow_banks_cover_shipped_workflows() -> None:
    """Every workflow that appears in 'business' should also appear in 'learning'."""
    assert set(WORKFLOW_BANKS["business"].keys()) == set(WORKFLOW_BANKS["learning"].keys())


def test_load_workflow_banks_from_platform_config(tmp_path) -> None:
    config = tmp_path / "platform-config.yaml"
    config.write_text(
        """
memory:
    banks:
        business:
            platform-test: incident-rca-platform-test
        learning:
            platform-test: workflow-learning-platform-test
""".lstrip(),
        encoding="utf-8",
    )

    banks = load_workflow_banks(str(config))

    assert banks["business"] == {"platform-test": "incident-rca-platform-test"}
    assert banks["learning"] == {"platform-test": "workflow-learning-platform-test"}
