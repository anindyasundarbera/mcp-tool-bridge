"""Continuity release: durable checkpoints, paused states, soft/multi-dimensional
budgets, progress-gated extensions, and the structured terminal report.

These tests exercise the new job-lifecycle behavior with fake executors (no
real Claude, no network), mirroring the patterns in ``tests/test_jobs.py`` and
``tests/test_persistence.py``.
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager, JobStatus
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.store import (
    checkpoint_count,
    gc_artifacts,
    insert_checkpoint_row,
    load_checkpoint_detail_for_job,
    load_checkpoint_detail_rows,
    load_checkpoint_rows,
    open_state_db,
)

from tests.support import initialize_git_repository
from tests.test_jobs import (
    BlockingExecutor,
    ImmediateExecutor,
    policy as job_policy,
    settings as job_settings,
    wait_for_status,
)


def continuity_settings(base: Path, **env: str) -> Settings:
    payload = {
        "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
        "JOB_WORKTREE_ROOT": str(base / "jobs"),
        "CANDIDATE_MCP_PORT": "9101",
    }
    payload.update(env)
    return Settings.from_env(payload)


class MaxTurnsProgressExecutor:
    """Each call writes a new file (changed set grows) and returns a max_turns
    terminal reason with a resumable session and a fixed turn count.

    Models a drafter that keeps advancing but keeps hitting its per-slice turn
    limit — the condition that makes a progress-gated extension eligible.
    """

    def __init__(self, num_turns: int = 30) -> None:
        self.calls: list[dict[str, Any]] = []
        self.num_turns = num_turns

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        n = len(self.calls) + 1
        self.calls.append(
            {
                "root": root,
                "text": specification,
                "resume_session_id": resume_session_id,
                "max_turns": max_turns,
            }
        )
        (root / f"step{n}.txt").write_text(f"step {n}\n", encoding="utf-8")
        return {
            "status": "failed",
            "terminal_reason": "max_turns",
            "num_turns": self.num_turns,
            "session_id": "bridge-session-progress",
            "result": f"slice {n}",
        }


class StalledExecutor:
    """Writes nothing and blocks briefly so the progress sampler can observe a
    stalled (empty) changed set, then returns a max_turns slice."""

    def __init__(self, delay: float = 0.3, num_turns: int = 5) -> None:
        self.delay = delay
        self.num_turns = num_turns
        self.calls = 0

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        self.calls += 1
        await asyncio.sleep(self.delay)
        return {
            "status": "failed",
            "terminal_reason": "max_turns",
            "num_turns": self.num_turns,
            "session_id": "bridge-session-stalled",
            "result": "stalled",
        }


class UsageExecutor:
    """Returns a completed slice with token/turn usage so the budget counters
    can be exercised deterministically."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        self.calls.append({"root": root, "resume_session_id": resume_session_id})
        (root / "candidate.txt").write_text("done\n", encoding="utf-8")
        return {
            "status": "completed",
            "session_id": "bridge-session-usage",
            "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 25},
            "num_turns": 3,
            "result": "done",
        }


class StreamJsonInputRequiredExecutor:
    """M-CT-05: returns a non-completed slice carrying a stream-json
    ``input_required`` event, so the job transitions to
    BLOCKED_INPUT_REQUIRED (resumable) via the real ``_run`` loop."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        self.calls.append({"root": root, "resume_session_id": resume_session_id})
        return {
            "status": "failed",
            "terminal_reason": "input_required",
            "num_turns": 2,
            "session_id": "bridge-session-input",
            "result": "waiting for input",
            "stream_events": [
                {"type": "input_required", "message": "need operator confirmation"},
            ],
        }


class ToolUseInputRequiredExecutor:
    """M-CT-05: returns a non-completed slice carrying a tool-use of an
    input-asking shape (``ask_question``), so the job transitions to
    BLOCKED_INPUT_REQUIRED (resumable) via the real ``_run`` loop."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        self.calls.append({"root": root, "resume_session_id": resume_session_id})
        return {
            "status": "failed",
            "terminal_reason": "input_required",
            "num_turns": 1,
            "session_id": "bridge-session-tool-input",
            "result": "asking a question",
            "tool_uses": [
                {"name": "ask_question", "message": "confirm the approach"},
            ],
        }


