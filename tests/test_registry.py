import tempfile
import unittest
from pathlib import Path

from local_dev_mcp import store
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
        with self.assertRaisesRegex(ProjectRegistryError, "does not allow markdown_write"):
            registry.resolve("test-project", ProjectCapability.MARKDOWN_WRITE)

    def test_markdown_write_requires_an_approved_root(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        write_registry(
            config,
            [
                project_payload(
                    repository,
                    capabilities=["repository_read", "markdown_write"],
                    markdown_write_roots=[],
                )
            ],
        )

        with self.assertRaisesRegex(ProjectRegistryError, "approved write root"):
            ProjectRegistry.load(config)

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

    def test_self_managed_allows_isolated_claude_but_rejects_direct_writer(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        write_registry(
            config,
            [project_payload(repository, self_managed=True)],
        )
        registry = ProjectRegistry.load(config)
        self.assertTrue(registry.resolve("test-project").self_managed)

        write_registry(
            config,
            [
                project_payload(
                    repository,
                    self_managed=True,
                    capabilities=["repository_read", "markdown_write"],
                    markdown_write_roots=["docs"],
                )
            ],
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

    def test_verification_commit_requires_fixed_commands_roots_and_claude(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        valid = project_payload(
            repository,
            capabilities=[
                "repository_read",
                "claude_edit",
                "verification_commit",
            ],
            commit_roots=["."],
            verification_commands=[
                {
                    "id": "core",
                    "argv": ["{python}", "-m", "unittest"],
                    "python_path": ["src"],
                }
            ],
        )
        write_registry(config, [valid])
        project = ProjectRegistry.load(config).resolve(
            "test-project", ProjectCapability.VERIFICATION_COMMIT
        )
        self.assertEqual(project.commit_roots, (".",))
        self.assertEqual(project.verification_commands[0].id, "core")
        self.assertNotIn("verification_commands", project.public_metadata())

        for update, expected in (
            ({"commit_roots": []}, "approved commit root"),
            ({"verification_commands": []}, "verification command"),
            (
                {"capabilities": ["repository_read", "verification_commit"]},
                "requires claude_edit",
            ),
            (
                {
                    "verification_commands": [
                        {"id": "core", "argv": ["python"]},
                        {"id": "core", "argv": ["python", "-V"]},
                    ]
                },
                "IDs must be unique",
            ),
        ):
            with self.subTest(update=update):
                payload = dict(valid)
                payload.update(update)
                write_registry(config, [payload])
                with self.assertRaisesRegex(ProjectRegistryError, expected):
                    ProjectRegistry.load(config)

    def test_push_development_commit_requires_verification_commit(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        valid = project_payload(
            repository,
            capabilities=[
                "repository_read",
                "claude_edit",
                "verification_commit",
                "push_development_commit",
            ],
            commit_roots=["."],
            verification_commands=[
                {"id": "core", "argv": ["{python}", "-m", "unittest"]},
            ],
        )
        write_registry(config, [valid])
        project = ProjectRegistry.load(config).resolve(
            "test-project", ProjectCapability.PUSH_DEVELOPMENT_COMMIT
        )
        self.assertIn(
            "push_development_commit", project.public_metadata()["capabilities"]
        )

        payload = dict(valid)
        payload.update(
            {"capabilities": ["repository_read", "claude_edit", "push_development_commit"]}
        )
        write_registry(config, [payload])
        with self.assertRaisesRegex(
            ProjectRegistryError, "push_development_commit requires verification_commit"
        ):
            ProjectRegistry.load(config)

    def test_rejects_invalid_verification_command_vectors(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        base = project_payload(
            repository,
            capabilities=["repository_read", "claude_edit", "verification_commit"],
            commit_roots=["."],
        )
        for commands, expected in (
            ([{"id": "core", "argv": []}], "1 through 64"),
            ([{"id": "core", "argv": "python"}], "list of arguments"),
            (
                [{"id": "core", "argv": ["python", "{python}"]}],
                "only as the verification executable",
            ),
            ([{"id": "core", "argv": ["bash", "-c", "true"]}], "must not invoke a shell"),
            (
                [{"id": "core", "argv": ["python"], "python_path": ["../src"]}],
                "policy paths",
            ),
        ):
            with self.subTest(commands=commands):
                payload = dict(base)
                payload["verification_commands"] = commands
                write_registry(config, [payload])
                with self.assertRaisesRegex(ProjectRegistryError, expected):
                    ProjectRegistry.load(config)

    # ------------------------------------------------------------------
    # M-GV-03: external grounding for genesis/apex authority.
    # ------------------------------------------------------------------

    def test_grounded_policy_registers(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        ref = f"docs/spec.md@{'a' * 40}"
        write_registry(
            config,
            [
                project_payload(
                    repository,
                    apex_authority_ref=ref,
                    require_apex_grounding=True,
                )
            ],
        )
        registry = ProjectRegistry.load(
            config, require_apex_grounding=True
        )
        self.assertEqual(
            registry.resolve("test-project").apex_authority_ref, ref
        )

    def test_url_apex_ref_is_accepted(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        write_registry(
            config,
            [
                project_payload(
                    repository,
                    apex_authority_ref="https://example.com/specs/apex@v1",
                )
            ],
        )
        registry = ProjectRegistry.load(config)
        self.assertEqual(
            registry.resolve("test-project").apex_authority_ref,
            "https://example.com/specs/apex@v1",
        )

    def test_ungrounded_policy_is_rejected_when_flag_is_set(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        write_registry(config, [project_payload(repository)])
        with self.assertRaisesRegex(
            ProjectRegistryError, "requires apex_authority_ref"
        ):
            ProjectRegistry.load(config, require_apex_grounding=True)
        # Without the flag the same policy still loads (opt-in).
        ProjectRegistry.load(config)

    def test_per_policy_grounding_flag_requires_a_ref(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        write_registry(
            config,
            [project_payload(repository, require_apex_grounding=True)],
        )
        with self.assertRaisesRegex(
            ProjectRegistryError, "requires apex_authority_ref"
        ):
            ProjectRegistry.load(config)

    def test_rejects_invalid_apex_ref_shapes(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        for ref, expected in (
            ("", "must not be empty"),
            ("docs/spec.md", "URL or a '<repo-path>@<git-sha>'"),
            ("docs/spec.md@not-a-sha", "7-40 character git SHA"),
            ("../escape@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "repo path"),
            ("http://", "scheme and a target"),
        ):
            with self.subTest(ref=ref):
                write_registry(
                    config,
                    [project_payload(repository, apex_authority_ref=ref)],
                )
                with self.assertRaisesRegex(ProjectRegistryError, expected):
                    ProjectRegistry.load(config)

    def test_changing_apex_ref_without_reconfirmation_is_rejected(self) -> None:
        repository = self.root / "repo"
        initialize_git_repository(repository)
        config = self.root / "projects.json"
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_db = store.open_state_db(state_dir / "bridge.db")
        ref_a = f"docs/spec.md@{'a' * 40}"
        write_registry(
            config,
            [project_payload(repository, apex_authority_ref=ref_a)],
        )
        # First load registers the ref.
        registry = ProjectRegistry.load(config, state_db=state_db)
        self.assertEqual(
            registry.resolve("test-project").apex_authority_ref, ref_a
        )

        # Changing the ref in projects.json without re-confirmation is rejected.
        ref_b = f"docs/spec.md@{'b' * 40}"
        write_registry(
            config,
            [project_payload(repository, apex_authority_ref=ref_b)],
        )
        with self.assertRaisesRegex(ProjectRegistryError, "changed without"):
            ProjectRegistry.load(config, state_db=state_db)

        # Explicit re-confirmation accepts the new ref durably.
        registry.confirm_apex_authority("test-project", ref_b)
        reloaded = ProjectRegistry.load(config, state_db=state_db)
        self.assertEqual(
            reloaded.resolve("test-project").apex_authority_ref, ref_b
        )


if __name__ == "__main__":
    unittest.main()
