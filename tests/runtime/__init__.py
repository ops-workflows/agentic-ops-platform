"""Layer 2 — runtime scenario tests (real Docker runtime container).

All tests in this package require TEST_RUNTIME_ENABLED=1 and a built
`ai-ops-agent-runtime:latest` image. They are skipped by default so the
standard `pytest` run stays fast.
"""
