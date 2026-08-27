"""M-EV-01: requirement traceability — parse, matrix, deferrals, contradictions,
link round-trip, and reload re-hydration.

These tests exercise the RequirementManager with a fixture spec containing
normative + example blocks. No real Claude, no network — the reader scans a
local temporary git repository. SCHEMA_VERSION stays 1; no new MCP tool is
added.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from local_dev_mcp import store as store_module
from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.reader import RepositoryReader
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.requirement import RequirementManager
from local_dev_mcp.store import open_state_db

from tests.support import initialize_git_repository
from tests.test_jobs import ImmediateExecutor, policy as job_policy, settings as job_settings

import subprocess


def commit_spec(root: Path) -> None:
    """Stage and commit the spec file so the working tree is clean."""
    subprocess.run(["git", "-C", str(root), "add", "SPEC.md"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "Add spec"],
        check=True,
    )


def requirement_settings(base: Path) -> Settings:
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
            "CANDIDATE_MCP_PORT": "9101",
        }
    )


def write_spec(root: Path) -> None:
    """Write a fixture spec with normative + example + contradictory + deferral blocks."""
    spec = """# Specification

## Normative Requirements

```normative
CKS-002B-R1 The system must persist requirements additively.
CKS-002B-R2 The system must not persist requirements destructively.
CKS-002B-R3 The system shall classify requirements as normative or example.
```

## Examples

Note: CKS-002B-R1 is an example of how requirements look — for demonstration.

CKS-002B-R4 The repository may optionally support tagged releases.
"""
    (root / "SPEC.md").write_text(spec, encoding="utf-8")


class RequirementParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        write_spec(self.root)
        self.settings = requirement_settings(self.base)
        self.project = job_policy(self.root)
        self._db_path = self.settings.job_worktree_root / ".state" / "bridge.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manager(self) -> RequirementManager:
        reader = RepositoryReader(self.settings)
        state_root = self.settings.job_worktree_root / ".state"
        state_root.mkdir(parents=True, exist_ok=True)
        return RequirementManager(
            self.settings, store_module, reader, db_path=self._db_path
        )

    def test_parse_extracts_normative_and_example(self) -> None:
        async def exercise() -> None:
            manager = self._manager()
            reqs = await manager.parse(self.project)
            ids = {r.requirement_id for r in reqs}
            self.assertIn("CKS-002B-R1", ids)
            self.assertIn("CKS-002B-R2", ids)
            self.assertIn("CKS-002B-R3", ids)
            self.assertIn("CKS-002B-R4", ids)
            # R1 is in a normative block → normative
            r1 = next(r for r in reqs if r.requirement_id == "CKS-002B-R1")
            self.assertTrue(r1.normative)
            self.assertEqual(r1.kind, "normative")
            # R4 is outside a normative block in the "Examples" section with
            # "example" in the surrounding context → classified as example.
            r4 = next(r for r in reqs if r.requirement_id == "CKS-002B-R4")
            self.assertFalse(r4.normative)
            self.assertEqual(r4.kind, "example")

        asyncio.run(exercise())

    def test_matrix_marks_unimplemented_as_uncovered(self) -> None:
        async def exercise() -> None:
            manager = self._manager()
            await manager.parse(self.project)
            matrix = await manager.matrix(self.project)
            self.assertGreater(matrix["normative"], 0)
            self.assertEqual(matrix["covered"], 0)
            self.assertGreater(matrix["uncovered"], 0)
            self.assertEqual(matrix["deferred"], 0)

        asyncio.run(exercise())

    def test_deferral_recorded_with_authority(self) -> None:
        async def exercise() -> None:
            manager = self._manager()
            await manager.parse(self.project)
            # Defer a normative requirement (R1 is in the normative block).
            manager.record_deferral("CKS-002B-R1", "chatgpt:2026-08-26")
            matrix = await manager.matrix(self.project)
            self.assertGreater(matrix["deferred"], 0)
            details = [d for d in matrix["details"] if d["requirement_id"] == "CKS-002B-R1"]
            self.assertEqual(len(details), 1)
            self.assertEqual(details[0]["status"], "deferred")
            self.assertEqual(details[0]["deferred_authority"], "chatgpt:2026-08-26")

        asyncio.run(exercise())

    def test_contradictory_pair_flagged(self) -> None:
        """Two normative requirements: one asserts, the other negates, sharing a keyword."""
        async def exercise() -> None:
            spec = """# Spec

