"""Shared deterministic output budgeting for compact MCP evidence bundles."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

MAX_REDACTION_PATTERNS = 32
MAX_REDACTION_PATTERN_CHARS = 512
MAX_REDACTION_REPLACEMENT_CHARS = 64
_DEFAULT_REDACTION_PATTERNS = (
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), "<email>"),
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            re.I,
        ),
        "<uuid>",
    ),
    (re.compile(r"(?<!\d)\d{12}(?!\d)"), "<account>"),
    (
        re.compile(r"(?i)\b(request|session|trace|invocation|order|model|account)[_-]?id\s*[:=]\s*[^\s,;]+"),
        r"\1_id=<id>",
    ),
    (re.compile(r"\b\d{6}[+-A]\d{3}[0-9A-FHJ-NPR-Y]\b", re.I), "<personal_id>"),
    (re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{4}(?!\d)"), "<payment_card>"),
)


def compile_redaction_patterns(configured: Any = None) -> tuple[tuple[re.Pattern[str], str], ...]:
    """Compile trusted deployment regexes after the conservative built-in rules."""
    if configured in (None, []):
        return _DEFAULT_REDACTION_PATTERNS
    if not isinstance(configured, Sequence) or isinstance(configured, (str, bytes)):
        raise ValueError("evidence redaction_patterns must be a list")
    if len(configured) > MAX_REDACTION_PATTERNS:
        raise ValueError(f"evidence redaction_patterns cannot exceed {MAX_REDACTION_PATTERNS} entries")

    custom: list[tuple[re.Pattern[str], str]] = []
    for index, item in enumerate(configured):
        if not isinstance(item, dict):
            raise ValueError(f"evidence redaction_patterns[{index}] must be a mapping")
        expression = str(item.get("pattern") or "")
        replacement = str(item.get("replacement") or "<redacted>")
        if not expression or len(expression) > MAX_REDACTION_PATTERN_CHARS:
            raise ValueError(f"evidence redaction_patterns[{index}].pattern is invalid")
        if len(replacement) > MAX_REDACTION_REPLACEMENT_CHARS:
            raise ValueError(f"evidence redaction_patterns[{index}].replacement is too long")
        try:
            custom.append((re.compile(expression), replacement))
        except re.error as exc:
            raise ValueError(f"evidence redaction_patterns[{index}].pattern is invalid: {exc}") from exc
    return (*_DEFAULT_REDACTION_PATTERNS, *custom)


def redact_text(value: Any, patterns: tuple[tuple[re.Pattern[str], str], ...]) -> str:
    """Apply shared evidence redaction rules to one projected value."""
    text = str(value or "")
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return text


def _serialized_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())


def fit_evidence_budget(bundle: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Trim lower-priority evidence until the serialized bundle fits ``max_bytes``."""
    if _serialized_size(bundle) <= max_bytes:
        return bundle

    bundle["truncated"] = True
    warnings = bundle.setdefault("warnings", [])
    if "serialized_budget_exceeded" not in warnings:
        warnings.append("serialized_budget_exceeded")
    groups = bundle.get("groups")
    if not isinstance(groups, list):
        return bundle

    for group in reversed(groups):
        samples = group.get("samples") if isinstance(group, dict) else None
        while isinstance(samples, list) and len(samples) > 1 and _serialized_size(bundle) > max_bytes:
            samples.pop()

    omitted_groups = 0
    while len(groups) > 1 and _serialized_size(bundle) > max_bytes:
        groups.pop()
        omitted_groups += 1
    if omitted_groups:
        bundle["omitted_groups"] = omitted_groups

    for group in groups:
        samples = group.get("samples") if isinstance(group, dict) else None
        if not isinstance(samples, list):
            continue
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            for field in ("stack_excerpt", "message_excerpt", "detail"):
                while isinstance(sample.get(field), str) and len(sample[field]) > 256:
                    sample[field] = sample[field][: len(sample[field]) // 2]
                    sample["stack_truncated"] = True
                    if _serialized_size(bundle) <= max_bytes:
                        return bundle

    return bundle
