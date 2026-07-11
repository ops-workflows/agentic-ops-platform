"""Scenario DSL helpers for runtime tests.

A Scenario bundles:

- a list of scripted `Turn`s for the mock LLM (or probe-only turns)
- scripted fake Message replies
- scripted fake Hindsight recall items
- expected assertions about final DB state

The DSL is intentionally minimal — most scenarios only set ``name``,
``prompt``, and ``llm_turns``. Probe-style scenarios set ``probe_markers``
which are tested for presence in the upstream LLM request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tests.fakes.mock_llm import Turn, make_marker_probe


@dataclass
class MattermostReply:
    kind: str  # "approval" or "question" or "chat"
    message: str
    delay_sec: float = 0.0
    user_id: str = "user-operator"
    username: str = "operator"


@dataclass
class Scenario:
    name: str
    prompt: str
    llm_turns: list[Turn] = field(default_factory=list)
    mattermost_replies: list[MattermostReply] = field(default_factory=list)
    hindsight_recall: list[dict[str, Any]] = field(default_factory=list)
    expected_task_status: str = "succeeded"
    expected_approvals: list[dict[str, Any]] = field(default_factory=list)
    # Markers tested for presence on the first LLM request via a probe turn.
    probe_markers: list[str] = field(default_factory=list)


def probe_then_end(markers: list[str]) -> list[Turn]:
    """Build a one-turn scenario that probes for ``markers`` and ends.

    Use for instruction-surface tests: the runtime sends one request,
    the mock returns a found/missing summary, the runtime ends cleanly.
    """
    return [
        Turn(
            probe=make_marker_probe(markers),
            stop_reason="end_turn",
        ),
    ]