```normative
CKS-001-R1 The bridge must encrypt the session channel.
CKS-001-R2 The bridge must not encrypt the session channel.
```
"""
            (self.root / "CONTRADICT.md").write_text(spec, encoding="utf-8")
            manager = self._manager()
            reqs = await manager.parse(self.project)
            contradictions = RequirementManager.detect_contradictions(reqs)
            self.assertGreater(len(contradictions), 0)
            pair_ids = {
                contradictions[0].requirement_id_a,
                contradictions[0].requirement_id_b,
            }
            self.assertEqual(pair_ids, {"CKS-001-R1", "CKS-001-R2"})

        asyncio.run(exercise())

    def test_link_upsert_round_trips(self) -> None:
        """Link round-trip via a real job (FK to jobs(job_id) requires a job row)."""
        async def exercise() -> None:
            # Ensure the spec is committed so the worktree can be created.
            commit_spec(self.root)
            executor = ImmediateExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            # Start a job so a job row exists, then use its job_id for links.
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            # Wait for completion so persist_links_for_candidate was called.
            for _ in range(200):
                snapshot = await manager.get(self.project, job_id)
                if str(snapshot["status"]) == "completed":
                    break
                await asyncio.sleep(0.01)
            # Now verify links exist via the requirement manager.
            req_manager = manager._requirements_manager()
            links = req_manager.get_links_for_job(self.project.id, job_id)
            # Each normative requirement gets a 'job' link + a 'changed_path' link.
            job_links = [l for l in links if l.link_type == "job"]
            path_links = [l for l in links if l.link_type == "changed_path"]
            self.assertGreater(len(job_links), 0)
            self.assertGreater(len(path_links), 0)
            # Re-persist does not duplicate (idempotent).
            req_manager.persist_links_for_candidate(
                self.project, job_id, ("candidate.txt",)
            )
            links2 = req_manager.get_links_for_job(self.project.id, job_id)
            self.assertEqual(len(links), len(links2))

        asyncio.run(exercise())

    def test_reload_re_hydrates(self) -> None:
        async def exercise() -> None:
            manager = self._manager()
            await manager.parse(self.project)
            manager.record_deferral("CKS-002B-R1", "chatgpt:2026-08-26")
            # A fresh manager on the same DB re-hydrates the persisted state.
            manager2 = self._manager()
            reqs = manager2.get_requirements_for_project(self.project.id)
            ids = {r.requirement_id for r in reqs}
            self.assertIn("CKS-002B-R1", ids)
            r1 = next(r for r in reqs if r.requirement_id == "CKS-002B-R1")
            self.assertEqual(r1.status, "deferred")
            self.assertEqual(r1.deferred_authority, "chatgpt:2026-08-26")

        asyncio.run(exercise())

    def test_schema_version_stays_one(self) -> None:
        # Ensure the state DB exists by creating a manager.
        manager = self._manager()
        manager.close()
        conn = open_state_db(self._db_path)
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        self.assertEqual(row["value"], "1")
        conn.close()


class RequirementJobWiringTests(unittest.TestCase):
    """Verify RequirementManager is wired into the job lifecycle."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        write_spec(self.root)
        commit_spec(self.root)
        self.settings = requirement_settings(self.base)
        self.project = job_policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_job_start_scans_requirements(self) -> None:
        """A started job triggers a requirement scan, persisting requirements."""
        async def exercise() -> None:
            executor = ImmediateExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            rows = manager._db.execute(
                "SELECT * FROM requirements WHERE project_id = ?",
                (self.project.id,),
            ).fetchall()
            self.assertGreater(len(rows), 0)
            ids = {row["requirement_id"] for row in rows}
            self.assertIn("CKS-002B-R1", ids)

        asyncio.run(exercise())

    def test_snapshot_surfaces_requirements_covered(self) -> None:
        """The terminal snapshot includes a requirements_covered summary."""
        async def exercise() -> None:
            executor = ImmediateExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            for _ in range(200):
                snapshot = await manager.get(self.project, job_id)
                if str(snapshot["status"]) == "completed":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(str(snapshot["status"]), "completed")
            self.assertIn("requirements_covered", snapshot)
            rc = snapshot["requirements_covered"]
            self.assertGreater(rc["normative"], 0)
            # The candidate wrote candidate.txt, which is a changed path.
            # After completion, links are persisted, so covered > 0.
            self.assertGreaterEqual(rc["covered"], 1)

        asyncio.run(exercise())


class RequirementToolSurfaceTests(unittest.TestCase):
    """Zero-new-surface discipline: no new MCP tool was added."""

    def test_requirement_adds_no_new_mcp_tools(self) -> None:
        from fastmcp import FastMCP

        from local_dev_mcp.server import create_server

        async def exercise() -> None:
            temporary = tempfile.TemporaryDirectory()
            base = Path(temporary.name)
            root = base / "repo"
            initialize_git_repository(root)
            (root / "SPEC.md").write_text(
                "# Spec\n```normative\nTEST-R1 must work.\n```\n",
                encoding="utf-8",
            )
            from tests.support import project_payload, write_registry

            write_registry(
                base / "projects.json",
                [project_payload(root, capabilities=["repository_read", "claude_edit"])],
            )
            s = Settings.from_env(
                {
                    "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
                    "JOB_WORKTREE_ROOT": str(base / "jobs"),
                    "CANDIDATE_MCP_PORT": "9102",
                }
            )
            mcp = create_server(settings=s)
            tools = {tool.name: tool for tool in await mcp.list_tools()}
            tool_names = set(tools.keys())
            temporary.cleanup()
            self.assertEqual(len(tool_names), 33)
            self.assertNotIn("get_requirement_coverage", tool_names)
            self.assertNotIn("export_requirement_matrix", tool_names)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()