"""Deprecated compatibility shim.

The live helper module is `gateway.plugin_dir`. This shim exists only to avoid
breaking older imports while the codebase finishes moving over.
"""

from __future__ import annotations

from gateway.plugin_dir import read_platform_config, read_plugin_files, validate_plugin_dir

compose_for_editor = read_plugin_files

__all__ = ["read_platform_config", "read_plugin_files", "validate_plugin_dir", "compose_for_editor"]
