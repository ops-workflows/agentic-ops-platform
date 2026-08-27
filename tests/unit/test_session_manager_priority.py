"""Unit coverage for workflow priority normalization in task admission."""

from __future__ import annotations

import pytest
from session_manager.main import _workflow_priority

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("high", 0),
        ("medium", 1),
        ("low", 2),
        (1, 0),
        (2, 1),
        (3, 2),
    ],
)
def test_workflow_priority_normalizes_supported_values(value: str | int, expected: int) -> None:
    assert _workflow_priority({"runtime": {"priority": value}}) == expected


def test_workflow_priority_defaults_to_medium() -> None:
    assert _workflow_priority({"runtime": {}}) == 1
