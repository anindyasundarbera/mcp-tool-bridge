"""M-GV-01: Durable per-action authorization objects.

Tests exercise the AuthorizationManager: issue -> check -> consume (single-use),
reuse rejection, expiry rejection, target/capability mismatch rejection, and
delegation-chain recording. Uses a temp state DB (no network).
"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from local_dev_mcp.authorization import (
    AuthorizationError,
    AuthorizationManager,
)
from local_dev_mcp.config import Settings


def settings(base: Path) -> Settings:
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
        }
    )


class AuthorizationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.settings = settings(self.base)
        self.manager = AuthorizationManager(self.settings)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_issue_check_consume_single_use(self) -> None:
        authorization = self.manager.issue(
            "test-project", "commit", "abc123", job_id="j1"
        )
        checked = self.manager.check(
            authorization.authorization_id,
            project_id="test-project",
            capability="commit",
            target="abc123",
        )
        self.assertEqual(checked.authorization_id, authorization.authorization_id)
        self.assertFalse(checked.used)

        self.manager.consume(authorization.authorization_id)
        with self.assertRaisesRegex(AuthorizationError, "already been used"):
            self.manager.check(
                authorization.authorization_id,
                project_id="test-project",
                capability="commit",
                target="abc123",
            )

    def test_different_target_rejected(self) -> None:
        authorization = self.manager.issue("test-project", "commit", "abc123")
        with self.assertRaisesRegex(AuthorizationError, "different target"):
            self.manager.check(
                authorization.authorization_id,
                project_id="test-project",
                capability="commit",
                target="def456",
            )

    def test_different_capability_rejected(self) -> None:
        authorization = self.manager.issue("test-project", "commit", "abc123")
        with self.assertRaisesRegex(AuthorizationError, "different capability"):
            self.manager.check(
                authorization.authorization_id,
                project_id="test-project",
                capability="push",
                target="abc123",
            )

    def test_different_project_rejected(self) -> None:
        authorization = self.manager.issue("test-project", "commit", "abc123")
        with self.assertRaisesRegex(AuthorizationError, "different project"):
            self.manager.check(
                authorization.authorization_id,
                project_id="other-project",
                capability="commit",
                target="abc123",
            )

    def test_expired_authorization_rejected(self) -> None:
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        authorization = self.manager.issue(
            "test-project", "commit", "abc123", expires_at=past
        )
        with self.assertRaisesRegex(AuthorizationError, "expired"):
            self.manager.check(
                authorization.authorization_id,
                project_id="test-project",
                capability="commit",
                target="abc123",
            )

    def test_unknown_authorization_rejected(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "unknown authorization_id"):
            self.manager.check(
                "0" * 32,
                project_id="test-project",
                capability="commit",
                target="abc123",
            )

    def test_delegation_chain_records_delegated_from(self) -> None:
        parent = self.manager.issue("test-project", "commit", "abc123")
        child = self.manager.issue(
            "test-project",
            "commit",
            "abc123",
            delegated_from=parent.authorization_id,
        )
        self.assertEqual(child.delegated_from, parent.authorization_id)
        # The child is independently checkable and consumable.
        self.manager.check(
            child.authorization_id,
            project_id="test-project",
            capability="commit",
            target="abc123",
        )

    def test_get_returns_record(self) -> None:
        authorization = self.manager.issue("test-project", "commit", "abc123")
        fetched = self.manager.get(authorization.authorization_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.authorization_id, authorization.authorization_id)
        self.assertIsNone(self.manager.get("0" * 32))

    def test_check_and_consume_is_atomic_single_use(self) -> None:
        authorization = self.manager.issue("test-project", "commit", "abc123")
        consumed = self.manager.check_and_consume(
            authorization.authorization_id,
            project_id="test-project",
            capability="commit",
            target="abc123",
        )
        self.assertEqual(consumed.authorization_id, authorization.authorization_id)
        # A second check_and_consume must fail: the atomic guard already used it.
        with self.assertRaisesRegex(AuthorizationError, "already been used"):
            self.manager.check_and_consume(
                authorization.authorization_id,
                project_id="test-project",
                capability="commit",
                target="abc123",
            )

    def test_consume_twice_rejected(self) -> None:
        authorization = self.manager.issue("test-project", "commit", "abc123")
        self.manager.consume(authorization.authorization_id)
        with self.assertRaisesRegex(AuthorizationError, "already been used"):
            self.manager.consume(authorization.authorization_id)


if __name__ == "__main__":
    unittest.main()
