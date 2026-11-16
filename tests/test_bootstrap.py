"""Tests for bootstrap and BridgeLab configuration import.

Phase 3 Operator Experience (M3.2 / A3.2.1):
Covers safe initialization, BridgeLab import, dry-run mode, backup creation,
rollback, atomic activation, permissions, and conflict rejection.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_dev_mcp.bootstrap import (
    CONFIG_SCHEMA_VERSION,
    BootstrapConflictError,
    BootstrapConflictItem,
    BootstrapFileRecord,
    BootstrapReport,
    init_conrad_installation,
    rollback_conrad_config,
)
from local_dev_mcp.models import ProjectCapability
from local_dev_mcp.registry import ProjectRegistry

from tests.support import initialize_git_repository, project_payload


class BootstrapInstallationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config_path = self.root / "projects.json"
        self.state_dir = self.root / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fresh_init_creates_state_dir_and_empty_config(self) -> None:
        report = init_conrad_installation(
            config_path=self.config_path,
            state_dir=self.state_dir,
            import_from=None,
            dry_run=False,
        )

        self.assertTrue(report.ok)
        self.assertFalse(report.dry_run)
        self.assertEqual(report.config_schema_version, CONFIG_SCHEMA_VERSION)
        self.assertEqual(report.config_path, str(self.config_path))
        self.assertEqual(report.state_dir, str(self.state_dir))
        self.assertEqual(report.imported_project_ids, ())
        self.assertEqual(report.conflicts, ())

        # Verify state_dir mode is 0o700
        self.assertTrue(self.state_dir.is_dir())
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)

        # Verify config_path mode is 0o600
        self.assertTrue(self.config_path.is_file())
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)

        # Verify config contents
        content = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(content, {"version": 1, "projects": []})

        # Verify file record
        self.assertEqual(len(report.files), 1)
        record = report.files[0]
        self.assertEqual(record.path, str(self.config_path))
        self.assertEqual(record.mode_octal, "0600")
        self.assertEqual(record.action, "created")
        self.assertTrue(record.sha256)

    def test_repeat_init_without_backup_fails_closed(self) -> None:
        # First init
        init_conrad_installation(
            config_path=self.config_path,
            state_dir=self.state_dir,
            dry_run=False,
        )
        original_bytes = self.config_path.read_bytes()

        # Repeat init without allow_existing_with_backup must fail closed
        with self.assertRaises(BootstrapConflictError) as cm:
            init_conrad_installation(
                config_path=self.config_path,
                state_dir=self.state_dir,
                allow_existing_with_backup=False,
            )

        self.assertIn("already exists", str(cm.exception))
        self.assertTrue(any(c.field == "config_path" for c in cm.exception.conflicts))

        # Config remains byte-for-byte identical
        self.assertEqual(self.config_path.read_bytes(), original_bytes)

    def test_dry_run_zero_filesystem_writes(self) -> None:
        # Fresh dry-run
        report = init_conrad_installation(
            config_path=self.config_path,
            state_dir=self.state_dir,
            dry_run=True,
        )

        self.assertTrue(report.ok)
        self.assertTrue(report.dry_run)
        self.assertFalse(self.config_path.exists())
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(len(report.files), 1)
        self.assertEqual(report.files[0].path, str(self.config_path))
        self.assertEqual(report.files[0].mode_octal, "0600")

    def test_dry_run_detects_collision_without_backup_flag(self) -> None:
        # Write initial file
        self.config_path.write_text("existing", encoding="utf-8")

        report = init_conrad_installation(
            config_path=self.config_path,
            state_dir=self.state_dir,
            dry_run=True,
            allow_existing_with_backup=False,
        )

        self.assertFalse(report.ok)
        self.assertTrue(report.dry_run)
        self.assertTrue(any(c.field == "config_path" for c in report.conflicts))
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), "existing")

    def test_import_valid_projects_json(self) -> None:
        repo_dir = self.root / "repo"
        initialize_git_repository(repo_dir)

        payload = project_payload(
            repo_dir,
            project_id="test-proj",
            capabilities=["repository_read", "claude_edit"],
        )
        import_file = self.root / "legacy_projects.json"
        import_file.write_text(json.dumps({"version": 1, "projects": [payload]}), encoding="utf-8")

        report = init_conrad_installation(
            config_path=self.config_path,
            state_dir=self.state_dir,
            import_from=import_file,
            dry_run=False,
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.imported_project_ids, ("test-proj",))
        self.assertEqual(report.diff_summary["added_projects"], ["test-proj"])
        self.assertEqual(
            report.diff_summary["capabilities_by_project"]["test-proj"],
            ["claude_edit", "repository_read"],
        )

        # Ensure ProjectRegistry.load and ProjectRegistry.from_file accept the written file
        registry = ProjectRegistry.load(self.config_path)
        self.assertIsNotNone(registry.resolve("test-proj", ProjectCapability.CLAUDE_EDIT))

        registry_from_file = getattr(ProjectRegistry, "from_file")(self.config_path)
        self.assertIsNotNone(registry_from_file.resolve("test-proj", ProjectCapability.REPOSITORY_READ))

    def test_import_rejects_embedded_secrets(self) -> None:
        repo_dir = self.root / "repo"
        initialize_git_repository(repo_dir)

        # Include forbidden secret-like keys
        payload = project_payload(repo_dir, project_id="secret-proj")
        payload["env"] = {"OPENAI_API_KEY": "sk-secret123"}

        import_file = self.root / "secret_projects.json"
        import_file.write_text(json.dumps({"projects": [payload]}), encoding="utf-8")

        with self.assertRaises(BootstrapConflictError) as cm:
            init_conrad_installation(
                config_path=self.config_path,
                state_dir=self.state_dir,
                import_from=import_file,
                dry_run=False,
            )

        self.assertTrue(any("OPENAI_API_KEY" in c.field for c in cm.exception.conflicts))
        # Ensure target file was never created
        self.assertFalse(self.config_path.exists())

    def test_import_rejects_nonexistent_or_relative_root(self) -> None:
        # Relative root
        rel_payload = {
            "id": "rel-proj",
            "display_name": "Relative",
            "description": "Relative root project",
            "root": "relative/path/to/repo",
            "capabilities": ["repository_read"],
        }
        # Non-existent root
        nonexist_payload = {
            "id": "nonexist-proj",
            "display_name": "Nonexistent",
            "description": "Nonexistent root project",
            "root": str(self.root / "does_not_exist"),
            "capabilities": ["repository_read"],
        }
        import_file = self.root / "bad_roots.json"
        import_file.write_text(json.dumps({"projects": [rel_payload, nonexist_payload]}), encoding="utf-8")

        with self.assertRaises(BootstrapConflictError) as cm:
            init_conrad_installation(
                config_path=self.config_path,
                state_dir=self.state_dir,
                import_from=import_file,
                dry_run=False,
            )

        self.assertTrue(any(c.field == "root" and "relative" in c.reason for c in cm.exception.conflicts))
        self.assertTrue(any(c.field == "root" and "does not exist" in c.reason for c in cm.exception.conflicts))
        self.assertFalse(self.config_path.exists())

    def test_import_rejects_duplicate_project_ids_and_shared_roots(self) -> None:
        repo_dir = self.root / "repo"
        initialize_git_repository(repo_dir)

        payload1 = project_payload(repo_dir, project_id="dup-id")
        payload2 = project_payload(repo_dir, project_id="dup-id")

        import_file = self.root / "dup.json"
        import_file.write_text(json.dumps({"projects": [payload1, payload2]}), encoding="utf-8")

        with self.assertRaises(BootstrapConflictError) as cm:
            init_conrad_installation(
                config_path=self.config_path,
                state_dir=self.state_dir,
                import_from=import_file,
                dry_run=False,
            )

        self.assertTrue(any(c.field == "id" and "Duplicate" in c.reason for c in cm.exception.conflicts))
        self.assertFalse(self.config_path.exists())

    def test_import_rejects_broken_capability_dependencies(self) -> None:
        repo_dir = self.root / "repo"
        initialize_git_repository(repo_dir)

        # verification_commit requires claude_edit
        broken_payload = project_payload(
            repo_dir,
            project_id="broken-cap",
            capabilities=["verification_commit"],
        )
        import_file = self.root / "broken_cap.json"
        import_file.write_text(json.dumps({"projects": [broken_payload]}), encoding="utf-8")

        with self.assertRaises(BootstrapConflictError) as cm:
            init_conrad_installation(
                config_path=self.config_path,
                state_dir=self.state_dir,
                import_from=import_file,
                dry_run=False,
            )

        self.assertTrue(any("verification_commit requires claude_edit" in c.reason for c in cm.exception.conflicts))
        self.assertFalse(self.config_path.exists())

    def test_backup_and_rollback_restores_exact_sha256_and_mode(self) -> None:
        # Create initial config
        repo_dir = self.root / "repo"
        initialize_git_repository(repo_dir)
        payload1 = project_payload(repo_dir, project_id="proj-v1")
        initial_file = self.root / "initial.json"
        initial_file.write_text(json.dumps({"version": 1, "projects": [payload1]}), encoding="utf-8")

        report1 = init_conrad_installation(
            config_path=self.config_path,
            state_dir=self.state_dir,
            import_from=initial_file,
            dry_run=False,
        )
        self.assertTrue(report1.ok)
        original_bytes = self.config_path.read_bytes()
        original_sha = report1.files[0].sha256

        # Now overwrite with allow_existing_with_backup=True
        repo_dir2 = self.root / "repo2"
        initialize_git_repository(repo_dir2)
        payload2 = project_payload(repo_dir2, project_id="proj-v2")
        second_file = self.root / "second.json"
        second_file.write_text(json.dumps({"version": 1, "projects": [payload2]}), encoding="utf-8")

        report2 = init_conrad_installation(
            config_path=self.config_path,
            state_dir=self.state_dir,
            import_from=second_file,
            allow_existing_with_backup=True,
            dry_run=False,
        )
        self.assertTrue(report2.ok)
        self.assertIsNotNone(report2.backup_path)
        self.assertIsNotNone(report2.backup_manifest_path)
        self.assertIn("proj-v2", report2.imported_project_ids)

        # Rollback using the manifest
        assert report2.backup_manifest_path is not None
        rollback_report = rollback_conrad_config(report2.backup_manifest_path)

        self.assertTrue(rollback_report.ok)
        self.assertEqual(self.config_path.read_bytes(), original_bytes)
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(rollback_report.files[0].sha256, original_sha)
        self.assertEqual(rollback_report.imported_project_ids, ("proj-v1",))

    def test_rollback_detects_tampered_or_missing_backup(self) -> None:
        # Create initial config and backup
        init_conrad_installation(
            config_path=self.config_path,
            state_dir=self.state_dir,
            dry_run=False,
        )
        report = init_conrad_installation(
            config_path=self.config_path,
            state_dir=self.state_dir,
            allow_existing_with_backup=True,
            dry_run=False,
        )
        manifest_path = Path(report.backup_manifest_path or "")
        backup_path = Path(report.backup_path or "")

        # Tampered backup test
        backup_path.write_text("tampered content", encoding="utf-8")
        with self.assertRaises(BootstrapConflictError) as cm:
            rollback_conrad_config(manifest_path)
        self.assertIn("tampered with or corrupted", str(cm.exception))

        # Missing backup file test
        backup_path.unlink()
        with self.assertRaises(BootstrapConflictError) as cm:
            rollback_conrad_config(manifest_path)
        self.assertIn("not found", str(cm.exception))

        # Missing manifest file test
        manifest_path.unlink()
        with self.assertRaises(BootstrapConflictError) as cm:
            rollback_conrad_config(manifest_path)
        self.assertIn("not found", str(cm.exception))

    def test_simulated_write_failure_leaves_target_untouched(self) -> None:
        # Create an existing config
        self.config_path.write_text("original content", encoding="utf-8")
        os.chmod(self.config_path, 0o600)

        # Simulate os.replace failure
        with patch("os.replace", side_effect=OSError("Simulated I/O failure")):
            with self.assertRaises(BootstrapConflictError):
                init_conrad_installation(
                    config_path=self.config_path,
                    state_dir=self.state_dir,
                    allow_existing_with_backup=True,
                    dry_run=False,
                )

        # Target file is untouched
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), "original content")


if __name__ == "__main__":
    unittest.main()
