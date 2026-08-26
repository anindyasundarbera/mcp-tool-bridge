import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy, ProjectCapability
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.open_hands_action import OpenHandsActionManager, OpenHandsActionError

class TestOpenHandsAction(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = Settings.from_env()
        self.locks = ProjectLockManager()
        self.project = ProjectPolicy(
            id="test-project",
            display_name="Test Project",
            description="A project for testing OpenHands actions",
            root=Path("/tmp/test-project"),
            capabilities={ProjectCapability.REPOSITORY_READ, ProjectCapability.GOVERNED_DISPOSABLE_RUNTIME_ACTION_EXECUTION},
        )
        self.manager = OpenHandsActionManager(
            settings=self.settings,
            locks=self.locks,
            projects=(self.project,),
        )

    async def test_successful_execution(self):
        params = {
            "canonical_baseline_fingerprint": "a" * 64,
            "action_specification": "Perform some action",
            "expected_outcome": "Success",
        }
        result = await self.manager.execute_action(self.project, params)
        self.assertEqual(result["final_verdict"], "PASS")
        self.assertIn("ledger_reference", result)
        self.assertTrue(result["ledger_reference"].startswith("OH-001D-"))

    async def test_missing_baseline_fingerprint(self):
        params = {
            "action_specification": "Perform some action",
        }
        with self.assertRaises(OpenHandsActionError) as cm:
            await self.manager.execute_action(self.project, params)
        self.assertIn("missing canonical_baseline_fingerprint", str(cm.exception))

    async def test_ledger_binding_persistence(self):
        # This test ensures that the result is actually written to the store.
        params = {
            "canonical_baseline_fingerprint": "b" * 64,
            "action_specification": "Test persistence",
        }
        result = await self.manager.execute_action(self.project, params)
        ledger_id = result["ledger_reference"]

        # Use the manager's internal db to verify
        import local_dev_mcp.store as store
        row = store.load_oh_ledger_row(self.manager._db, ledger_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["operation_id"], result["operation_id"])
        self.assertEqual(row["final_verdict"], "PASS")

if __name__ == "__main__":
    unittest.main()