class StreamingDetailExecutor:
    """M-CT-03: returns a completed slice with structured stream events
    (completed/remaining steps, tests, decisions) and a ``tool_uses`` list,
    so the checkpoint detail sidecar row is populated with the parsed fields.

    Mirrors the real Claude stream-json contract: the executor result carries a
    ``stream_events`` list of typed events plus a ``tool_uses`` list. The job
    manager's ``_record_checkpoint`` parses these into the
    ``job_checkpoint_detail`` sidecar (completed_steps / remaining_steps /
    tests / decisions / tool_history).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        self.calls.append({"root": root, "resume_session_id": resume_session_id})
        (root / "candidate.txt").write_text("done\n", encoding="utf-8")
        return {
            "status": "completed",
            "session_id": "bridge-session-detail",
            "num_turns": 2,
            "result": "done with structured events",
            "stream_events": [
                {"type": "step_completed", "data": {"step": "write module"}},
                {"type": "step_remaining", "data": {"step": "add tests"}},
                {
                    "type": "test_result",
                    "data": {"name": "test_smoke", "passed": True},
                },
                {"type": "decision", "data": {"choice": "use sidecar table"}},
            ],
            "tool_uses": [
                {"name": "Read", "input": {"file_path": "src/store.py"}},
                {"name": "Edit", "input": {"file_path": "src/store.py"}},
            ],
        }


class OscillatingEditsExecutor:
    """M-CT-07: writes the same file with alternating content (A/B/A/B) across
    slices and returns a max_turns terminal reason each time.

    The changed-path set stays ``{file.txt}`` while the content (and thus the
    partial fingerprint) flip-flops between two distinct values. With the
    default oscillation window (4) and alternations (2), the progress-
    intelligence helper detects the oscillation and the ``_run`` loop pauses
    with ``pause_reason == "oscillating_edits"`` before the generic no-progress
    check fires.
    """

    def __init__(self, num_turns: int = 5) -> None:
        self.calls: list[dict[str, Any]] = []
        self.num_turns = num_turns

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        n = len(self.calls) + 1
        self.calls.append(
            {"root": root, "resume_session_id": resume_session_id, "max_turns": max_turns}
        )
        # Alternate between two distinct contents so the partial fingerprint
        # flip-flops while the path set stays {file.txt}.
        content = "a\n" if n % 2 == 1 else "b\n"
        (root / "file.txt").write_text(content, encoding="utf-8")
        return {
            "status": "failed",
            "terminal_reason": "max_turns",
            "num_turns": self.num_turns,
            "session_id": "bridge-session-oscillate",
            "result": f"slice {n}",
        }


class ScopeGrowthExecutor:
    """M-CT-07: writes a new file each slice so the cumulative changed-path
    count grows without bound.

    With ``CLAUDE_SCOPE_GROWTH_FACTOR=3.0`` and one file per slice, the
    cumulative count crosses ``earliest_count * factor`` at the fourth slice
    and the ``_run`` loop pauses with ``pause_reason == "scope_growth"``.
    """

    def __init__(self, num_turns: int = 5) -> None:
        self.calls: list[dict[str, Any]] = []
        self.num_turns = num_turns

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        n = len(self.calls) + 1
        self.calls.append(
            {"root": root, "resume_session_id": resume_session_id, "max_turns": max_turns}
        )
        (root / f"step{n}.txt").write_text(f"step {n}\n", encoding="utf-8")
        return {
            "status": "failed",
            "terminal_reason": "max_turns",
            "num_turns": self.num_turns,
            "session_id": "bridge-session-scope",
            "result": f"slice {n}",
        }


class WeakenedTestsExecutor:
    """M-CT-07: writes a Python test file with a shrinking assertion count
    while reporting a green (passed) test result via stream events.

    The first slice writes ``test_something.py`` with 5 assertions; the second
    rewrites it with 2. The detail sidecar's ``tests_json`` carries a passed
    ``test_result`` event on both slices, so the weakened-tests helper detects
    the assertion-count drop while the test stays green and the ``_run`` loop
    pauses with ``pause_reason == "weakened_tests"``.
    """

    def __init__(self, num_turns: int = 5) -> None:
        self.calls: list[dict[str, Any]] = []
        self.num_turns = num_turns

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        n = len(self.calls) + 1
        self.calls.append(
            {"root": root, "resume_session_id": resume_session_id, "max_turns": max_turns}
        )
        # First slice: 5 assertions. Second slice: 2 assertions (weakened).
        count = 5 if n == 1 else 2
        lines = [f"    assert {i} == {i}\n" for i in range(1, count + 1)]
        (root / "test_something.py").write_text(
            "def test_weak():\n" + "".join(lines), encoding="utf-8"
        )
        return {
            "status": "failed",
            "terminal_reason": "max_turns",
            "num_turns": self.num_turns,
            "session_id": "bridge-session-weakened",
            "result": f"slice {n}",
            "stream_events": [
                {"type": "test_result", "data": {"name": "test_weak", "passed": True}},
            ],
        }


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.settings = continuity_settings(self.base)
        self.project = job_policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _db_path(self) -> Path:
        return self.settings.job_worktree_root / ".state" / "bridge.db"

    def _flip_status(self, job_id: str, status: str) -> None:
        conn = sqlite3.connect(self._db_path())
        conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (status, job_id))
        conn.commit()
        conn.close()

    def test_checkpoints_appended_and_rehydrated(self) -> None:
        async def exercise() -> None:
            executor = MaxTurnsProgressExecutor(num_turns=40)
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager, self.project, job_id, {"paused_budget_exhausted"}
            )
            self.assertGreaterEqual(paused["checkpoint_count"], 2)
            # At least one extension checkpoint was recorded.
            rows = load_checkpoint_rows(manager._db, job_id)
            kinds = {row["kind"] for row in rows}
            self.assertIn("boundary", kinds)
            self.assertIn("extension", kinds)

            # A fresh manager re-hydrates the checkpoints from the same db.
            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            reloaded = await b.get(self.project, job_id)
            self.assertEqual(reloaded["status"], "paused_budget_exhausted")
            self.assertEqual(reloaded["checkpoint_count"], paused["checkpoint_count"])

        asyncio.run(exercise())

    def test_progress_grants_extension_and_then_hard_cap_pauses(self) -> None:
        async def exercise() -> None:
            settings = continuity_settings(
                self.base, CLAUDE_MAX_EXTENSIONS="2", CLAUDE_HARD_TURNS="100"
            )
            executor = MaxTurnsProgressExecutor(num_turns=40)
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager, self.project, job_id, {"paused_budget_exhausted"}
            )
            # Two extensions were granted (3 executor slices), cumulative turns
            # crossed the hard cap, and each extension slice resumed the same
            # Claude session rather than restarting.
            self.assertEqual(len(executor.calls), 3)
            self.assertEqual(paused["budget"]["extensions_granted"], 2)
            self.assertGreaterEqual(paused["budget"]["turns_used"], 100)
            self.assertEqual(executor.calls[0]["max_turns"], 30)
            self.assertEqual(executor.calls[1]["max_turns"], 30)
            self.assertEqual(executor.calls[1]["resume_session_id"], "bridge-session-progress")
            self.assertEqual(executor.calls[2]["resume_session_id"], "bridge-session-progress")
            self.assertIn("hard turn cap", str(paused["pause_reason"]))

        asyncio.run(exercise())

    def test_no_progress_terminates_to_paused(self) -> None:
        async def exercise() -> None:
            settings = continuity_settings(
                self.base,
                CLAUDE_CHECKPOINT_INTERVAL_SECONDS="0.02",
                CLAUDE_MAX_NO_PROGRESS_TURNS="3",
                CLAUDE_HARD_TURNS="1000",
            )
            executor = StalledExecutor(delay=0.3, num_turns=5)
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager, self.project, job_id, {"paused_budget_exhausted"}
            )
            # The sampler observed an empty changed set across ticks; the slice
            # ended non-completed and the boundary paused on no-progress.
            self.assertEqual(paused["status"], "paused_budget_exhausted")
            self.assertIn("no progress", str(paused["pause_reason"]))
            self.assertGreaterEqual(paused["budget"]["no_progress_turns"], 3)

        asyncio.run(exercise())

    def test_crashed_job_with_checkpoints_becomes_paused_interrupted(self) -> None:
        async def exercise() -> None:
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            started = await a.start(self.project, "first", "high")
            job_id = str(started["job_id"])
            await wait_for_status(a, self.project, job_id, {"completed"})
            self.assertGreaterEqual(
                (await a.get(self.project, job_id))["checkpoint_count"], 1
            )

            # Simulate a crash mid-flight: flip the durable status back to
            # "running" while the checkpoint ledger is intact.
            self._flip_status(job_id, "running")

            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            reloaded = await b.get(self.project, job_id)
            self.assertEqual(reloaded["status"], "paused_interrupted")
            self.assertIn("bridge restarted", str(reloaded["pause_reason"]))
            # It retained a session + worktree, so it is resumable.
            self.assertTrue(reloaded["resume_available"])
            self.assertGreaterEqual(reloaded["checkpoint_count"], 1)

        asyncio.run(exercise())

    def test_crashed_job_without_checkpoints_still_failed(self) -> None:
        async def exercise() -> None:
            # Default 30s sampler interval means no sample fires before the
            # reload; a checkpoint-less crashed job must remain failed.
            executor_a = BlockingExecutor()
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor_a  # type: ignore[arg-type]
            )
            started = await a.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            await asyncio.wait_for(executor_a.entered.wait(), timeout=2)

            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            reloaded = await b.get(self.project, job_id)
            self.assertEqual(reloaded["status"], "failed")
            self.assertIn("bridge restarted", str(reloaded["error"]))

            await a.cancel(self.project, job_id)

        asyncio.run(exercise())

    def test_paused_job_resumes_from_last_checkpoint(self) -> None:
        async def exercise() -> None:
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            started = await a.start(self.project, "first", "high")
            job_id = str(started["job_id"])
            await wait_for_status(a, self.project, job_id, {"completed"})
            self._flip_status(job_id, "running")

            executor_b = ImmediateExecutor()
            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor_b  # type: ignore[arg-type]
            )
            paused = await b.get(self.project, job_id)
            self.assertEqual(paused["status"], "paused_interrupted")

            resumed = await b.resume(self.project, job_id, "continue", "high")
            self.assertEqual(resumed["attempt"], 2)
            await wait_for_status(b, self.project, job_id, {"completed"})

            # The resume seeded the drafter prompt with the last checkpoint's
            # candidate state and resumed the bridge-created session.
            self.assertEqual(
                executor_b.calls[0]["resume_session_id"], "bridge-session-1"
            )
            self.assertIn("Resuming a previously paused job", executor_b.calls[0]["text"])
            self.assertIn("candidate.txt", executor_b.calls[0]["text"])

        asyncio.run(exercise())

    def test_non_resumable_terminal_states_reject_resume(self) -> None:
        async def exercise() -> None:
            # Quarantined provenance is terminal and not resumable. Inject it
            # directly and assert resume fails closed.
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            started = await a.start(self.project, "first", "high")
            job_id = str(started["job_id"])
            await wait_for_status(a, self.project, job_id, {"completed"})
            self._flip_status(job_id, JobStatus.QUARANTINED_PROVENANCE.value)

            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            quarantined = await b.get(self.project, job_id)
            self.assertEqual(quarantined["status"], "quarantined_provenance")
            self.assertFalse(quarantined["resume_available"])
            with self.assertRaises(Exception):
                await b.resume(self.project, job_id, "again", "high")

        asyncio.run(exercise())

    def test_blocked_input_required_via_stream_json_signal(self) -> None:
        """M-CT-05: a stream-json ``input_required`` event drives the job into
        BLOCKED_INPUT_REQUIRED (resumable) via the real ``_run`` loop — not via
        manual ``_flip_status`` injection.

        The fake executor returns a non-completed slice carrying a
        ``stream_events`` list with an ``input_required`` event. The job
        manager's ``_blocked_input_signal`` detects the structured signal and
        ``_run`` transitions the job to BLOCKED_INPUT_REQUIRED with
        ``resume_available == True``.
        """

        async def exercise() -> None:
            executor = StreamJsonInputRequiredExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            blocked = await wait_for_status(
                manager, self.project, job_id, {"blocked_input_required"}
            )
            self.assertEqual(blocked["status"], "blocked_input_required")
            self.assertTrue(blocked["resume_available"])
            self.assertIn("need operator confirmation", str(blocked["pause_reason"]))

        asyncio.run(exercise())

    def test_blocked_input_required_via_tool_use_signal(self) -> None:
        """M-CT-05: a tool-use of an input-asking shape (``ask_question``) also
        drives the job into BLOCKED_INPUT_REQUIRED via the real ``_run`` loop.
        """

        async def exercise() -> None:
            executor = ToolUseInputRequiredExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            blocked = await wait_for_status(
                manager, self.project, job_id, {"blocked_input_required"}
            )
            self.assertEqual(blocked["status"], "blocked_input_required")
            self.assertTrue(blocked["resume_available"])
            self.assertIn("confirm the approach", str(blocked["pause_reason"]))

        asyncio.run(exercise())

    def test_terminal_report_contract_fields(self) -> None:
        async def exercise() -> None:
            executor = UsageExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            for key in (
                "prohibited_actions",
                "budget",
                "checkpoint_count",
                "milestones",
                "blockers",
                "candidate_fingerprint",
                "changed_path_count",
            ):
                self.assertIn(key, completed)
            self.assertIn("merge", completed["prohibited_actions"])
            self.assertIn("tag", completed["prohibited_actions"])
            self.assertIn("force-push", completed["prohibited_actions"])
            for key in (
                "turns_used",
                "tokens_used",
                "extensions_granted",
                "soft_turns",
                "hard_turns",
                "max_extensions",
            ):
                self.assertIn(key, completed["budget"])
            self.assertEqual(completed["budget"]["turns_used"], 3)
            self.assertEqual(completed["budget"]["tokens_used"], 175)
            self.assertGreaterEqual(completed["changed_path_count"], 1)

        asyncio.run(exercise())

    def test_streaming_executor_yields_detail_row(self) -> None:
        """M-CT-03: a streaming executor with structured stream events yields a
        ``job_checkpoint_detail`` sidecar row with the parsed rich fields."""

        async def exercise() -> None:
            executor = StreamingDetailExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            # The latest checkpoint has a detail row with the parsed fields.
            detail_rows = load_checkpoint_detail_for_job(manager._db, job_id)
            self.assertGreaterEqual(len(detail_rows), 1)
            detail = detail_rows[-1]
            self.assertEqual(
                json.loads(detail["completed_steps_json"]),
                [{"step": "write module"}],
            )
            self.assertEqual(
                json.loads(detail["remaining_steps_json"]),
                [{"step": "add tests"}],
            )
            self.assertEqual(
                json.loads(detail["tests_json"]),
                [{"name": "test_smoke", "passed": True}],
            )
            self.assertEqual(
                json.loads(detail["decisions_json"]),
                [{"choice": "use sidecar table"}],
            )
            tool_history = json.loads(detail["tool_history_json"])
            self.assertEqual(len(tool_history), 2)
            self.assertEqual(tool_history[0]["name"], "Read")
            self.assertEqual(tool_history[1]["name"], "Edit")
            # The snapshot surfaces the detail for the latest checkpoint.
            self.assertIn("checkpoint_detail", completed)
            self.assertEqual(
                completed["checkpoint_detail"]["completed_steps"],
                [{"step": "write module"}],
            )
            self.assertEqual(
                completed["checkpoint_detail"]["tool_history"][1]["name"],
                "Edit",
            )

        asyncio.run(exercise())

    def test_detail_rows_rehydrated_on_reload(self) -> None:
        """M-CT-03: a fresh manager re-hydrates the detail rows from the same
        durable db so the snapshot surfaces them across restart."""

        async def exercise() -> None:
            executor = StreamingDetailExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            await wait_for_status(manager, self.project, job_id, {"completed"})

            # A fresh manager re-hydrates the detail rows from the same db.
            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            reloaded = await b.get(self.project, job_id)
            self.assertEqual(reloaded["status"], "completed")
            self.assertIn("checkpoint_detail", reloaded)
            self.assertEqual(
                reloaded["checkpoint_detail"]["completed_steps"],
                [{"step": "write module"}],
            )
            self.assertEqual(
                reloaded["checkpoint_detail"]["remaining_steps"],
                [{"step": "add tests"}],
            )
            # The durable detail rows survive the reload.
            detail_rows = load_checkpoint_detail_for_job(b._db, job_id)
            self.assertGreaterEqual(len(detail_rows), 1)

        asyncio.run(exercise())

    def test_plain_executor_detail_row_nullable_no_crash(self) -> None:
        """M-CT-03: a plain executor (no structured events) still gets a
        checkpoint; the detail row has NULL fields and nothing crashes."""

        async def exercise() -> None:
            executor = ImmediateExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            # The checkpoint succeeded; detail rows exist with NULL fields.
            detail_rows = load_checkpoint_detail_for_job(manager._db, job_id)
            self.assertGreaterEqual(len(detail_rows), 1)
            for detail in detail_rows:
                for field in (
                    "completed_steps_json",
                    "remaining_steps_json",
                    "tests_json",
                    "decisions_json",
                    "tool_history_json",
                ):
                    self.assertIsNone(
                        detail.get(field),
                        f"{field} should be NULL for a plain executor",
                    )
            # The snapshot does not surface a checkpoint_detail (all NULL).
            self.assertNotIn("checkpoint_detail", completed)
            self.assertEqual(completed["status"], "completed")

        asyncio.run(exercise())

    def test_token_capture(self) -> None:
        async def exercise() -> None:
            executor = UsageExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            self.assertEqual(completed["budget"]["tokens_used"], 175)
            self.assertEqual(completed["budget"]["turns_used"], 3)
            self.assertTrue(completed["resume_available"])

        asyncio.run(exercise())

    def test_soft_budget_warning_flagged(self) -> None:
        async def exercise() -> None:
            # A single completed slice under the soft ceiling should not warn.
            settings = continuity_settings(self.base, CLAUDE_SOFT_TURNS="2")
            executor = UsageExecutor()  # num_turns=3 > soft_turns=2
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            self.assertTrue(completed["budget"]["soft_warned"])

        asyncio.run(exercise())

    def test_oscillating_edits_pause_with_distinct_reason(self) -> None:
        """M-CT-07: a path that flip-flops between two contents across >=4
        checkpoints pauses with ``pause_reason == "oscillating_edits"`` rather
        than the generic no-progress label.

        The fake executor writes ``file.txt`` with alternating content (A/B)
        on each max_turns slice. The changed-path set stays ``{file.txt}``
        while the partial fingerprint alternates; the progress-intelligence
        helper detects the oscillation and the ``_run`` loop pauses before
        the generic no-progress budget check fires.
        """

        async def exercise() -> None:
            settings = continuity_settings(
                self.base,
                CLAUDE_MAX_NO_PROGRESS_TURNS="100",
                CLAUDE_MAX_EXTENSIONS="10",
                CLAUDE_HARD_TURNS="1000",
                CLAUDE_CHECKPOINT_INTERVAL_SECONDS="999",
            )
            executor = OscillatingEditsExecutor(num_turns=5)
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager, self.project, job_id, {"paused_budget_exhausted"}
            )
            self.assertEqual(paused["status"], "paused_budget_exhausted")
            self.assertEqual(paused["pause_reason"], "oscillating_edits")
            # The executor was invoked at least 3 times (one per slice). With a
            # boundary + extension checkpoint per slice, the oscillation window
            # (>=4 checkpoints, >=2 alternations) is satisfied at the third
            # slice's boundary checkpoint.
            self.assertGreaterEqual(len(executor.calls), 3)

        asyncio.run(exercise())

    def test_scope_growth_pause_with_distinct_reason(self) -> None:
        """M-CT-07: a cumulative changed-path count that grows beyond
        ``earliest_count * factor`` pauses with
        ``pause_reason == "scope_growth"``.

        The fake executor writes a new file each slice so the cumulative
        changed-path count grows by one per slice. With a factor of 3.0 and
        one file in the first checkpoint, the fourth slice crosses the
        threshold (4 > 1 * 3.0).
        """

        async def exercise() -> None:
            settings = continuity_settings(
                self.base,
                CLAUDE_SCOPE_GROWTH_FACTOR="3.0",
                CLAUDE_MAX_NO_PROGRESS_TURNS="100",
                CLAUDE_MAX_EXTENSIONS="10",
                CLAUDE_HARD_TURNS="1000",
                CLAUDE_CHECKPOINT_INTERVAL_SECONDS="999",
            )
            executor = ScopeGrowthExecutor(num_turns=5)
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager, self.project, job_id, {"paused_budget_exhausted"}
            )
            self.assertEqual(paused["status"], "paused_budget_exhausted")
            self.assertEqual(paused["pause_reason"], "scope_growth")
            self.assertGreaterEqual(len(executor.calls), 4)

        asyncio.run(exercise())

    def test_weakened_tests_pause_with_distinct_reason(self) -> None:
        """M-CT-07: a test file whose assertion count drops while it stays
        green pauses with ``pause_reason == "weakened_tests"``.

        The fake executor writes ``test_something.py`` with 5 assertions on the
        first slice and 2 on the second, while reporting a passed
        ``test_result`` stream event on both. The helper reads the file's
        assertion markers, compares against the in-memory baseline, and
        detects the drop.
        """

        async def exercise() -> None:
            settings = continuity_settings(
                self.base,
                CLAUDE_MAX_NO_PROGRESS_TURNS="100",
                CLAUDE_MAX_EXTENSIONS="10",
                CLAUDE_HARD_TURNS="1000",
                CLAUDE_CHECKPOINT_INTERVAL_SECONDS="999",
            )
            executor = WeakenedTestsExecutor(num_turns=5)
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager, self.project, job_id, {"paused_budget_exhausted"}
            )
            self.assertEqual(paused["status"], "paused_budget_exhausted")
            self.assertEqual(paused["pause_reason"], "weakened_tests")
            self.assertGreaterEqual(len(executor.calls), 2)

        asyncio.run(exercise())


class CheckpointStoreTests(unittest.TestCase):
    """Direct tests of the append-only checkpoint ledger and compaction."""

    def _fresh_db(self) -> tuple[sqlite3.Connection, Path]:
        temporary = tempfile.TemporaryDirectory()
        self._tmp = temporary  # keep alive for the test
        db_path = Path(temporary.name) / "bridge.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = open_state_db(db_path)
        return connection, db_path

    def _row(self, job_id: str, kind: str, seq: int) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "project_id": "test-project",
            "kind": kind,
            "seq": seq,
            "recorded_at": "2026-08-07T00:00:00Z",
            "changed_paths_json": "[]",
            "partial_fingerprint": None,
            "tokens_json": None,
            "event_json": None,
            "milestone": None,
        }

    def test_checkpoint_detail_sidecar_additive_schema_version_stays_1(self) -> None:
        """M-CT-03: the ``job_checkpoint_detail`` sidecar table is created
        additively (``CREATE TABLE IF NOT EXISTS``), never alters
        ``job_checkpoints``, and ``SCHEMA_VERSION`` stays 1. A detail row
        round-trips through the insert/load helpers and cascades on
        checkpoint compaction."""
        from local_dev_mcp.store import (
            compact_checkpoints,
            insert_checkpoint_detail_row,
        )

        connection, _ = self._fresh_db()
        try:
            # The sidecar table exists alongside the legacy checkpoint table.
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='job_checkpoint_detail'"
            ).fetchone()
            self.assertIsNotNone(row)
            # SCHEMA_VERSION stays 1.
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()[0]
            self.assertEqual(int(version), 1)

            # Insert a checkpoint, then a detail row referencing it (FK).
            cp_id = insert_checkpoint_row(
                connection, self._row("job-detail", "sample", 0)
            )
            insert_checkpoint_detail_row(
                connection,
                {
                    "checkpoint_id": cp_id,
                    "completed_steps_json": '[{"step": "x"}]',
                    "remaining_steps_json": None,
                    "tests_json": None,
                    "decisions_json": None,
                    "tool_history_json": None,
                },
            )
            rows = load_checkpoint_detail_rows(connection, [cp_id])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["completed_steps_json"], '[{"step": "x"}]')
            self.assertIsNone(rows[0]["remaining_steps_json"])

            # load_checkpoint_detail_for_job joins back to job_checkpoints.
            job_rows = load_checkpoint_detail_for_job(connection, "job-detail")
            self.assertEqual(len(job_rows), 1)
            self.assertEqual(job_rows[0]["completed_steps_json"], '[{"step": "x"}]')

            # An empty checkpoint_ids list returns an empty list (no SQL error).
            self.assertEqual(load_checkpoint_detail_rows(connection, []), [])

            # Upsert: re-recording the same checkpoint_id updates, not fails.
            insert_checkpoint_detail_row(
                connection,
                {
                    "checkpoint_id": cp_id,
                    "completed_steps_json": '[{"step": "y"}]',
                    "remaining_steps_json": '[{"step": "z"}]',
                    "tests_json": None,
                    "decisions_json": None,
                    "tool_history_json": None,
                },
            )
            rows = load_checkpoint_detail_rows(connection, [cp_id])
            self.assertEqual(rows[0]["completed_steps_json"], '[{"step": "y"}]')
            self.assertEqual(rows[0]["remaining_steps_json"], '[{"step": "z"}]')

            # ON DELETE CASCADE: compacting the checkpoint prunes its detail.
            # Add 60 more sample rows so compaction prunes the oldest sample
            # (seq 0, not in keep_kinds) when keep_latest_n is small. The detail
            # row for the pruned checkpoint must cascade-delete, not orphan.
            for seq in range(60):
                insert_checkpoint_row(
                    connection, self._row("job-detail", "sample", seq + 1)
                )
            deleted = compact_checkpoints(
                connection, "job-detail", keep_latest_n=5
            )
            self.assertGreater(deleted, 0)
            # The oldest sample checkpoint (seq 0) was pruned by compaction; its
            # detail row cascaded away.
            orphan = connection.execute(
                "SELECT 1 FROM job_checkpoint_detail WHERE checkpoint_id = ?",
                (cp_id,),
            ).fetchone()
            self.assertIsNone(orphan)
        finally:
            connection.close()

    def test_compaction_keeps_milestone_and_boundary_checkpoints(self) -> None:
        connection, _ = self._fresh_db()
        try:
            job_id = "job-compact"
            # 60 sample rows (seq 0..59), 2 boundary rows, 1 milestone row.
            for seq in range(60):
                insert_checkpoint_row(connection, self._row(job_id, "sample", seq))
            insert_checkpoint_row(connection, self._row(job_id, "boundary", 60))
            insert_checkpoint_row(connection, self._row(job_id, "boundary", 61))
            insert_checkpoint_row(connection, self._row(job_id, "milestone", 62))
            self.assertEqual(checkpoint_count(connection, job_id), 63)

            deleted = __import__(
                "local_dev_mcp.store", fromlist=["compact_checkpoints"]
            ).compact_checkpoints(connection, job_id, keep_latest_n=10)
            self.assertEqual(deleted, 50)
            self.assertEqual(checkpoint_count(connection, job_id), 13)

            rows = load_checkpoint_rows(connection, job_id)
            kinds = [row["kind"] for row in rows]
            # All structural rows survive.
            self.assertEqual(kinds.count("boundary"), 2)
            self.assertEqual(kinds.count("milestone"), 1)
            # Only the 10 most-recent samples (seq 50..59) remain.
            sample_seqs = sorted(row["seq"] for row in rows if row["kind"] == "sample")
            self.assertEqual(sample_seqs, list(range(50, 60)))
        finally:
            connection.close()

    def test_additive_schema_upgrades_in_place(self) -> None:
        connection, db_path = self._fresh_db()
        try:
            connection.execute(
                "INSERT INTO jobs (job_id, project_id, effort, status, attempt, "
                "created_at, updated_at, cancel_requested) VALUES "
                "('legacyjob', 'test-project', 'high', 'completed', 1, 't', 't', 0)"
            )
            connection.commit()
            # Simulate a pre-continuity database: drop the continuity tables.
            connection.execute("DROP TABLE job_checkpoints")
            connection.execute("DROP TABLE job_events")
            connection.execute("DROP TABLE job_progress")
            connection.commit()
        finally:
            connection.close()

        # Re-opening with the continuity code recreates them additively, the
        # legacy job row survives, and the schema version is still 1.
        connection = open_state_db(db_path)
        try:
            for table in ("job_checkpoints", "job_events", "job_progress"):
                row = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                self.assertIsNotNone(row, f"{table} should be recreated on open")
            row = connection.execute(
                "SELECT status FROM jobs WHERE job_id='legacyjob'"
            ).fetchone()
            self.assertEqual(row["status"], "completed")
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()[0]
            self.assertEqual(int(version), 1)
        finally:
            connection.close()

    def test_job_progress_gains_tool_failures_column_in_place(self) -> None:
        """A job_progress table created without tool_failures is upgraded on open.

        Regression for the live-server failure ``OperationalError: table
        job_progress has no column named tool_failures``: the MCPB-010 schema
        always declared ``tool_failures`` but it was never registered in
        ``_ADDITIVE_COLUMNS``, so a table created by an earlier build was never
        upgraded and the progress upsert crashed. The column must be added
        idempotently (with a default for the NOT NULL constraint) and the
        progress upsert must then succeed.
        """
        import sqlite3

        from local_dev_mcp.store import upsert_progress_row

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "bridge.db"
            # Simulate a pre-MCPB-010 job_progress table (no tool_failures).
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE job_progress (
                    job_id TEXT PRIMARY KEY,
                    pause_reason TEXT,
                    turns_used INTEGER NOT NULL,
                    tokens_used INTEGER NOT NULL,
                    extensions_granted INTEGER NOT NULL,
                    no_progress_turns INTEGER NOT NULL,
                    soft_warned INTEGER NOT NULL,
                    milestones_json TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.commit()
            connection.close()

            # Re-opening with the current code adds tool_failures additively.
            connection = open_state_db(db_path)
            try:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(job_progress)"
                    ).fetchall()
                }
                self.assertIn("tool_failures", columns)
                self.assertIn("parent_job_id", columns)
                self.assertIn("delegation_mandate_json", columns)
                self.assertIn("wall_seconds_used", columns)
                self.assertIn("pause_requested", columns)
                self.assertIn("manual_extensions_granted", columns)
                self.assertIn("scope_paths_json", columns)
                # The progress upsert that crashed on the live server now works.
                upsert_progress_row(
                    connection,
                    {
                        "job_id": "job-tf",
                        "pause_reason": None,
                        "pause_requested": 0,
                        "manual_extensions_granted": 0,
                        "turns_used": 0,
                        "tokens_used": 0,
                        "extensions_granted": 0,
                        "no_progress_turns": 0,
                        "tool_failures": 0,
                        "soft_warned": 0,
                        "wall_seconds_used": 0.0,
                        "milestones_json": None,
                        "parent_job_id": None,
                        "delegation_mandate_json": None,
                        "successor_base_head": None,
                        "scope_paths_json": None,
                        "updated_at": "2026-08-12T00:00:00Z",
                    },
                )
                row = connection.execute(
                    "SELECT tool_failures FROM job_progress WHERE job_id='job-tf'"
                ).fetchone()
                self.assertEqual(row["tool_failures"], 0)
            finally:
                connection.close()

            # Re-opening again is a no-op: the migration is idempotent and the
            # upsert still succeeds (no duplicate-column error).
            connection = open_state_db(db_path)
            try:
                upsert_progress_row(
                    connection,
                    {
                        "job_id": "job-tf",
                        "pause_reason": None,
                        "pause_requested": 0,
                        "manual_extensions_granted": 0,
                        "turns_used": 1,
                        "tokens_used": 0,
                        "extensions_granted": 0,
                        "no_progress_turns": 0,
                        "tool_failures": 1,
                        "soft_warned": 0,
                        "wall_seconds_used": 1.5,
                        "milestones_json": None,
                        "parent_job_id": None,
                        "delegation_mandate_json": None,
                        "successor_base_head": None,
                        "scope_paths_json": None,
                        "updated_at": "2026-08-12T00:00:01Z",
                    },
                )
                row = connection.execute(
                    "SELECT tool_failures FROM job_progress WHERE job_id='job-tf'"
                ).fetchone()
                self.assertEqual(row["tool_failures"], 1)
            finally:
                connection.close()


class ArtifactGcTests(unittest.TestCase):
    """M-EV-08: time-based, promoted-evidence-safe artifact GC.

    Direct tests of ``store.gc_artifacts`` over a fresh state database. No
    real subprocess or network: checkpoints and evidence rows are inserted
    directly, the reference time is pinned via ``now_iso``, and the GC
    invariant is asserted (promoted evidence and its checkpoints survive GC;
    only old, non-promoted, non-structural checkpoint/detail rows are
    reclaimed).
    """

    def _fresh_db(self) -> tuple[sqlite3.Connection, Path]:
        temporary = tempfile.TemporaryDirectory()
        self._tmp = temporary  # keep alive for the test
        db_path = Path(temporary.name) / "bridge.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = open_state_db(db_path)
        return connection, db_path

    def _row(
        self,
        job_id: str,
        kind: str,
        seq: int,
        recorded_at: str = "2026-08-07T00:00:00Z",
    ) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "project_id": "test-project",
            "kind": kind,
            "seq": seq,
            "recorded_at": recorded_at,
            "changed_paths_json": "[]",
            "partial_fingerprint": None,
            "tokens_json": None,
            "event_json": None,
            "milestone": None,
        }

    def _insert_verification(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        *,
        used: int = 1,
        verification_id: str = "v-1",
    ) -> None:
        connection.execute(
            "INSERT INTO verifications (verification_id, project_id, job_id, "
            "base_head, diff_fingerprint, paths_json, verified_at, used, "
            "commands_json) VALUES (?, 'test-project', ?, 'head', 'fp', '[]', "
            "'2026-08-07T00:00:00Z', ?, '[]')",
            (verification_id, job_id, used),
        )
        connection.commit()

    def _insert_review(
        self, connection: sqlite3.Connection, job_id: str, review_id: str = "r-1"
    ) -> None:
        connection.execute(
            "INSERT INTO reviews (review_id, project_id, job_id, base_head, "
            "diff_fingerprint, paths_json, reviewed_at, reviewer_id, "
            "verdict_json, used) VALUES (?, 'test-project', ?, 'head', 'fp', "
            "'[]', '2026-08-07T00:00:00Z', 'reviewer', '{}', 0)",
            (review_id, job_id),
        )
        connection.commit()

    def _insert_publication(self, connection: sqlite3.Connection, job_id: str) -> None:
        connection.execute(
            "INSERT INTO publications (project_id, job_id, commit_sha, "
            "branch_name, remote_url, published_at) VALUES ('test-project', ?, "
            "'sha', 'branch', 'url', '2026-08-07T00:00:00Z')",
            (job_id,),
        )
        connection.commit()

    def _insert_resolution_link(
        self,
        connection: sqlite3.Connection,
        review_id: str,
        resolving_job_id: str,
        resolving_checkpoint_id: int,
    ) -> None:
        connection.execute(
            "INSERT INTO revision_resolves_finding (review_id, "
            "resolving_job_id, resolving_checkpoint_id, finding_ref, "
            "resolved_at) VALUES (?, ?, ?, 'finding', '2026-08-07T00:00:00Z')",
            (review_id, resolving_job_id, resolving_checkpoint_id),
        )
        connection.commit()

    def test_retention_zero_disabled_deletes_nothing(self) -> None:
        """With ``checkpoint_retention_days == 0`` (the default) GC is a no-op
        regardless of checkpoint age: the disabled path returns 0 and no rows
        are touched."""
        connection, _ = self._fresh_db()
        try:
            for seq in range(5):
                insert_checkpoint_row(
                    connection, self._row("job-a", "sample", seq, "2020-01-01T00:00:00Z")
                )
            self.assertEqual(checkpoint_count(connection, "job-a"), 5)
            deleted = gc_artifacts(
                connection,
                evidence_retention_days=0,
                checkpoint_retention_days=0,
                now_iso="2026-08-26T00:00:00Z",
            )
            self.assertEqual(deleted, 0)
            self.assertEqual(checkpoint_count(connection, "job-a"), 5)
        finally:
            connection.close()

    def test_gc_deletes_old_nonpromoted_samples_keeps_structural_and_recent(
        self,
    ) -> None:
        """GC deletes old non-promoted ``sample`` checkpoints beyond the
        retention window while keeping structural kinds (boundary/extension/
        milestone) and recent rows."""
        connection, _ = self._fresh_db()
        try:
            # Old sample rows (recorded 2026-08-01) — beyond a 7-day window
            # pinned at 2026-08-26. All are non-promoted (no evidence).
            old_sample_ids: list[int] = []
            for seq in range(3):
                old_sample_ids.append(
                    insert_checkpoint_row(
                        connection,
                        self._row("job-old", "sample", seq, "2026-08-01T00:00:00Z"),
                    )
                )
            # Old structural rows (same age) — must survive GC.
            old_boundary_id = insert_checkpoint_row(
                connection, self._row("job-old", "boundary", 10, "2026-08-01T00:00:00Z")
            )
            old_extension_id = insert_checkpoint_row(
                connection, self._row("job-old", "extension", 11, "2026-08-01T00:00:00Z")
            )
            old_milestone_id = insert_checkpoint_row(
                connection, self._row("job-old", "milestone", 12, "2026-08-01T00:00:00Z")
            )
            # Recent sample row (within the 7-day window) — must survive.
            recent_sample_id = insert_checkpoint_row(
                connection, self._row("job-old", "sample", 20, "2026-08-24T00:00:00Z")
            )
            self.assertEqual(checkpoint_count(connection, "job-old"), 7)

            deleted = gc_artifacts(
                connection,
                evidence_retention_days=0,
                checkpoint_retention_days=7,
                now_iso="2026-08-26T00:00:00Z",
            )
            # The three old sample rows are reclaimed; structural + recent
            # rows survive.
            self.assertEqual(deleted, 3)
            remaining = {
                row["checkpoint_id"] for row in load_checkpoint_rows(connection, "job-old")
            }
            self.assertEqual(remaining, {old_boundary_id, old_extension_id, old_milestone_id, recent_sample_id})
            for old_sample_id in old_sample_ids:
                self.assertNotIn(old_sample_id, remaining)
        finally:
            connection.close()

    def test_promoted_verification_protects_its_checkpoints_forever(self) -> None:
        """A checkpoint whose job has a committed verification (``used = 1``)
        is kept forever even when its ``recorded_at`` is far beyond the
        retention window — the promoted-evidence guard."""
        connection, _ = self._fresh_db()
        try:
            old_sample_id = insert_checkpoint_row(
                connection, self._row("job-promoted", "sample", 0, "2020-01-01T00:00:00Z")
            )
            insert_checkpoint_row(
                connection, self._row("job-promoted", "turn", 1, "2020-01-01T00:00:00Z")
            )
            # A committed verification promotes the whole job's checkpoints.
            self._insert_verification(connection, "job-promoted", used=1)
            self.assertEqual(checkpoint_count(connection, "job-promoted"), 2)

            deleted = gc_artifacts(
                connection,
                evidence_retention_days=0,
                checkpoint_retention_days=7,
                now_iso="2026-08-26T00:00:00Z",
            )
            self.assertEqual(deleted, 0)
            self.assertEqual(checkpoint_count(connection, "job-promoted"), 2)
            rows = load_checkpoint_rows(connection, "job-promoted")
            self.assertEqual(rows[0]["checkpoint_id"], old_sample_id)
        finally:
            connection.close()

    def test_unused_verification_does_not_protect(self) -> None:
        """A verification row with ``used = 0`` is not a committed verification
        and does not promote its job's checkpoints — old non-structural rows
        are still eligible for GC."""
        connection, _ = self._fresh_db()
        try:
            old_sample_id = insert_checkpoint_row(
                connection, self._row("job-unused", "sample", 0, "2020-01-01T00:00:00Z")
            )
            # An unused verification does NOT protect.
            self._insert_verification(
                connection, "job-unused", used=0, verification_id="v-unused"
            )

            deleted = gc_artifacts(
                connection,
                evidence_retention_days=0,
                checkpoint_retention_days=7,
                now_iso="2026-08-26T00:00:00Z",
            )
            self.assertEqual(deleted, 1)
            remaining = {
                row["checkpoint_id"] for row in load_checkpoint_rows(connection, "job-unused")
            }
            self.assertNotIn(old_sample_id, remaining)
        finally:
            connection.close()

    def test_review_or_publication_protects_checkpoints(self) -> None:
        """Any review/publication row on a job promotes its checkpoints — old
        non-structural rows survive GC. (``syncs`` is project-level with no
        ``job_id`` column, so it cannot protect a specific job's checkpoints;
        it is still never deleted by GC — verified in
        ``test_gc_never_touches_evidence_tables``.)"""
        connection, _ = self._fresh_db()
        try:
            insert_checkpoint_row(
                connection, self._row("job-review", "sample", 0, "2020-01-01T00:00:00Z")
            )
            insert_checkpoint_row(
                connection, self._row("job-pub", "sample", 0, "2020-01-01T00:00:00Z")
            )
            self._insert_review(connection, "job-review", review_id="r-review")
            self._insert_publication(connection, "job-pub")

            deleted = gc_artifacts(
                connection,
                evidence_retention_days=0,
                checkpoint_retention_days=7,
                now_iso="2026-08-26T00:00:00Z",
            )
            self.assertEqual(deleted, 0)
            self.assertEqual(checkpoint_count(connection, "job-review"), 1)
            self.assertEqual(checkpoint_count(connection, "job-pub"), 1)
        finally:
            connection.close()

    def test_resolution_link_protects_referenced_checkpoint(self) -> None:
        """A checkpoint directly referenced by
        ``revision_resolves_finding.resolving_checkpoint_id`` is kept forever
        even when old, non-structural, and on a job with no promoted evidence.

        The protection is per-checkpoint (by id), not per-job: another old
        sample checkpoint on the same job without a resolution link is still
        reclaimed."""
        connection, _ = self._fresh_db()
        try:
            referenced_id = insert_checkpoint_row(
                connection, self._row("job-resolve", "sample", 0, "2020-01-01T00:00:00Z")
            )
            unreferenced_id = insert_checkpoint_row(
                connection, self._row("job-resolve", "sample", 1, "2020-01-01T00:00:00Z")
            )
            # A review on a DIFFERENT job references the checkpoint. The
            # checkpoint's own job has no promoted evidence, yet it survives
            # because it is directly referenced.
            self._insert_review(connection, "job-other", review_id="r-resolve")
            self._insert_resolution_link(
                connection, "r-resolve", "job-resolve", referenced_id
            )

            deleted = gc_artifacts(
                connection,
                evidence_retention_days=0,
                checkpoint_retention_days=7,
                now_iso="2026-08-26T00:00:00Z",
            )
            # Only the unreferenced old sample is reclaimed.
            self.assertEqual(deleted, 1)
            remaining = {
                row["checkpoint_id"] for row in load_checkpoint_rows(connection, "job-resolve")
            }
            self.assertIn(referenced_id, remaining)
            self.assertNotIn(unreferenced_id, remaining)
        finally:
            connection.close()

    def test_gc_cascades_detail_rows(self) -> None:
        """Deleting an old non-promoted checkpoint cascades to its
        ``job_checkpoint_detail`` sidecar row (FK ON DELETE CASCADE)."""
        from local_dev_mcp.store import insert_checkpoint_detail_row, load_checkpoint_detail_rows

        connection, _ = self._fresh_db()
        try:
            old_cp_id = insert_checkpoint_row(
                connection, self._row("job-cascade", "sample", 0, "2020-01-01T00:00:00Z")
            )
            insert_checkpoint_detail_row(
                connection,
                {
                    "checkpoint_id": old_cp_id,
                    "completed_steps_json": '[{"step": "x"}]',
                    "remaining_steps_json": None,
                    "tests_json": None,
                    "decisions_json": None,
                    "tool_history_json": None,
                },
            )
            # A structural row that must survive, plus its detail.
            boundary_cp_id = insert_checkpoint_row(
                connection, self._row("job-cascade", "boundary", 1, "2020-01-01T00:00:00Z")
            )
            insert_checkpoint_detail_row(
                connection,
                {
                    "checkpoint_id": boundary_cp_id,
                    "completed_steps_json": '[{"step": "keep"}]',
                    "remaining_steps_json": None,
                    "tests_json": None,
                    "decisions_json": None,
                    "tool_history_json": None,
                },
            )
            self.assertEqual(len(load_checkpoint_detail_rows(connection, [old_cp_id, boundary_cp_id])), 2)

            deleted = gc_artifacts(
                connection,
                evidence_retention_days=0,
                checkpoint_retention_days=7,
                now_iso="2026-08-26T00:00:00Z",
            )
            self.assertEqual(deleted, 1)
            # The old sample's detail row cascaded away.
            orphan = connection.execute(
                "SELECT 1 FROM job_checkpoint_detail WHERE checkpoint_id = ?",
                (old_cp_id,),
            ).fetchone()
            self.assertIsNone(orphan)
            # The structural row's detail survived.
            survivor = connection.execute(
                "SELECT 1 FROM job_checkpoint_detail WHERE checkpoint_id = ?",
                (boundary_cp_id,),
            ).fetchone()
            self.assertIsNotNone(survivor)
        finally:
            connection.close()

    def test_gc_never_touches_evidence_tables(self) -> None:
        """GC deletes only ``job_checkpoints`` (+ cascaded detail). Evidence
        rows in verifications/reviews/publications/syncs are never deleted,
        even when their job has old, non-promoted-looking checkpoints that are
        protected solely because the evidence row exists."""
        connection, _ = self._fresh_db()
        try:
            # job-evidence has a review (promotes its checkpoints) and old
            # samples. GC must not delete the review or the checkpoints.
            insert_checkpoint_row(
                connection, self._row("job-evidence", "sample", 0, "2020-01-01T00:00:00Z")
            )
            self._insert_review(connection, "job-evidence", review_id="r-keep")

            deleted = gc_artifacts(
                connection,
                evidence_retention_days=0,
                checkpoint_retention_days=7,
                now_iso="2026-08-26T00:00:00Z",
            )
            self.assertEqual(deleted, 0)
            # The evidence row is intact.
            review_count = connection.execute(
                "SELECT COUNT(*) FROM reviews WHERE review_id = 'r-keep'"
            ).fetchone()[0]
            self.assertEqual(int(review_count), 1)
            # And the protected checkpoint survived.
            self.assertEqual(checkpoint_count(connection, "job-evidence"), 1)
        finally:
            connection.close()

    def test_gc_additive_index_created_and_schema_version_stays_1(self) -> None:
        """M-EV-08: the GC backing index is additive (CREATE INDEX IF NOT
        EXISTS) and ``SCHEMA_VERSION`` stays 1."""
        connection, _ = self._fresh_db()
        try:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_job_checkpoints_recorded_at'"
            ).fetchone()
            self.assertIsNotNone(row)
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()[0]
            self.assertEqual(int(version), 1)
        finally:
            connection.close()


class ContinuityToolSurfaceTests(unittest.TestCase):
    def test_continuity_adds_no_new_mcp_tools(self) -> None:
        import asyncio as _asyncio

        from local_dev_mcp.registry import ProjectRegistry
        from local_dev_mcp.server import create_server

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            initialize_git_repository(root)
            (root / "docs").mkdir()
            project = ProjectPolicy.model_validate(
                {
                    "id": "test-project",
                    "display_name": "Test Project",
                    "description": "Continuity",
                    "root": str(root),
                    "capabilities": [
                        "repository_read",
                        "markdown_write",
                        "claude_edit",
                        "verification_commit",
                    ],
                    "markdown_write_roots": ["docs"],
                    "commit_roots": ["."],
                    "verification_commands": [
                        {"id": "core", "argv": ["{python}", "-c", "print('ok')"]}
                    ],
                    "protected_paths": [".git", ".env"],
                }
            )
            server = create_server(
                Settings.from_env(
                    {
                        "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
                        "JOB_WORKTREE_ROOT": str(base / "jobs"),
                    }
                ),
                ProjectRegistry({"test-project": project}),
            )
            tools = {tool.name for tool in _asyncio.run(server.list_tools())}

        # Continuity is internal to the job lifecycle: it must not add tools.
        # The count is the current tool surface (37: the M-CC-08 human control
        # surface added pause_development_job, extend_budget, narrow_scope,
        # replace_worker, and discard_candidate); continuity itself adds none.
        self.assertEqual(len(tools), 37)
        for leaked in (
            "checkpoint_development_job",
            "extend_development_job",
            "compact_checkpoints",
            "list_checkpoints",
        ):
            self.assertNotIn(leaked, tools)
        self.assertIn("resume_development_job", tools)
        self.assertIn("pause_development_job", tools)
        self.assertIn("extend_budget", tools)
        self.assertIn("narrow_scope", tools)
        self.assertIn("replace_worker", tools)
        self.assertIn("discard_candidate", tools)


if __name__ == "__main__":
    unittest.main()