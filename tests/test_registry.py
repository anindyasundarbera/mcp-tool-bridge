import tempfile
import unittest
from pathlib import Path

from local_dev_mcp.models import ProjectCapability
from local_dev_mcp.registry import ProjectRegistry, ProjectRegistryError

from tests.support import initialize_git_repository, project_payload, write_registry


class ProjectRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_loads_and_returns_safe_public_metadata(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        write_registry(config, [project_payload(repository)])

        registry = ProjectRegistry.load(config)
        public = registry.list_public()

        self.assertEqual(public[0]["project_id"], "test-project")
        self.assertNotIn("root", public[0])
        self.assertNotIn("protected_paths", public[0])
        resolved = registry.resolve("test-project", ProjectCapability.CLAUDE_EDIT)
        self.assertEqual(resolved.root, repository.resolve())

    def test_unknown_and_unauthorized_projects_fail_closed(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        write_registry(
            config,
            [project_payload(repository, capabilities=["repository_read"])],
        )
        registry = ProjectRegistry.load(config)

        with self.assertRaisesRegex(ProjectRegistryError, "unknown or disabled"):
            registry.resolve("missing")
        with self.assertRaisesRegex(ProjectRegistryError, "does not allow claude_edit"):
            registry.resolve("test-project", ProjectCapability.CLAUDE_EDIT)

    def test_disabled_missing_project_does_not_block_startup(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        write_registry(
            config,
            [
                project_payload(repository),
                project_payload(
                    self.root / "missing",
                    project_id="future-project",
                    enabled=False,
                ),
            ],
        )
        registry = ProjectRegistry.load(config)
        self.assertEqual(len(registry.list_public()), 1)

    def test_rejects_duplicate_resolved_roots(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        alias = self.root / "alias"
        alias.symlink_to(repository, target_is_directory=True)
        config = self.root / "projects.json"
        write_registry(
            config,
            [
                project_payload(repository, project_id="project-one"),
                project_payload(alias, project_id="project-two"),
            ],
        )
        with self.assertRaisesRegex(ProjectRegistryError, "share a root"):
            ProjectRegistry.load(config)

    def test_rejects_self_managed_direct_edit(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        write_registry(
            config,
            [project_payload(repository, self_managed=True)],
        )
        with self.assertRaisesRegex(ProjectRegistryError, "self-managed"):
            ProjectRegistry.load(config)

    def test_rejects_policy_path_traversal(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        write_registry(
            config,
            [project_payload(repository, protected_paths=["../secret"])],
        )
        with self.assertRaisesRegex(ProjectRegistryError, "policy paths"):
            ProjectRegistry.load(config)


if __name__ == "__main__":
    unittest.main()

