"""Layer 0 — approval metadata merge helper."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from shared.lib.approvals import _merge_metadata  # noqa: E402


def test_merge_metadata_with_none_base() -> None:
    assert _merge_metadata(None, {"a": 1}) == {"a": 1}


def test_merge_metadata_overrides_existing_keys() -> None:
    assert _merge_metadata({"a": 1, "b": 2}, {"b": 3, "c": 4}) == {"a": 1, "b": 3, "c": 4}


def test_merge_metadata_does_not_mutate_input() -> None:
    original = {"a": 1}
    out = _merge_metadata(original, {"b": 2})
    assert original == {"a": 1}
    assert out == {"a": 1, "b": 2}
