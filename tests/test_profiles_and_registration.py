"""Unit tests for capability profiles and read-only project registration.

Covers every requirement of ``local_dev_mcp.profiles`` and
``local_dev_mcp.project_registration``: the profile catalog, containment and
trust-mode enforcement, branch-prefix validation, dependency-closure
enforcement, deterministic policy digests, read-only Git inspection, and a
registration flow that never executes verification commands and produces a
config the production registry can actually load.
"""

import hashlib
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from local_dev_mcp import profiles as profiles_module
from local_dev_mcp import project_registration as registration_module
from local_dev_mcp.models import ProjectCapability, ProjectPolicy
from local_dev_mcp.profiles import (
    PROFILE_CATALOG_VERSION,
    CapabilityProfileDefinition,
    ExpandedProfilePolicy,
    ProfileName,
    RepositoryTrustMode,
    expand_capability_profile,
    explain_capability_profile,
    list_capability_profiles,
)
from local_dev_mcp.project_registration import (
    PROTECTED_PATH_SUGGESTIONS,
    inspect_git_repository_readonly,
    register_project,
    show_project,
    validate_projects_config,
)
from local_dev_mcp.registry import ProjectRegistry

from tests.support import initialize_git_repository, project_payload, write_registry

HEX64 = "0123456789abcdef"

UNTRUSTED_CONTAINMENT_ERROR = (
    "Untrusted repository profile requires active containment and cannot run "
    "unsandboxed"
)


def _core_command() -> dict[str, object]:
    return {"id": "core", "argv": ["{python}", "-m", "unittest"]}


