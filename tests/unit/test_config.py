"""Unit coverage for typed repo configuration overlays."""

from __future__ import annotations

import pytest

from shared.lib.config import Settings, _apply_platform_overrides

pytestmark = pytest.mark.unit


def test_platform_overrides_preserve_integer_settings(monkeypatch) -> None:
    monkeypatch.delenv("PLATFORM_MAX_RUNNING_TASKS", raising=False)
    target = Settings(platform_config_file="")

    _apply_platform_overrides(target, {"PLATFORM_MAX_RUNNING_TASKS": "3"})

    assert target.platform_max_running_tasks == 3
    assert isinstance(target.platform_max_running_tasks, int)


def test_environment_value_takes_precedence_over_platform_override(monkeypatch) -> None:
    monkeypatch.setenv("PLATFORM_MAX_RUNNING_TASKS", "2")
    target = Settings(platform_config_file="")

    _apply_platform_overrides(target, {"PLATFORM_MAX_RUNNING_TASKS": "3"})

    assert target.platform_max_running_tasks == 2
