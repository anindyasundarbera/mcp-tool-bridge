import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy, ProjectCapability
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.open_hands_action import OpenHandsActionManager, OpenHandsActionError
from local_dev_mcp.openhands_executor import OpenHandsExecutor
from tests.test_openhands_executor import FakeOpenHandsRuntimeDriver

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


class TestOpenHandsActionRealRuntime(IsolatedAsyncioTestCase):
    """Tests for the real-executor path and fail-closed runtime requirements."""

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

    async def test_require_real_runtime_without_executor_fails_closed(self):
        with self.assertRaises(OpenHandsActionError) as cm:
            OpenHandsActionManager(
                settings=self.settings,
                locks=self.locks,
                projects=(self.project,),
                require_real_runtime=True,
            )
        self.assertIn(
            "Real runtime required but OpenHands runtime/executor is not available or unhealthy",
            str(cm.exception),
        )

    async def test_require_real_runtime_with_unhealthy_executor_fails_closed(self):
        executor = OpenHandsExecutor(driver=FakeOpenHandsRuntimeDriver(healthy=False))
        manager = OpenHandsActionManager(
            settings=self.settings,
            locks=self.locks,
            projects=(self.project,),
            executor=executor,
            require_real_runtime=True,
        )
        self.addAsyncCleanup(manager.close)
        params = {
            "canonical_baseline_fingerprint": "c" * 64,
            "action_specification": "Perform some action",
        }
        with self.assertRaises(OpenHandsActionError) as cm:
            await manager.execute_action(self.project, params)
        self.assertIn(
            "Real runtime required but OpenHands runtime/executor is not available or unhealthy",
            str(cm.exception),
        )

    async def test_execute_with_executor_records_ledger(self):
        driver = FakeOpenHandsRuntimeDriver(driver_checkpoints=1)
        executor = OpenHandsExecutor(driver=driver)
        manager = OpenHandsActionManager(
            settings=self.settings,
            locks=self.locks,
            projects=(self.project,),
            executor=executor,
            require_real_runtime=True,
        )
        self.addAsyncCleanup(manager.close)
        params = {
            "canonical_baseline_fingerprint": "d" * 64,
            "action_specification": "Add a feature",
            "expected_outcome": "Success",
        }
        result = await manager.execute_action(self.project, params)

        self.assertEqual(result["final_verdict"], "PASS")
        self.assertIsNone(result["failure_reason"])
        self.assertTrue(result["ledger_reference"].startswith("OH-001D-"))
        self.assertEqual(result["executor_state"], "completed")
        self.assertFalse(result["grants_authority"])
        self.assertEqual(result["permitted_tools"], ["Read", "Edit", "Write", "Glob", "Grep"])
        self.assertIn("openhands_edit.txt", result["modified_paths"])
        self.assertEqual(result["canonical_state_verification_result"], "PASS")
        self.assertEqual(result["teardown_result"], "SUCCESS")
        self.assertEqual(result["leak_residue_verification_result"], "CLEAN")
        self.assertEqual(len(driver.active_containers), 0)

        import local_dev_mcp.store as store
        row = store.load_oh_ledger_row(manager._db, result["ledger_reference"])
        self.assertIsNotNone(row)
        self.assertEqual(row["operation_id"], result["operation_id"])
        self.assertEqual(row["final_verdict"], "PASS")
        self.assertEqual(row["canonical_verification_result"], "PASS")
        self.assertEqual(row["teardown_result"], "SUCCESS")
        self.assertEqual(row["permitted_tools"], '["Read", "Edit", "Write", "Glob", "Grep"]')

    async def test_execute_via_executor_fails_closed_without_executor(self):
        manager = OpenHandsActionManager(
            settings=self.settings,
            locks=self.locks,
            projects=(self.project,),
        )
        self.addAsyncCleanup(manager.close)
        params = {
            "canonical_baseline_fingerprint": "e" * 64,
            "action_specification": "Perform some action",
        }
        with self.assertRaises(OpenHandsActionError) as cm:
            await manager.execute_via_executor(self.project, params)
        self.assertIn(
            "Real runtime required but OpenHands runtime/executor is not available or unhealthy",
            str(cm.exception),
        )

    async def test_execute_via_executor_requires_baseline_fingerprint(self):
        executor = OpenHandsExecutor(driver=FakeOpenHandsRuntimeDriver())
        manager = OpenHandsActionManager(
            settings=self.settings,
            locks=self.locks,
            projects=(self.project,),
            executor=executor,
        )
        self.addAsyncCleanup(manager.close)
        with self.assertRaises(OpenHandsActionError) as cm:
            await manager.execute_via_executor(self.project, {"action_specification": "x"})
        self.assertIn("missing canonical_baseline_fingerprint", str(cm.exception))

    async def test_execute_via_executor_records_failure_on_crash(self):
        driver = FakeOpenHandsRuntimeDriver(crash=True)
        executor = OpenHandsExecutor(driver=driver)
        manager = OpenHandsActionManager(
            settings=self.settings,
            locks=self.locks,
            projects=(self.project,),
            executor=executor,
        )
        self.addAsyncCleanup(manager.close)
        params = {
            "canonical_baseline_fingerprint": "f" * 64,
            "action_specification": "Add a feature",
        }
        result = await manager.execute_action(self.project, params)

        self.assertEqual(result["final_verdict"], "FAIL")
        self.assertEqual(result["executor_state"], "failed")
        self.assertEqual(result["independent_inspection_result"], "FAIL")
        self.assertEqual(result["canonical_state_verification_result"], "PASS")
        self.assertIn("OPENHANDS_RUNTIME_CRASH", str(result["failure_reason"]))
        self.assertEqual(len(driver.active_containers), 0)

        import local_dev_mcp.store as store
        row = store.load_oh_ledger_row(manager._db, result["ledger_reference"])
        self.assertIsNotNone(row)
        self.assertEqual(row["final_verdict"], "FAIL")
        self.assertIn("OPENHANDS_RUNTIME_CRASH", str(row["failure_reason"]))

if __name__ == "__main__":
    unittest.main()