class ModuleHygieneTests(unittest.TestCase):
    def test_profiles_module_has_no_fastmcp_or_subprocess(self) -> None:
        source = Path(profiles_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("fastmcp", source)
        self.assertNotIn("subprocess", source)

    def test_registration_module_has_no_fastmcp_and_no_shell(self) -> None:
        source = Path(registration_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("fastmcp", source)
        self.assertNotIn("shell=True", source)


class ProfileCatalogTests(unittest.TestCase):
    def test_catalog_version(self) -> None:
        self.assertEqual(PROFILE_CATALOG_VERSION, "1.0")

    def test_profile_name_values(self) -> None:
        self.assertEqual(ProfileName.READONLY.value, "readonly")
        self.assertEqual(ProfileName.DEVELOP.value, "develop")
        self.assertEqual(ProfileName.DEVELOP_VERIFY.value, "develop-verify")
        self.assertEqual(
            ProfileName.REVIEWED_DEVELOPMENT.value, "reviewed-development"
        )
        self.assertEqual(ProfileName.PUBLISH_CANDIDATE.value, "publish-candidate")

    def test_repository_trust_mode_values(self) -> None:
        self.assertEqual(RepositoryTrustMode.UNTRUSTED.value, "untrusted")
        self.assertEqual(RepositoryTrustMode.TRUSTED.value, "trusted")

    def test_list_capability_profiles_returns_exact_catalog(self) -> None:
        definitions = list_capability_profiles()
        self.assertEqual(
            [definition.name for definition in definitions],
            [
                ProfileName.READONLY,
                ProfileName.DEVELOP,
                ProfileName.DEVELOP_VERIFY,
                ProfileName.REVIEWED_DEVELOPMENT,
                ProfileName.PUBLISH_CANDIDATE,
            ],
        )
        for definition in definitions:
            self.assertIsInstance(definition, CapabilityProfileDefinition)
            self.assertEqual(definition.version, PROFILE_CATALOG_VERSION)
            self.assertTrue(definition.summary)
            self.assertEqual(definition.default_containment_mode, "bwrap")

        readonly, develop, develop_verify, reviewed, publish = definitions
        self.assertEqual(
            readonly.capabilities,
            (
                ProjectCapability.REPOSITORY_READ,
                ProjectCapability.POLICY_INTROSPECTION,
            ),
        )
        self.assertEqual(
            develop.capabilities,
            (
                ProjectCapability.REPOSITORY_READ,
                ProjectCapability.POLICY_INTROSPECTION,
                ProjectCapability.MARKDOWN_WRITE,
                ProjectCapability.CLAUDE_EDIT,
                ProjectCapability.JOB_INTROSPECTION,
                ProjectCapability.PAUSE_DEVELOPMENT_JOB,
                ProjectCapability.DISCARD_CANDIDATE,
            ),
        )
        self.assertEqual(
            develop_verify.capabilities,
            develop.capabilities
            + (
                ProjectCapability.VERIFICATION_COMMIT,
                ProjectCapability.REPLAY_VERIFICATION,
                ProjectCapability.REVOKE_AUTHORIZATION,
            ),
        )
        self.assertEqual(
            reviewed.capabilities,
            develop_verify.capabilities
            + (
                ProjectCapability.CANDIDATE_REVIEW,
                ProjectCapability.CANONICAL_HISTORY_READ,
                ProjectCapability.EXPORT_EVIDENCE_BUNDLE,
            ),
        )
        self.assertEqual(
            publish.capabilities,
            reviewed.capabilities
            + (ProjectCapability.PUSH_DEVELOPMENT_COMMIT,),
        )

        # Profile authority-shape flags.
        self.assertEqual(
            [
                (
                    definition.requires_write_roots,
                    definition.requires_verification_commands,
                )
                for definition in definitions
            ],
            [
                (False, False),
                (True, False),
                (True, True),
                (True, True),
                (True, True),
            ],
        )
        self.assertEqual(
            [definition.commit_requires_review for definition in definitions],
            [False, False, False, True, True],
        )
        self.assertEqual(
            [
                definition.commit_requires_human_approval
                for definition in definitions
            ],
            [False, False, True, True, True],
        )
        self.assertEqual(
            [definition.allows_publication for definition in definitions],
            [False, False, False, False, True],
        )

    def test_profile_definitions_are_frozen(self) -> None:
        definition = list_capability_profiles()[0]
        with self.assertRaises(FrozenInstanceError):
            definition.summary = "tampered"  # type: ignore[misc]


class ProfileExpansionTests(unittest.TestCase):
    def test_readonly_default_expansion(self) -> None:
        expanded = expand_capability_profile("readonly")
        self.assertIsInstance(expanded, ExpandedProfilePolicy)
        self.assertEqual(expanded.profile_name, ProfileName.READONLY)
        self.assertEqual(expanded.profile_version, PROFILE_CATALOG_VERSION)
        self.assertEqual(expanded.trust_mode, RepositoryTrustMode.UNTRUSTED)
        self.assertEqual(
            expanded.capabilities, ("policy_introspection", "repository_read")
        )
        self.assertEqual(expanded.containment_mode, "bwrap")
        self.assertFalse(expanded.commit_requires_review)
        self.assertFalse(expanded.commit_requires_human_approval)
        self.assertTrue(expanded.authority_summary)
        self.assertEqual(len(expanded.policy_digest), 64)
        self.assertLessEqual(set(expanded.policy_digest), set(HEX64))

    def test_expansion_accepts_enum_members(self) -> None:
        expanded = expand_capability_profile(
            ProfileName.REVIEWED_DEVELOPMENT,
            trust_mode=RepositoryTrustMode.TRUSTED,
        )
        self.assertEqual(expanded.profile_name, ProfileName.REVIEWED_DEVELOPMENT)
        self.assertEqual(expanded.trust_mode, RepositoryTrustMode.TRUSTED)

    def test_expansion_is_deterministic(self) -> None:
        first = expand_capability_profile("develop-verify")
        second = expand_capability_profile("develop-verify")
        self.assertEqual(first.policy_digest, second.policy_digest)
        self.assertEqual(first.capabilities, second.capabilities)
        self.assertEqual(first.authority_summary, second.authority_summary)
        self.assertEqual(dict(first.policy_dict), dict(second.policy_dict))

    def test_policy_dict_is_immutable(self) -> None:
        expanded = expand_capability_profile("readonly")
        with self.assertRaises(TypeError):
            expanded.policy_dict["capabilities"] = []  # type: ignore[index]

    def test_expanded_policy_is_frozen(self) -> None:
        expanded = expand_capability_profile("readonly")
        with self.assertRaises(FrozenInstanceError):
            expanded.containment_mode = "trusted_dev_uncontained"  # type: ignore[misc]

    def test_digest_is_canonical_json_sha256(self) -> None:
        expanded = expand_capability_profile(
            "publish-candidate", branch_prefix="release/"
        )
        canonical = json.dumps(
            dict(expanded.policy_dict), sort_keys=True, separators=(",", ":")
        )
        self.assertEqual(
            expanded.policy_digest,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    def test_digest_tracks_every_authority_input(self) -> None:
        base = expand_capability_profile("develop")
        for changed in (
            expand_capability_profile("develop", trust_mode="trusted"),
            expand_capability_profile("develop", branch_prefix="feature/"),
            expand_capability_profile(
                "develop", extra_capabilities=("export_evidence_bundle",)
            ),
            expand_capability_profile(
                "develop", remove_capabilities=("markdown_write",)
            ),
        ):
            self.assertNotEqual(base.policy_digest, changed.policy_digest)

    def test_untrusted_requires_active_containment(self) -> None:
        with self.assertRaisesRegex(ValueError, UNTRUSTED_CONTAINMENT_ERROR):
            expand_capability_profile("readonly", containment_available=False)
        with self.assertRaisesRegex(ValueError, UNTRUSTED_CONTAINMENT_ERROR):
            expand_capability_profile("readonly", allow_uncontained_override=True)
        with self.assertRaisesRegex(ValueError, UNTRUSTED_CONTAINMENT_ERROR):
            expand_capability_profile(
                "readonly",
                containment_available=False,
                allow_uncontained_override=True,
            )

    def test_trusted_containment_modes(self) -> None:
        contained = expand_capability_profile("develop", trust_mode="trusted")
        self.assertEqual(contained.containment_mode, "bwrap")
        uncontained = expand_capability_profile(
            "develop", trust_mode="trusted", allow_uncontained_override=True
        )
        self.assertEqual(uncontained.containment_mode, "trusted_dev_uncontained")
        uncontained_without_probe = expand_capability_profile(
            "develop",
            trust_mode="trusted",
            containment_available=False,
            allow_uncontained_override=True,
        )
        self.assertEqual(
            uncontained_without_probe.containment_mode, "trusted_dev_uncontained"
        )

    def test_trusted_without_containment_and_without_override_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "containment is unavailable"):
            expand_capability_profile(
                "develop", trust_mode="trusted", containment_available=False
            )

    def test_branch_prefix_must_not_be_empty_or_target_trunk(self) -> None:
        for prefix in (
            "",
            "   ",
            "main",
            "master",
            "trunk",
            "HEAD",
            "Main",
            "main/feature",
            "master/x",
            "trunk/x",
            "HEAD/feature",
            "MAIN/feature",
            "refs/heads/main",
            "refs/heads/master",
            "refs/heads/trunk",
            "refs/heads/HEAD",
            "refs/heads/main/x",
        ):
            with self.assertRaisesRegex(ValueError, "trunk|empty"):
                expand_capability_profile("publish-candidate", branch_prefix=prefix)

    def test_branch_prefix_accepts_non_trunk_prefixes(self) -> None:
        for prefix in ("conrad/", "feature/", "conrad/phase-3", "release/"):
            expanded = expand_capability_profile(
                "publish-candidate", branch_prefix=prefix
            )
            self.assertEqual(expanded.policy_dict["branch_prefix"], prefix)

    def test_extra_capabilities_are_added(self) -> None:
        expanded = expand_capability_profile(
            "develop", extra_capabilities=("export_evidence_bundle",)
        )
        self.assertIn("export_evidence_bundle", expanded.capabilities)
        self.assertIn("repository_read", expanded.capabilities)

    def test_remove_capabilities_are_removed(self) -> None:
        expanded = expand_capability_profile(
            "publish-candidate", remove_capabilities=("push_development_commit",)
        )
        self.assertNotIn("push_development_commit", expanded.capabilities)
        self.assertFalse(expanded.policy_dict["allows_publication"])

    def test_removing_an_absent_capability_is_a_noop(self) -> None:
        base = expand_capability_profile("develop")
        narrowed = expand_capability_profile(
            "develop", remove_capabilities=("push_development_commit",)
        )
        self.assertEqual(base.policy_digest, narrowed.policy_digest)

    def test_extra_capabilities_must_satisfy_dependency_closure(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "verification_commit requires claude_edit"
        ):
            expand_capability_profile(
                "readonly", extra_capabilities=("verification_commit",)
            )
        with self.assertRaisesRegex(
            ValueError, "push_development_commit requires verification_commit"
        ):
            expand_capability_profile(
                "readonly", extra_capabilities=("push_development_commit",)
            )
        with self.assertRaisesRegex(
            ValueError, "candidate_review requires claude_edit"
        ):
            expand_capability_profile(
                "readonly", extra_capabilities=("candidate_review",)
            )
        with self.assertRaisesRegex(ValueError, "job_introspection requires claude_edit"):
            expand_capability_profile(
                "readonly", extra_capabilities=("job_introspection",)
            )

    def test_removed_capabilities_must_satisfy_dependency_closure(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires verification_commit"):
            expand_capability_profile(
                "publish-candidate", remove_capabilities=("verification_commit",)
            )
        with self.assertRaisesRegex(
            ValueError, "requires_review requires candidate_review"
        ):
            expand_capability_profile(
                "reviewed-development", remove_capabilities=("candidate_review",)
            )
        with self.assertRaisesRegex(ValueError, "requires claude_edit"):
            expand_capability_profile(
                "develop-verify", remove_capabilities=("claude_edit",)
            )
        with self.assertRaisesRegex(ValueError, "requires repository_read"):
            expand_capability_profile(
                "develop", remove_capabilities=("repository_read",)
            )

    def test_unknown_profiles_capabilities_and_trust_modes_raise(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown capability profile"):
            expand_capability_profile("root-access")
        with self.assertRaisesRegex(ValueError, "unknown capability in"):
            expand_capability_profile("develop", extra_capabilities=("sudo",))
        with self.assertRaisesRegex(ValueError, "unknown capability in"):
            expand_capability_profile("develop", remove_capabilities=("sudo",))
        with self.assertRaisesRegex(ValueError, "unknown repository trust mode"):
            expand_capability_profile("develop", trust_mode="sometimes")

    def test_authority_summary_describes_the_expanded_authority(self) -> None:
        expanded = expand_capability_profile("publish-candidate")
        summary = expanded.authority_summary
        self.assertIn("'publish-candidate'", summary)
        self.assertIn("untrusted repository", summary)
        self.assertIn("independent review", summary)
        self.assertIn("explicit human approval", summary)
        self.assertIn("conrad/", summary)
        self.assertIn("bwrap", summary)

    def test_authority_summary_for_profile_without_commit(self) -> None:
        expanded = expand_capability_profile("readonly")
        self.assertIn("commit authority: none", expanded.authority_summary)
        self.assertIn("publication authority: none", expanded.authority_summary)


class ExplainProfileTests(unittest.TestCase):
    def test_explain_returns_the_definition_and_trust_mode(self) -> None:
        explanation = explain_capability_profile("reviewed-development")
        self.assertEqual(explanation["profile"], "reviewed-development")
        self.assertEqual(explanation["profile_catalog_version"], "1.0")
        self.assertEqual(explanation["version"], PROFILE_CATALOG_VERSION)
        self.assertEqual(explanation["trust_mode"], "untrusted")
        self.assertEqual(
            explanation["capabilities"],
            sorted(
                capability.value
                for capability in list_capability_profiles()[3].capabilities
            ),
        )
        self.assertTrue(explanation["requires_write_roots"])
        self.assertTrue(explanation["requires_verification_commands"])
        self.assertTrue(explanation["commit_requires_review"])
        self.assertTrue(explanation["commit_requires_human_approval"])
        self.assertFalse(explanation["allows_publication"])
        self.assertEqual(explanation["default_containment_mode"], "bwrap")
        self.assertTrue(explanation["containment_required"])
        self.assertFalse(explanation["uncontained_override_allowed"])
        self.assertTrue(explanation["summary"])

    def test_explain_reflects_trust_mode(self) -> None:
        trusted = explain_capability_profile(
            "readonly", trust_mode=RepositoryTrustMode.TRUSTED
        )
        self.assertEqual(trusted["trust_mode"], "trusted")
        self.assertFalse(trusted["containment_required"])
        self.assertTrue(trusted["uncontained_override_allowed"])

    def test_explain_unknown_profile_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown capability profile"):
            explain_capability_profile("deploy-everything")


class GitRepositoryInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        initialize_git_repository(self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_inspects_clean_repository_readonly(self) -> None:
        report = inspect_git_repository_readonly(self.repo)
        self.assertTrue(report["is_git_repository"])
        self.assertEqual(report["repo_root"], str(self.repo.resolve()))
        self.assertTrue(report["root_is_toplevel"])
        self.assertEqual(report["git_toplevel"], str(self.repo.resolve()))
        self.assertEqual(report["git_root"], str(self.repo.resolve()))
        self.assertIsInstance(report["branch"], str)
        self.assertTrue(report["branch"])
        self.assertEqual(report["default_branch"], report["branch"])
        self.assertEqual(len(report["head"]), 40)
        self.assertTrue(report["clean"])
        self.assertFalse(report["is_dirty"])
        self.assertEqual(report["changed_count"], 0)
        self.assertEqual(report["changed_paths"], [])
        self.assertFalse(report["changed_paths_truncated"])
        self.assertFalse(report["has_origin_remote"])
        self.assertIsInstance(report["remotes"], list)
        self.assertIn(".git", report["protected_paths_present"])
        self.assertNotIn(".env", report["protected_paths_present"])

    def test_inspects_dirty_repository(self) -> None:
        (self.repo / "notes.md").write_text("dirty\n", encoding="utf-8")
        report = inspect_git_repository_readonly(self.repo)
        self.assertFalse(report["clean"])
        self.assertEqual(report["changed_count"], 1)
        self.assertEqual(report["changed_paths"], ["notes.md"])

    def test_inspection_never_mutates_the_repository(self) -> None:
        before = inspect_git_repository_readonly(self.repo)
        inspect_git_repository_readonly(self.repo)
        after = inspect_git_repository_readonly(self.repo)
        self.assertEqual(before["head"], after["head"])
        self.assertTrue(after["clean"])
        self.assertEqual(
            sorted(path.name for path in self.repo.iterdir()),
            [".git", "README.md"],
        )

    def test_inspects_missing_and_non_git_paths(self) -> None:
        missing = inspect_git_repository_readonly(self.root / "missing")
        self.assertFalse(missing["is_git_repository"])
        self.assertIn("reason", missing)

        plain = self.root / "plain"
        plain.mkdir()
        report = inspect_git_repository_readonly(plain)
        self.assertFalse(report["is_git_repository"])
        self.assertIn("Git repository", report["reason"])

    def test_inspects_subdirectory_against_toplevel(self) -> None:
        subdirectory = self.repo / "sub"
        subdirectory.mkdir()
        report = inspect_git_repository_readonly(subdirectory)
        self.assertTrue(report["is_git_repository"])
        self.assertFalse(report["root_is_toplevel"])
        self.assertEqual(report["git_toplevel"], str(self.repo.resolve()))


class ProjectRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        initialize_git_repository(self.repo)
        self.config = self.root / "projects.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(self, **overrides: object) -> dict[str, object]:
        parameters: dict[str, object] = {
            "config_path": self.config,
            "project_id": "test-project",
            "repo_root": self.repo,
        }
        parameters.update(overrides)
        return register_project(**parameters)

    def test_registers_readonly_project_and_writes_loadable_config(self) -> None:
        result = self.register()
        self.assertTrue(result["written"])
        self.assertTrue(result["registered"])
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["profile"], "readonly")
        self.assertEqual(result["profile_version"], "1.0")
        self.assertEqual(result["trust_mode"], "untrusted")
        self.assertEqual(result["containment_mode"], "bwrap")
        self.assertEqual(result["verification_commands_executed"], 0)
        self.assertEqual(result["verification_commands_recorded"], 0)
        self.assertEqual(len(result["policy_digest"]), 64)
        self.assertTrue(self.config.exists())

        document = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(document["version"], 1)
        entry = result["project_entry"]
        self.assertEqual(entry["id"], "test-project")
        self.assertEqual(entry["enabled"], True)
        self.assertEqual(
            entry["capabilities"], ["policy_introspection", "repository_read"]
        )
        self.assertEqual(entry["markdown_write_roots"], ["docs"])
        self.assertEqual(entry["protected_paths"], list(PROTECTED_PATH_SUGGESTIONS))
        self.assertFalse(entry["requires_review"])
        self.assertEqual(entry["commit_roots"], [])
        self.assertEqual(entry["verification_commands"], [])
        self.assertEqual(document["projects"][0], entry)

        # The written config must load with the production registry (including from_file).
        registry = ProjectRegistry.from_file(self.config)
        self.assertEqual(
            registry.resolve("test-project").root, self.repo.resolve()
        )
        registry.close()
        self.assertTrue(validate_projects_config(self.config)["valid"])

    def test_protected_path_suggestions(self) -> None:
        self.assertEqual(
            PROTECTED_PATH_SUGGESTIONS,
            (".git", ".env", ".claude", ".github/workflows"),
        )

    def test_dry_run_writes_nothing(self) -> None:
        result = self.register(dry_run=True)
        self.assertFalse(result["written"])
        self.assertFalse(result["registered"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["project_entry"]["id"], "test-project")
        self.assertFalse(self.config.exists())

    def test_develop_profile_sets_write_roots(self) -> None:
        result = self.register(profile="develop")
        entry = result["project_entry"]
        self.assertEqual(entry["markdown_write_roots"], ["docs"])
        for capability in (
            "markdown_write",
            "claude_edit",
            "job_introspection",
            "pause_development_job",
            "discard_candidate",
        ):
            self.assertIn(capability, entry["capabilities"])
        self.assertEqual(entry["commit_roots"], [])
        self.assertEqual(entry["verification_commands"], [])
        self.assertFalse(entry["requires_review"])
        self.assertTrue(validate_projects_config(self.config)["valid"])

    def test_develop_verify_requires_verification_commands(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "--verify-cmd"
        ):
            self.register(profile="develop-verify", verification_commands=())

    def test_develop_verify_records_commands_and_commit_root(self) -> None:
        result = self.register(
            profile="develop-verify", verification_commands=(_core_command(),)
        )
        entry = result["project_entry"]
        self.assertEqual(entry["commit_roots"], ["."])
        self.assertEqual(
            entry["verification_commands"],
            [
                {
                    "id": "core",
                    "argv": ["{python}", "-m", "unittest"],
                    "python_path": [],
                    "sandbox": False,
                    "kind": "unit",
                }
            ],
        )
        self.assertEqual(result["verification_commands_recorded"], 1)
        self.assertEqual(result["verification_commands_executed"], 0)
        self.assertIn("verification_commit", entry["capabilities"])
        self.assertIn("replay_verification", entry["capabilities"])
        self.assertIn("revoke_authorization", entry["capabilities"])
        self.assertTrue(validate_projects_config(self.config)["valid"])

    def test_reviewed_development_sets_review_gate(self) -> None:
        result = self.register(
            profile="reviewed-development", verification_commands=(_core_command(),)
        )
        entry = result["project_entry"]
        self.assertTrue(entry["requires_review"])
        for capability in (
            "candidate_review",
            "canonical_history_read",
            "export_evidence_bundle",
        ):
            self.assertIn(capability, entry["capabilities"])
        policy = ProjectPolicy.model_validate(entry)
        self.assertTrue(policy.requires_review)

    def test_publish_candidate_grants_publication(self) -> None:
        result = self.register(
            profile="publish-candidate", verification_commands=(_core_command(),)
        )
        entry = result["project_entry"]
        self.assertIn("push_development_commit", entry["capabilities"])
        self.assertIn("publication authority", result["authority_summary"])
        self.assertTrue(result["policy_digest"])
        self.assertTrue(validate_projects_config(self.config)["valid"])

    def test_rejects_duplicate_project_id(self) -> None:
        self.register()
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.register()

    def test_rejects_shared_repository_root(self) -> None:
        self.register()
        with self.assertRaisesRegex(ValueError, "already registered as project"):
            self.register(project_id="second-project")

    def test_rejects_invalid_project_id(self) -> None:
        for project_id in ("Bad_ID", "x", "-bad", "has_underscore"):
            with self.assertRaisesRegex(ValueError, "project_id"):
                self.register(project_id=project_id)

    def test_rejects_missing_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.register(repo_root=self.root / "missing")

    def test_rejects_non_git_directory(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        with self.assertRaisesRegex(ValueError, "not a Git repository"):
            self.register(repo_root=plain)

    def test_rejects_git_subdirectory(self) -> None:
        subdirectory = self.repo / "sub"
        subdirectory.mkdir()
        with self.assertRaisesRegex(ValueError, "Git top level"):
            self.register(repo_root=subdirectory)

    def test_rejects_empty_write_roots_for_write_profile(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved write root"):
            self.register(profile="develop", markdown_write_roots=())

    def test_untrusted_registration_requires_containment(self) -> None:
        with self.assertRaisesRegex(ValueError, UNTRUSTED_CONTAINMENT_ERROR):
            self.register(containment_available=False)

    def test_trusted_registration_stays_contained_by_default(self) -> None:
        result = self.register(trust_mode="trusted")
        self.assertTrue(result["registered"])
        self.assertEqual(result["trust_mode"], "trusted")
        self.assertEqual(result["containment_mode"], "bwrap")
        # Trusted mode only relaxes the containment *requirement*; without an
        # explicit uncontained override (not offered at registration time) an
        # unavailable containment still fails closed.
        with self.assertRaisesRegex(ValueError, "containment is unavailable"):
            self.register(trust_mode="trusted", containment_available=False)

    def test_registration_never_executes_verification_commands(self) -> None:
        marker = self.repo / "side-effect-marker.txt"
        result = self.register(
            profile="develop-verify",
            verification_commands=(
                {
                    "id": "side-effect",
                    "argv": [
                        "{python}",
                        "-c",
                        (
                            "import pathlib; "
                            "pathlib.Path('side-effect-marker.txt')"
                            ".write_text('executed')"
                        ),
                    ],
                },
            ),
        )
        self.assertEqual(result["verification_commands_executed"], 0)
        self.assertEqual(result["verification_commands_recorded"], 1)
        self.assertFalse(marker.exists())

    def test_rejects_shell_verification_command(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not invoke a shell"):
            self.register(
                profile="develop-verify",
                verification_commands=(
                    {"id": "shell", "argv": ["bash", "-c", "ls"]},
                ),
            )

    def test_rejects_duplicate_verification_command_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "verification command IDs must be unique"):
            self.register(
                profile="develop-verify",
                verification_commands=(_core_command(), _core_command()),
            )

    def test_warns_on_dirty_repository_without_touching_it(self) -> None:
        untracked = self.repo / "local-notes.md"
        untracked.write_text("draft\n", encoding="utf-8")
        result = self.register()
        self.assertFalse(result["repo_is_clean"])
        self.assertTrue(any("dirty" in warning for warning in result["warnings"]))
        self.assertTrue(untracked.exists())
        self.assertEqual(untracked.read_text(encoding="utf-8"), "draft\n")

    def test_registration_preserves_existing_projects(self) -> None:
        other = self.root / "other-repo"
        initialize_git_repository(other)
        self.register()
        self.register(project_id="second-project", repo_root=other)
        document = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(
            [project["id"] for project in document["projects"]],
            ["test-project", "second-project"],
        )
        report = validate_projects_config(self.config)
        self.assertTrue(report["valid"])
        self.assertEqual(report["project_count"], 2)

    def test_registration_creates_missing_config_directory(self) -> None:
        nested = self.root / "nested" / "projects.json"
        result = self.register(config_path=nested)
        self.assertTrue(result["registered"])
        self.assertTrue(nested.exists())

    def test_rejects_symlink_repository(self) -> None:
        symlink_repo = self.root / "repo-symlink"
        symlink_repo.symlink_to(self.repo)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.register(repo_root=symlink_repo)

    def test_rejects_nested_repository_root(self) -> None:
        self.register()
        nested_repo = self.repo / "nested-git"
        initialize_git_repository(nested_repo)
        with self.assertRaisesRegex(ValueError, "nested inside"):
            self.register(project_id="nested-project", repo_root=nested_repo)

    def test_rejects_overlapping_enclosing_repository_root(self) -> None:
        nested_repo = self.repo / "nested-git"
        initialize_git_repository(nested_repo)
        self.register(project_id="nested-project", repo_root=nested_repo)
        with self.assertRaisesRegex(ValueError, "overlaps and encloses"):
            self.register(project_id="parent-project", repo_root=self.repo)

    def test_rejects_malicious_verification_commands(self) -> None:
        with self.assertRaisesRegex(ValueError, "shell execution"):
            self.register(
                profile="develop-verify",
                verification_commands=(
                    {"id": "shell-true", "argv": ["{python}", "-m", "unittest"], "shell": True},
                ),
            )
        with self.assertRaisesRegex(ValueError, "invoke a shell"):
            self.register(
                profile="develop-verify",
                verification_commands=(
                    {"id": "bash-c", "argv": ["bash", "-c", "echo pwned"]},
                ),
            )
        with self.assertRaisesRegex(ValueError, r"\.\."):
            self.register(
                profile="develop-verify",
                verification_commands=(
                    {"id": "traverse", "argv": ["{python}", "../evil.py"]},
                ),
            )
        with self.assertRaisesRegex(ValueError, "secret key"):
            self.register(
                profile="develop-verify",
                verification_commands=(
                    {
                        "id": "secret-env",
                        "argv": ["{python}", "-m", "unittest"],
                        "environment": {"API_KEY": "supersecret"},
                    },
                ),
            )

    def test_rejects_read_only_config_directory(self) -> None:
        readonly_dir = self.root / "readonly-dir"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o500)
        try:
            with self.assertRaises((PermissionError, OSError)):
                self.register(config_path=readonly_dir / "projects.json")
        finally:
            readonly_dir.chmod(0o700)


class ShowProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        initialize_git_repository(self.repo)
        self.config = self.root / "projects.json"
        register_project(
            config_path=self.config,
            project_id="test-project",
            repo_root=self.repo,
            profile="develop-verify",
            verification_commands=({"id": "core", "argv": ["{python}", "-m", "unittest"]},),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_shows_registered_project(self) -> None:
        shown = show_project(self.config, "test-project")
        self.assertEqual(shown["config_path"], str(self.config))
        self.assertEqual(shown["project_id"], "test-project")
        self.assertEqual(shown["project"]["id"], "test-project")
        self.assertTrue(shown["policy_valid"])
        self.assertIsNone(shown["validation_error"])
        self.assertEqual(shown["project"]["verification_commands"][0]["id"], "core")

    def test_show_unknown_project_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "not registered"):
            show_project(self.config, "missing-project")

    def test_show_missing_config_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "project registry not found"):
            show_project(self.root / "absent.json", "test-project")

    def test_show_reports_invalid_entry_without_raising(self) -> None:
        write_registry(
            self.config,
            [
                project_payload(
                    self.repo,
                    capabilities=["repository_read", "markdown_write"],
                    markdown_write_roots=[],
                )
            ],
        )
        shown = show_project(self.config, "test-project")
        self.assertFalse(shown["policy_valid"])
        self.assertIn("approved write root", shown["validation_error"])


class ValidateProjectsConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        initialize_git_repository(self.repo)
        self.config = self.root / "projects.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_validates_a_registered_config(self) -> None:
        register_project(
            config_path=self.config,
            project_id="test-project",
            repo_root=self.repo,
            profile="reviewed-development",
            verification_commands=({"id": "core", "argv": ["{python}", "-m", "unittest"]},),
        )
        report = validate_projects_config(self.config)
        self.assertTrue(report["valid"])
        self.assertEqual(report["project_count"], 1)
        self.assertEqual(report["enabled_project_count"], 1)
        self.assertEqual(report["project_ids"], ["test-project"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(
            report["projects"],
            [{"id": "test-project", "enabled": True, "valid": True, "errors": []}],
        )

    def test_missing_config_is_reported_not_raised(self) -> None:
        report = validate_projects_config(self.root / "absent.json")
        self.assertFalse(report["valid"])
        self.assertTrue(any("not found" in error for error in report["errors"]))

    def test_invalid_json_is_reported(self) -> None:
        self.config.write_text("not json at all", encoding="utf-8")
        report = validate_projects_config(self.config)
        self.assertFalse(report["valid"])
        self.assertTrue(any("invalid JSON" in error for error in report["errors"]))

    def test_policy_violating_entry_is_pinpointed(self) -> None:
        write_registry(
            self.config,
            [
                project_payload(
                    self.repo,
                    capabilities=["repository_read", "markdown_write"],
                    markdown_write_roots=[],
                )
            ],
        )
        report = validate_projects_config(self.config)
        self.assertFalse(report["valid"])
        self.assertTrue(report["errors"])
        self.assertEqual(len(report["projects"]), 1)
        self.assertFalse(report["projects"][0]["valid"])
        self.assertTrue(
            any("approved write root" in error for error in report["projects"][0]["errors"])
        )

    def test_registry_without_enabled_projects_is_reported(self) -> None:
        write_registry(self.config, [project_payload(self.repo, enabled=False)])
        report = validate_projects_config(self.config)
        self.assertFalse(report["valid"])
        self.assertTrue(any("no enabled projects" in error for error in report["errors"]))
        self.assertEqual(report["enabled_project_count"], 0)

    def test_shared_roots_are_reported(self) -> None:
        write_registry(
            self.config,
            [
                project_payload(self.repo, project_id="project-one"),
                project_payload(self.repo, project_id="project-two"),
            ],
        )
        report = validate_projects_config(self.config)
        self.assertFalse(report["valid"])
        self.assertTrue(any("share a root" in error for error in report["errors"]))

    def test_unsupported_document_version_is_reported(self) -> None:
        self.config.write_text(
            json.dumps({"version": 2, "projects": [project_payload(self.repo)]}),
            encoding="utf-8",
        )
        report = validate_projects_config(self.config)
        self.assertFalse(report["valid"])
        self.assertTrue(report["errors"])

    def test_nonexistent_project_root_is_reported(self) -> None:
        write_registry(
            self.config,
            [project_payload(self.root / "missing-repo")],
        )
        report = validate_projects_config(self.config)
        self.assertFalse(report["valid"])
        self.assertTrue(any("does not exist" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
