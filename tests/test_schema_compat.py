"""Tests for public MCP tool catalog, schema compatibility, and drift detection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import unittest
from typing import Any

from local_dev_mcp.registry import ProjectRegistry
from local_dev_mcp.schema_compat import (
    PUBLIC_TOOL_CATALOG,
    ToolLifecycleStage,
    compute_schema_digest,
    inspect_server_schema,
    validate_server_schema_compatibility,
)
from local_dev_mcp.server import create_server


@dataclass
class _DummyAnnotations:
    readOnlyHint: bool = False
    destructiveHint: bool = False
    idempotentHint: bool = False
    openWorldHint: bool = False


@dataclass
class _DummyTool:
    name: str
    parameters: dict[str, Any]
    annotations: _DummyAnnotations


class _MockServer:
    def __init__(self, tools: list[_DummyTool]) -> None:
        self._tools = tools

    async def list_tools(self) -> list[_DummyTool]:
        return self._tools


class SchemaCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server(registry=ProjectRegistry({}))

    def tearDown(self) -> None:
        if hasattr(self.server, "close"):
            self.server.close()

    def test_public_tool_catalog_contains_exact_39_tools(self) -> None:
        self.assertEqual(len(PUBLIC_TOOL_CATALOG), 39)

        quarantined = [
            t for t in PUBLIC_TOOL_CATALOG.values()
            if t.stage == ToolLifecycleStage.QUARANTINED_PRE_V1
        ]
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].name, "standalone_openhands_action")

        secondary = [
            t for t in PUBLIC_TOOL_CATALOG.values()
            if t.stage == ToolLifecycleStage.SECONDARY_V1
        ]
        self.assertEqual(len(secondary), 7)
        secondary_names = {t.name for t in secondary}
        self.assertEqual(
            secondary_names,
            {
                "list_project_markdown",
                "read_project_markdown",
                "search_project_markdown",
                "get_markdown_outline",
                "write_project_markdown",
                "open_pull_request",
                "sync_canonical_checkout",
            },
        )

        core = [
            t for t in PUBLIC_TOOL_CATALOG.values()
            if t.stage == ToolLifecycleStage.CORE_V1
        ]
        self.assertEqual(len(core), 31)
        self.assertEqual(len(core) + len(secondary) + len(quarantined), 39)

    def test_live_server_matches_public_catalog_completely(self) -> None:
        errors = asyncio.run(validate_server_schema_compatibility(self.server))
        self.assertEqual(errors, [])

    def test_inspect_server_schema_reports_expected_counts_and_digest(self) -> None:
        info = asyncio.run(inspect_server_schema(self.server))
        self.assertEqual(info["total_tools"], 39)
        self.assertEqual(info["core_v1_count"], 31)
        self.assertEqual(info["secondary_v1_count"], 7)
        self.assertEqual(info["quarantined_count"], 1)
        self.assertTrue(isinstance(info["schema_digest"], str))
        self.assertEqual(len(info["schema_digest"]), 64)
        self.assertEqual(len(info["tools"]), 39)

        # Inspect specific entries in tools
        reader_entry = info["tools"]["list_allowed_projects"]
        self.assertEqual(reader_entry["stage"], ToolLifecycleStage.CORE_V1.value)
        self.assertTrue(reader_entry["read_only"])
        self.assertFalse(reader_entry["destructive"])

        oh_entry = info["tools"]["standalone_openhands_action"]
        self.assertEqual(oh_entry["stage"], ToolLifecycleStage.QUARANTINED_PRE_V1.value)
        self.assertFalse(oh_entry["read_only"])
        self.assertTrue(oh_entry["destructive"])

    def test_schema_digest_is_deterministic_across_server_instances(self) -> None:
        server_2 = create_server(registry=ProjectRegistry({}))
        try:
            info_1 = asyncio.run(inspect_server_schema(self.server))
            info_2 = asyncio.run(inspect_server_schema(server_2))
            self.assertEqual(info_1["schema_digest"], info_2["schema_digest"])
        finally:
            server_2.close()

    def test_drift_detection_missing_tool(self) -> None:
        # Build mock tools omitting one catalog tool
        all_tools = asyncio.run(self.server.list_tools())
        omitted = all_tools[1:]
        mock_server = _MockServer(omitted)

        errors = asyncio.run(validate_server_schema_compatibility(mock_server))
        self.assertTrue(len(errors) >= 1)
        self.assertIn("Missing expected tool from public catalog", errors[0])

    def test_drift_detection_unexpected_tool(self) -> None:
        all_tools = asyncio.run(self.server.list_tools())
        extra = _DummyTool(
            name="unexpected_governance_bypass_tool",
            parameters={"type": "object"},
            annotations=_DummyAnnotations(readOnlyHint=False),
        )
        mock_server = _MockServer(list(all_tools) + [extra])

        errors = asyncio.run(validate_server_schema_compatibility(mock_server))
        self.assertTrue(len(errors) >= 1)
        self.assertTrue(any("Unexpected tool registered" in e for e in errors))

    def test_drift_detection_annotation_mismatch(self) -> None:
        all_tools = list(asyncio.run(self.server.list_tools()))
        # Find list_allowed_projects (which is readOnlyHint=True) and flip to False
        mutated_tools = []
        for t in all_tools:
            if t.name == "list_allowed_projects":
                mutated_tools.append(
                    _DummyTool(
                        name="list_allowed_projects",
                        parameters=t.parameters,
                        annotations=_DummyAnnotations(
                            readOnlyHint=False,
                            destructiveHint=False,
                            idempotentHint=True,
                            openWorldHint=False,
                        ),
                    )
                )
            else:
                mutated_tools.append(t)

        mock_server = _MockServer(mutated_tools)
        errors = asyncio.run(validate_server_schema_compatibility(mock_server))
        self.assertTrue(len(errors) >= 1)
        self.assertTrue(any("annotation mismatch for readOnlyHint" in e for e in errors))

    def test_compute_schema_digest_handles_empty_annotations(self) -> None:
        dummy = _DummyTool(name="test", parameters={}, annotations=None)  # type: ignore[arg-type]
        digest = compute_schema_digest([dummy])
        self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
