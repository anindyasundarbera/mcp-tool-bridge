import asyncio
import unittest

from local_dev_mcp.registry import ProjectLockManager


class ProjectLockTests(unittest.TestCase):
    def test_same_project_reuses_lock_and_projects_are_independent(self) -> None:
        async def scenario() -> tuple[object, object, object]:
            manager = ProjectLockManager()
            first = await manager.for_project("one")
            again = await manager.for_project("one")
            second = await manager.for_project("two")
            return first, again, second

        first, again, second = asyncio.run(scenario())
        self.assertIs(first, again)
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()

