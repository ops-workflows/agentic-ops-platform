"""Local-only Knowledge Source serving tests."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.lib.knowledge_source_serving import (
    KnowledgeSourceAccessError,
    KnowledgeSourceServingRegistry,
    ServingSnapshot,
    _workflow_has_knowledge_mcp,
    find_paths,
    get_neighbors,
    get_source_excerpt,
    search_source,
    search_symbols,
)

pytestmark = pytest.mark.unit


def _snapshot(tmp_path: Path) -> ServingSnapshot:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "app.py").write_text("def main():\n    return helper()\n", encoding="utf-8")
    return ServingSnapshot(
        source_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        canonical_alias="org/example",
        commit_sha="a" * 40,
        graph={
            "nodes": [
                {"id": "function:main", "name": "main", "source_file": "app.py", "line": 1},
                {"id": "function:helper", "name": "helper", "source_file": "app.py", "line": 2},
            ],
            "links": [{"source": "function:main", "target": "function:helper", "relation": "CALLS"}],
            "hyperedges": [],
        },
        source_root=source_root,
    )


def test_local_graph_queries_return_commit_and_path_provenance(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    (snapshot.source_root / "errors.txt").write_text(
        "Total prices in request doesn't match calculated prices\n",
        encoding="utf-8",
    )

    matches = search_symbols(snapshot, "main")
    source_matches = search_source(snapshot, "TOTAL PRICES IN REQUEST")
    neighbors = get_neighbors(snapshot, "function:main", direction="outgoing")
    paths = find_paths(snapshot, "function:main", "function:helper")
    excerpt = get_source_excerpt(snapshot, "app.py", start_line=1, end_line=2)

    assert matches[0]["commit_sha"] == "a" * 40
    assert source_matches == {
        "source": "org/example",
        "commit_sha": "a" * 40,
        "query": "TOTAL PRICES IN REQUEST",
        "matches": [
            {
                "path": "errors.txt",
                "line": 1,
                "column": 1,
                "preview": "Total prices in request doesn't match calculated prices",
            }
        ],
        "count": 1,
        "truncated": False,
    }
    assert neighbors["neighbors"][0]["symbol"]["id"] == "function:helper"
    assert [item["id"] for item in paths[0]] == ["function:main", "function:helper"]
    assert excerpt["path"] == "app.py"
    assert "return helper()" in excerpt["content"]


def test_source_search_is_literal_bounded_and_skips_generated_content(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    (snapshot.source_root / "first.txt").write_text("needle one\nneedle two\n", encoding="utf-8")
    generated = snapshot.source_root / "node_modules" / "package"
    generated.mkdir(parents=True)
    (generated / "index.js").write_text("needle generated\n", encoding="utf-8")

    result = search_source(snapshot, "needle", limit=1)

    assert result["count"] == 1
    assert result["truncated"] is True
    assert result["matches"][0]["path"] == "first.txt"
    with pytest.raises(ValueError, match="one line"):
        search_source(snapshot, "needle\nsecond line")


def test_symbol_search_falls_back_to_typed_source_matches(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)

    result = search_symbols(snapshot, "return helper()")

    assert result == [
        {
            "result_type": "source_match",
            "source": "org/example",
            "commit_sha": "a" * 40,
            "query": "return helper()",
            "path": "app.py",
            "line": 2,
            "column": 5,
            "preview": "return helper()",
        }
    ]
    assert "id" not in result[0]


def test_symbol_search_does_not_mix_graph_and_source_matches(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)

    result = search_symbols(snapshot, "main")

    assert [match["id"] for match in result] == ["function:main"]
    assert all("result_type" not in match for match in result)


def test_registry_authorizes_and_pins_workflow_with_knowledge_mcp(tmp_path: Path, monkeypatch) -> None:
    snapshot = _snapshot(tmp_path)
    registry = KnowledgeSourceServingRegistry(cache_root=tmp_path / "cache", bucket="knowledge")
    registry._by_alias[snapshot.canonical_alias] = snapshot
    monkeypatch.setattr(
        "shared.lib.knowledge_source_serving._workflow_has_knowledge_mcp",
        lambda workflow: workflow == "example-workflow",
    )

    with registry.pin("org/example", "example-workflow") as pinned:
        assert pinned.version_id == snapshot.version_id
        assert registry._pins[(snapshot.source_id, snapshot.version_id)] == 1
    assert registry._pins == {}

    with pytest.raises(KnowledgeSourceAccessError), registry.pin("org/example", "other-workflow"):
        pass


def test_workflow_access_requires_knowledge_mcp_declaration(tmp_path: Path, monkeypatch) -> None:
    workflow_dir = tmp_path / "investigator"
    workflow_dir.mkdir()
    (workflow_dir / ".mcp.json").write_text('{"mcpServers":{"knowledge":{}}}', encoding="utf-8")
    monkeypatch.setattr(
        "shared.lib.knowledge_source_serving.discover_workflow_packages",
        lambda: [SimpleNamespace(name="investigator", path=workflow_dir)],
    )

    assert _workflow_has_knowledge_mcp("investigator")
    assert not _workflow_has_knowledge_mcp("other-workflow")
