from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import store
from .candidate import CandidateHealthChecker
from .claude import ClaudeExecutor, validate_effort
from .config import Settings
from .models import Effort, ProjectPolicy
from .registry import ProjectLockManager
from .worktrees import GitWorktreeManager, JobWorktree

# NOTE: ``verification`` is imported lazily inside the checkpoint helpers below
# (not at module top) to avoid a circular import: ``verification`` imports the
# job manager, and the job manager now uses ``candidate_changed_paths`` /
# ``partial_fingerprint`` for in-flight progress sampling.


class DevelopmentJobError(RuntimeError):
    """Raised when a development job request is invalid or unauthorized."""


class JobStatus(StrEnum):
    QUEUED = "queued"
    PREPARING_WORKTREE = "preparing_worktree"
    RUNNING = "running"
    VALIDATING_CANDIDATE = "validating_candidate"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    # Continuity release: durable in-flight state. The ``PAUSED_*`` family is
    # resumable from the last checkpoint; ``FAILED_INFRASTRUCTURE`` and
    # ``QUARANTINED_PROVENANCE`` are terminal and not resumable.
    PAUSED_BUDGET_EXHAUSTED = "paused_budget_exhausted"
    PAUSED_INTERRUPTED = "paused_interrupted"
    BLOCKED_INPUT_REQUIRED = "blocked_input_required"
    FAILED_INFRASTRUCTURE = "failed_infrastructure"
    QUARANTINED_PROVENANCE = "quarantined_provenance"


# Every status below is "not running": a freshly loaded job in any of these is
# inactive and safe to publish/resume from. The PAUSED_* and BLOCKED_* states
# are resumable; the FAILED_INFRASTRUCTURE / QUARANTINED_PROVENANCE states are
# terminal and must not be resumed.
TERMINAL_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.TIMED_OUT,
        JobStatus.CANCELLED,
        JobStatus.PAUSED_BUDGET_EXHAUSTED,
        JobStatus.PAUSED_INTERRUPTED,
        JobStatus.BLOCKED_INPUT_REQUIRED,
        JobStatus.FAILED_INFRASTRUCTURE,
        JobStatus.QUARANTINED_PROVENANCE,
    }
)

PAUSED_STATUSES = frozenset(
    {
        JobStatus.PAUSED_BUDGET_EXHAUSTED,
        JobStatus.PAUSED_INTERRUPTED,
        JobStatus.BLOCKED_INPUT_REQUIRED,
    }
)

NON_RESUMABLE_STATUSES = frozenset(
    {
        JobStatus.FAILED_INFRASTRUCTURE,
        JobStatus.QUARANTINED_PROVENANCE,
    }
)

# Actions the bridge never authorizes from a development job, surfaced in every
# terminal report so an orchestrator can never mistake a completed candidate for
# authority to publish beyond the opt-in push surface.
PROHIBITED_ACTIONS = (
    "merge",
    "tag",
    "force-push",
    "push-to-trunk",
    "branch-deletion",
    "ref-move",
    "release",
    "deploy",
)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _valid_session_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and all(ord(character) >= 32 for character in value)
    )


def _redact(value: Any, paths: tuple[Path, ...]) -> Any:
    if isinstance(value, str):
        result = value
        for path in paths:
            result = result.replace(str(path), "<redacted-path>")
        return result
    if isinstance(value, dict):
        return {key: _redact(item, paths) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, paths) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, paths) for item in value)
    return value


@dataclass(slots=True)
class DevelopmentJob:
    job_id: str
    project_id: str
    effort: Effort
    status: JobStatus = JobStatus.QUEUED
    attempt: int = 1
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)
    cancel_requested: bool = False
    worktree: JobWorktree | None = None
    session_id: str | None = None
    result: dict[str, Any] | None = None
    candidate: dict[str, Any] | None = None
    error: str | None = None
    committed_sha: str | None = None
    task: asyncio.Task[None] | None = None
    # Continuity release: turn-level budget/progress state. Persisted in the
    # ``job_progress`` sidecar (not the ``jobs`` row) so the schema change is
    # purely additive — existing rows and tests are untouched.
    pause_reason: str | None = None
    turns_used: int = 0
    tokens_used: int = 0
    extensions_granted: int = 0
    no_progress_turns: int = 0
    tool_failures: int = 0
    soft_warned: bool = False
    milestones: list[dict[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class CommittedCandidate:
    """A completed job's detached commit and its retained worktree.

    Returned by ``committed_candidate`` for publication: ``committed_sha`` is the
    exact object ``commit_verified_development_job`` created, and ``worktree`` is
    the retained isolated worktree whose ``base_head`` is the verified base.
    """

    committed_sha: str
    worktree: JobWorktree


_VALID_EFFORTS = frozenset({"high", "ultracode"})


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def job_to_row(job: DevelopmentJob) -> dict[str, Any]:
    """Serialize a job to a dict row for the durable store."""
    worktree_path: str | None = None
    worktree_base_head: str | None = None
    if job.worktree is not None:
        worktree_path = str(job.worktree.path)
        worktree_base_head = job.worktree.base_head
    return {
        "job_id": job.job_id,
        "project_id": job.project_id,
        "effort": job.effort,
        "status": job.status.value,
        "attempt": job.attempt,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "cancel_requested": 1 if job.cancel_requested else 0,
        "session_id": job.session_id,
        "committed_sha": job.committed_sha,
        "worktree_path": worktree_path,
        "worktree_base_head": worktree_base_head,
        "result_json": _json_dumps(job.result),
        "candidate_json": _json_dumps(job.candidate),
        "error": job.error,
    }


def job_from_row(row: dict[str, Any]) -> DevelopmentJob | None:
    """Rebuild a job from a dict row, or None if the row is unparseable.

    Returns None (rather than raising) for any malformed, unknown-status, or
    unknown-effort row so one corrupt record never prevents the server from
    starting.
    """
    try:
        status = JobStatus(row["status"])
    except (KeyError, ValueError):
        return None
    effort = row.get("effort")
    if effort not in _VALID_EFFORTS:
        return None
    worktree = None
    worktree_path = row.get("worktree_path")
    worktree_base_head = row.get("worktree_base_head")
    if worktree_path is not None and worktree_base_head is not None:
        worktree = JobWorktree(path=Path(worktree_path), base_head=worktree_base_head)
    try:
        attempt = int(row.get("attempt", 1))
    except (TypeError, ValueError):
        return None
    return DevelopmentJob(
        job_id=row["job_id"],
        project_id=row["project_id"],
        effort=effort,  # type: ignore[arg-type]
        status=status,
        attempt=attempt,
        created_at=row.get("created_at", _timestamp()),
        updated_at=row.get("updated_at", _timestamp()),
        cancel_requested=bool(row.get("cancel_requested", 0)),
        session_id=row.get("session_id"),
        committed_sha=row.get("committed_sha"),
        worktree=worktree,
        result=_json_loads(row.get("result_json")),
        candidate=_json_loads(row.get("candidate_json")),
        error=row.get("error"),
    )


def _worktree_dir_ok(path: Path) -> bool:
    """True if ``path`` is an existing, non-symlink directory (sync, no git)."""
    try:
        metadata = os.lstat(path)
    except (FileNotFoundError, OSError):
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return False
    return True


class DevelopmentJobManager:
    def __init__(
        self,
        settings: Settings,
        locks: ProjectLockManager,
        executor: ClaudeExecutor,
        worktrees: GitWorktreeManager | None = None,
        candidate_checker: CandidateHealthChecker | None = None,
        projects: tuple[ProjectPolicy, ...] = (),
    ) -> None:
        self.settings = settings
        self._locks = locks
        self._executor = executor
        self._worktrees = worktrees or GitWorktreeManager(
            settings,
            tuple(project.root for project in projects),
        )
        self._candidate_checker = candidate_checker or CandidateHealthChecker(settings)
        self._project_roots = tuple(project.root for project in projects)
        self._jobs: dict[str, DevelopmentJob] = {}
        self._state_lock = asyncio.Lock()
        # Per-run (in-memory only) last observed changed-path set, used to
        # detect progress between checkpoints within a single ``_run``. Not
        # persisted: it is re-derived from checkpoints on resume.
        self._last_changed_paths: dict[str, tuple[str, ...]] = {}
        # Durable state: re-hydrate every job from disk so a server restart no
        # longer amnesiates in-flight or completed work. Done synchronously in
        # __init__ (only filesystem/DB work, no await) so it works before the
        # event loop starts.
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")
        self._reload_jobs()

    def _reload_jobs(self) -> None:
        """Load persisted jobs and reconcile any that were mid-flight at crash.

        A job that was not terminal when the previous process died is
        reconciled using its checkpoint ledger: if it has any checkpoint rows
        it becomes ``paused_interrupted`` (resumable from the last checkpoint),
        otherwise it is marked ``failed`` (its task and Claude process are gone
        and cannot be reattached, and nothing was durably captured). A terminal
        job whose worktree directory has since vanished is also marked
        ``failed``. Both keep the retained worktree content for inspection;
        neither is silently lost. Unparseable rows are skipped.
        """
        changed: list[DevelopmentJob] = []
        for row in store.load_job_rows(self._db):
            job = job_from_row(row)
            if job is None:
                continue
            job.task = None
            self._apply_progress_row(job)
            mutated = False
            if job.status not in TERMINAL_STATUSES:
                if store.checkpoint_count(self._db, job.job_id) > 0:
                    job.status = JobStatus.PAUSED_INTERRUPTED
                    job.pause_reason = "bridge restarted while job was in progress"
                else:
                    job.status = JobStatus.FAILED
                    job.error = "bridge restarted while job was in progress"
                job.updated_at = _timestamp()
                mutated = True
            elif job.worktree is not None and not _worktree_dir_ok(job.worktree.path):
                job.worktree = None
                job.status = JobStatus.FAILED
                job.error = "job worktree no longer exists"
                job.updated_at = _timestamp()
                mutated = True
            self._jobs[job.job_id] = job
            if mutated:
                changed.append(job)
                self._persist_progress(job)
        for job in changed:
            store.upsert_job_row(self._db, job_to_row(job))

    def _persist_job(self, job: DevelopmentJob) -> None:
        """Persist one job as a single ACID transaction.

        Call this inside ``async with self._state_lock`` after mutating a job
        so the durable store mirrors the in-memory state.
        """
        store.upsert_job_row(self._db, job_to_row(job))

    def _progress_to_row(self, job: DevelopmentJob) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "pause_reason": job.pause_reason,
            "turns_used": job.turns_used,
            "tokens_used": job.tokens_used,
            "extensions_granted": job.extensions_granted,
            "no_progress_turns": job.no_progress_turns,
            "tool_failures": job.tool_failures,
            "soft_warned": 1 if job.soft_warned else 0,
            "milestones_json": _json_dumps(job.milestones),
            "updated_at": _timestamp(),
        }

    def _persist_progress(self, job: DevelopmentJob) -> None:
        """Persist the budget/progress sidecar row (additive to the jobs row)."""
        store.upsert_progress_row(self._db, self._progress_to_row(job))

    def _apply_progress_row(self, job: DevelopmentJob) -> None:
        """Re-hydrate a job's budget/progress counters from the sidecar row."""
        row = store.load_progress_row(self._db, job.job_id)
        if row is None:
            return
        job.pause_reason = row.get("pause_reason")
        try:
            job.turns_used = int(row.get("turns_used", 0))
            job.tokens_used = int(row.get("tokens_used", 0))
            job.extensions_granted = int(row.get("extensions_granted", 0))
            job.no_progress_turns = int(row.get("no_progress_turns", 0))
            job.tool_failures = int(row.get("tool_failures", 0))
        except (TypeError, ValueError):
            return
        job.soft_warned = bool(row.get("soft_warned", 0))
        milestones = _json_loads(row.get("milestones_json"))
        if isinstance(milestones, list):
            job.milestones = milestones

    def _record_event(
        self,
        job: DevelopmentJob,
        event_type: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append one row to the append-only audit ledger."""
        store.insert_event_row(
            self._db,
            {
                "job_id": job.job_id,
                "project_id": job.project_id,
                "occurred_at": _timestamp(),
                "event_type": event_type,
                "detail_json": _json_dumps(detail or {}),
            },
        )

    def _accumulate_tokens(self, job: DevelopmentJob, usage: Any) -> None:
        """Add a Claude result's ``usage`` token totals to the cumulative count."""
        if not isinstance(usage, dict):
            return
        total = 0
        for value in usage.values():
            if isinstance(value, (int, float)) and value > 0:
                total += int(value)
        job.tokens_used += total

    async def _current_changed_paths(
        self, job: DevelopmentJob
    ) -> tuple[str, ...]:
        """Return the worktree's current changed-path set, or () if none/unsafe.

        Wraps ``candidate_changed_paths`` so a sampler tick never aborts the
        job: a candidate with no changes yet, or a git read that races an in-
        flight edit, yields an empty set rather than raising.
        """
        if job.worktree is None:
            return ()
        try:
            from .verification import VerificationError, candidate_changed_paths

            return await candidate_changed_paths(self.settings, job.worktree)
        except VerificationError:
            return ()
        except Exception:
            # Any transient git/filesystem error during sampling must not kill
            # the job; record an empty set and move on.
            return ()

    async def _record_checkpoint(
        self,
        job: DevelopmentJob,
        kind: str,
        *,
        milestone: str | None = None,
        event: dict[str, Any] | None = None,
    ) -> tuple[str, ...]:
        """Append one checkpoint row and update no-progress tracking.

        Computes the current changed-path set and a best-effort partial
        fingerprint over the worktree, persists both as an append-only
        checkpoint, and advances the no-progress counter: it resets to 0 when
        the changed set changes and increments when it is unchanged from the
        last recorded checkpoint. Returns the current changed-path set.
        """
        paths = await self._current_changed_paths(job)
        previous = self._last_changed_paths.get(job.job_id)
        if previous is None:
            # First checkpoint of this run: any non-empty set counts as progress.
            job.no_progress_turns = 0 if paths else max(job.no_progress_turns, 0)
        elif paths == previous:
            job.no_progress_turns += 1
        else:
            job.no_progress_turns = 0
        self._last_changed_paths[job.job_id] = paths

        fingerprint: str | None = None
        if job.worktree is not None:
            try:
                from .verification import partial_fingerprint

                fingerprint = await partial_fingerprint(
                    self.settings, job.worktree, paths
                )
            except Exception:
                fingerprint = None

        seq = store.checkpoint_count(self._db, job.job_id)
        store.insert_checkpoint_row(
            self._db,
            {
                "job_id": job.job_id,
                "project_id": job.project_id,
                "kind": kind,
                "seq": seq,
                "recorded_at": _timestamp(),
                "changed_paths_json": _json_dumps(list(paths)),
                "partial_fingerprint": fingerprint,
                "tokens_json": _json_dumps({"tokens_used": job.tokens_used}),
                "event_json": _json_dumps(event),
                "milestone": milestone,
            },
        )
        return paths

    def _progressed(self, job: DevelopmentJob) -> bool:
        """True if the last checkpoint's changed set differs from the prior one."""
        current = self._last_changed_paths.get(job.job_id)
        return bool(current)

    async def _sampler_loop(
        self,
        job: DevelopmentJob,
        exec_task: asyncio.Task[dict[str, Any]],
    ) -> None:
        """Periodically record ``sample`` checkpoints while a slice runs.

        The sampler is the in-flight progress signal: it appends a checkpoint
        every ``claude_checkpoint_interval_seconds`` so a crash mid-slice is
        recoverable and so no-progress is observed during a single (possibly
        long) executor invocation. It never cancels the executor — pause
        decisions are made at slice boundaries from the budget counters this
        loop updates. Cancellation is benign and expected at slice end.
        """
        interval = self.settings.claude_checkpoint_interval_seconds
        try:
            while not exec_task.done():
                await asyncio.sleep(interval)
                if exec_task.done():
                    break
                await self._record_checkpoint(job, "sample")
                self._persist_progress(job)
        except asyncio.CancelledError:
            pass

    def _absorb_result_budget(self, job: DevelopmentJob, result: dict[str, Any]) -> None:
        """Accumulate one executor slice's turn/token/tool-failure counts."""
        num_turns = result.get("num_turns")
        if isinstance(num_turns, (int, float)) and num_turns > 0:
            job.turns_used += int(num_turns)
        self._accumulate_tokens(job, result.get("usage"))
        tool_failures = result.get("tool_failures")
        if isinstance(tool_failures, int) and tool_failures > 0:
            job.tool_failures += tool_failures
        if (
            not job.soft_warned
            and job.turns_used >= self.settings.claude_soft_turns > 0
        ):
            job.soft_warned = True

    def _validate_input(
        self,
        project: ProjectPolicy,
        text: str,
        effort: Effort,
    ) -> str:
        specification = text.strip()
        if not specification:
            raise DevelopmentJobError("text must not be empty")
        if len(specification) > self.settings.max_input_chars:
            raise DevelopmentJobError(
                f"text exceeds the {self.settings.max_input_chars}-character limit"
            )
        validate_effort(project, effort)
        return specification

    def _new_job_id(self) -> str:
        for _ in range(10):
            candidate = secrets.token_hex(16)
            if candidate not in self._jobs and not store.job_id_exists(
                self._db, candidate
            ):
                return candidate
        raise DevelopmentJobError("could not allocate a unique job ID")

    def _paths_for(self, project: ProjectPolicy, job: DevelopmentJob) -> tuple[Path, ...]:
        paths = (
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )
        if job.worktree is not None:
            paths += (job.worktree.path,)
        return paths

    def _snapshot(self, job: DevelopmentJob) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "job_id": job.job_id,
            "project_id": job.project_id,
            "status": job.status.value,
            "effort": job.effort,
            "attempt": job.attempt,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "cancel_requested": job.cancel_requested,
            "resume_available": (
                job.session_id is not None
                and job.worktree is not None
                and job.status in TERMINAL_STATUSES
                and job.status not in NON_RESUMABLE_STATUSES
                and job.committed_sha is None
            ),
            # Structured terminal report: the completion contract an
            # orchestrator checks before promoting a candidate. These fields
            # are additive to the historical snapshot and always present.
            "prohibited_actions": list(PROHIBITED_ACTIONS),
            "budget": {
                "turns_used": job.turns_used,
                "tokens_used": job.tokens_used,
                "extensions_granted": job.extensions_granted,
                "no_progress_turns": job.no_progress_turns,
                "tool_failures": job.tool_failures,
                "soft_turns": self.settings.claude_soft_turns,
                "extension_turns": self.settings.claude_extension_turns,
                "max_extensions": self.settings.claude_max_extensions,
                "hard_turns": self.settings.claude_hard_turns,
                "max_no_progress_turns": self.settings.claude_max_no_progress_turns,
                "soft_warned": job.soft_warned,
            },
            "checkpoint_count": store.checkpoint_count(self._db, job.job_id),
            "milestones": list(job.milestones) if job.milestones else [],
        }
        if job.worktree is not None:
            snapshot["base_head"] = job.worktree.base_head
        if job.result is not None:
            snapshot["result"] = job.result
        if job.candidate is not None:
            snapshot["candidate"] = job.candidate
        if job.error is not None:
            snapshot["error"] = job.error
            snapshot["blockers"] = [job.error]
        elif job.pause_reason is not None:
            snapshot["blockers"] = [job.pause_reason]
        else:
            snapshot["blockers"] = []
        if job.pause_reason is not None:
            snapshot["pause_reason"] = job.pause_reason
        if job.committed_sha is not None:
            snapshot["commit_sha"] = job.committed_sha
        # Surface a content-addressed identity for the candidate when one can
        # be computed cheaply from the persisted checkpoints; the canonical
        # fingerprint is still produced at verification time.
        latest = store.load_latest_checkpoint(self._db, job.job_id)
        if latest is not None:
            snapshot["candidate_fingerprint"] = latest.get("partial_fingerprint")
            changed = _json_loads(latest.get("changed_paths_json"))
            if isinstance(changed, list):
                snapshot["changed_path_count"] = len(changed)
                snapshot["changed_paths"] = changed
        return snapshot

    def _require_job(
        self,
        project: ProjectPolicy,
        job_id: str,
    ) -> DevelopmentJob:
        job = self._jobs.get(job_id)
        if job is None or job.project_id != project.id:
            raise DevelopmentJobError(
                f"unknown job_id for project {project.id!r}: {job_id!r}"
            )
        return job

    async def start(
        self,
        project: ProjectPolicy,
        text: str,
        effort: Effort,
    ) -> dict[str, Any]:
        specification = self._validate_input(project, text, effort)
        async with self._state_lock:
            job = DevelopmentJob(
                job_id=self._new_job_id(),
                project_id=project.id,
                effort=effort,
            )
            self._jobs[job.job_id] = job
            self._persist_progress(job)
            self._record_event(job, "created", {"effort": effort})
            job.task = asyncio.create_task(
                self._run(job, project, specification, effort, resume=False),
                name=f"development-job-{job.job_id}",
            )
            self._persist_job(job)
            return self._snapshot(job)

    async def get(
        self,
        project: ProjectPolicy,
        job_id: str,
    ) -> dict[str, Any]:
        async with self._state_lock:
            return self._snapshot(self._require_job(project, job_id))

    async def resume(
        self,
        project: ProjectPolicy,
        job_id: str,
        text: str,
        effort: Effort,
    ) -> dict[str, Any]:
        specification = self._validate_input(project, text, effort)
        async with self._state_lock:
            job = self._require_job(project, job_id)
            if job.task is not None:
                raise DevelopmentJobError("only a terminal development job can resume")
            if job.status not in TERMINAL_STATUSES:
                raise DevelopmentJobError("only a terminal development job can resume")
            if job.status in NON_RESUMABLE_STATUSES:
                raise DevelopmentJobError("this development job is not resumable")
            if job.committed_sha is not None:
                raise DevelopmentJobError("a committed development job cannot resume")
            if job.session_id is None:
                raise DevelopmentJobError(
                    "job has no bridge-created Claude session to resume"
                )
            if job.worktree is None:
                raise DevelopmentJobError("job has no retained worktree to resume")
            is_paused = job.status in PAUSED_STATUSES
            checkpoint = (
                store.load_latest_checkpoint(self._db, job.job_id)
                if is_paused
                else None
            )
            job.effort = effort
            job.attempt += 1
            job.status = JobStatus.QUEUED
            job.updated_at = _timestamp()
            job.cancel_requested = False
            job.result = None
            job.candidate = None
            job.error = None
            self._last_changed_paths.pop(job.job_id, None)
            if is_paused:
                # Continue the paused run: keep the cumulative budget so the
                # hard cap still bounds the whole mission, and clear the pause
                # reason. The resume seed re-states the last candidate state.
                job.pause_reason = None
            else:
                # Resuming a terminal (non-paused) job is a fresh attempt with a
                # full new budget (existing behavior): reset the counters.
                job.turns_used = 0
                job.tokens_used = 0
                job.extensions_granted = 0
                job.no_progress_turns = 0
                job.tool_failures = 0
                job.soft_warned = False
            self._persist_progress(job)
            self._record_event(
                job,
                "status",
                {"status": "queued", "attempt": job.attempt, "paused": is_paused},
            )
            job.task = asyncio.create_task(
                self._run(
                    job,
                    project,
                    specification,
                    effort,
                    resume=True,
                    resume_seed=self._resume_seed(checkpoint),
                ),
                name=f"development-job-{job.job_id}-attempt-{job.attempt}",
            )
            self._persist_job(job)
            return self._snapshot(job)

    async def cancel(
        self,
        project: ProjectPolicy,
        job_id: str,
    ) -> dict[str, Any]:
        async with self._state_lock:
            job = self._require_job(project, job_id)
            task = job.task
            if job.status in TERMINAL_STATUSES or task is None:
                return self._snapshot(job)
            job.cancel_requested = True
            job.status = JobStatus.CANCELLED
            job.updated_at = _timestamp()
            self._persist_job(job)
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        async with self._state_lock:
            if job.task is task:
                job.task = None
                job.updated_at = _timestamp()
                self._persist_job(job)
        return await self.get(project, job_id)

    async def verification_worktree(
        self,
        project: ProjectPolicy,
        job_id: str,
    ) -> JobWorktree:
        async with self._state_lock:
            job = self._require_job(project, job_id)
            if job.status != JobStatus.COMPLETED or job.task is not None:
                raise DevelopmentJobError(
                    "only a completed inactive development job can be verified"
                )
            if job.committed_sha is not None:
                raise DevelopmentJobError("development job is already committed")
            if job.worktree is None:
                raise DevelopmentJobError("job has no retained worktree")
            return job.worktree

    async def record_commit(
        self,
        project: ProjectPolicy,
        job_id: str,
        commit_sha: str,
    ) -> None:
        async with self._state_lock:
            job = self._require_job(project, job_id)
            if job.committed_sha is not None:
                raise DevelopmentJobError("development job is already committed")
            job.committed_sha = commit_sha
            job.session_id = None
            job.updated_at = _timestamp()
            self._persist_job(job)

    async def committed_candidate(
        self,
        project: ProjectPolicy,
        job_id: str,
    ) -> CommittedCandidate:
        """Return the detached commit SHA and retained worktree for a published job.

        Publication operates on a job that has already passed
        ``commit_verified_development_job``: it is completed, inactive, has a
        bridge-created ``committed_sha``, and still retains its worktree (whose
        ``base_head`` is the verified base). This method never exposes the raw
        Claude session ID; it returns only what publication needs.
        """
        async with self._state_lock:
            job = self._require_job(project, job_id)
            if job.status != JobStatus.COMPLETED or job.task is not None:
                raise DevelopmentJobError(
                    "only a completed inactive development job can be published"
                )
            if job.committed_sha is None:
                raise DevelopmentJobError(
                    "development job has no committed detached commit to publish"
                )
            if job.worktree is None:
                raise DevelopmentJobError("job has no retained worktree")
            return CommittedCandidate(
                committed_sha=job.committed_sha,
                worktree=job.worktree,
            )

    async def has_active_jobs(self, project_id: str) -> bool:
        """True if the project has any non-terminal job (queued/preparing/
        running/validating).

        Used by the canonical-checkout sync manager to refuse to move the
        guarded checkout's HEAD while in-flight work depends on the current
        base. The per-project lock is held for the whole job run, so a running
        job blocks sync at the lock; this check catches jobs that are queued
        but have not yet acquired the lock.
        """
        async with self._state_lock:
            return any(
                job.project_id == project_id and job.status not in TERMINAL_STATUSES
                for job in self._jobs.values()
            )

    async def stale_resumable_job_summaries(
        self, project_id: str
    ) -> list[dict[str, Any]]:
        """Completed-but-uncommitted jobs for a project that still retain a
        worktree, as ``{job_id, base_head}``.

        These jobs can still pass ``verify_development_job`` /
        ``commit_verified_development_job`` only while the canonical checkout
        HEAD remains at their ``base_head``. After a canonical-checkout sync
        moves HEAD to a new commit, ``validate_resume`` will reject them. The
        sync manager surfaces these as warnings so the operator knows which
        outstanding candidates lose their verify/commit path. Only ``job_id``
        and ``base_head`` are returned; no worktree path or session ID leaks.
        """
        async with self._state_lock:
            summaries: list[dict[str, Any]] = []
            for job in self._jobs.values():
                if (
                    job.project_id != project_id
                    or job.status != JobStatus.COMPLETED
                    or job.task is not None
                    or job.committed_sha is not None
                    or job.worktree is None
                ):
                    continue
                summaries.append(
                    {
                        "job_id": job.job_id,
                        "base_head": job.worktree.base_head,
                    }
                )
            return summaries

    async def _set_status(
        self,
        job: DevelopmentJob,
        status: JobStatus,
    ) -> None:
        async with self._state_lock:
            if not job.cancel_requested:
                job.status = status
            job.updated_at = _timestamp()
            self._persist_job(job)

    def _maybe_compact(self, job: DevelopmentJob) -> None:
        if store.checkpoint_count(self._db, job.job_id) > self.settings.claude_checkpoint_max:
            store.compact_checkpoints(self._db, job.job_id)

    def _resume_seed(self, checkpoint: dict[str, Any] | None) -> str:
        """Build a short resume-context block from the last paused checkpoint."""
        if not checkpoint:
            return ""
        changed = _json_loads(checkpoint.get("changed_paths_json"))
        if not isinstance(changed, list) or not changed:
            return ""
        lines = ["Resuming a previously paused job. Last recorded candidate changes:"]
        for path in changed[:50]:
            lines.append(f"  - {path}")
        if len(changed) > 50:
            lines.append(f"  ... ({len(changed) - 50} more)")
        fingerprint = checkpoint.get("partial_fingerprint")
        if fingerprint:
            lines.append(f"Last partial fingerprint: {fingerprint}")
        lines.append("Continue from this state; do not redo completed work.")
        return "\n".join(lines)

    async def _run(
        self,
        job: DevelopmentJob,
        project: ProjectPolicy,
        specification: str,
        effort: Effort,
        *,
        resume: bool,
        resume_seed: str = "",
    ) -> None:
        drafter_text = specification
        if resume_seed:
            combined = f"{specification}\n\n{resume_seed}"
            if len(combined) <= self.settings.max_input_chars:
                drafter_text = combined
        terminal: JobStatus | None = None
        applied = False
        try:
            project_lock = await self._locks.for_project(project.id)
            async with project_lock:
                if job.cancel_requested:
                    raise asyncio.CancelledError
                await self._set_status(job, JobStatus.PREPARING_WORKTREE)
                if resume:
                    assert job.worktree is not None
                    await self._worktrees.validate_resume(project, job.worktree)
                else:
                    worktree = await self._worktrees.create(project, job.job_id)
                    async with self._state_lock:
                        job.worktree = worktree
                        job.updated_at = _timestamp()
                        self._persist_job(job)
                        self._record_event(
                            job,
                            "worktree_prepared",
                            {"base_head": worktree.base_head},
                        )

                if job.cancel_requested:
                    raise asyncio.CancelledError
                assert job.worktree is not None
                await self._set_status(job, JobStatus.RUNNING)
                self._record_event(job, "status", {"status": "running"})

                # When resuming a paused job, seed the progress baseline from
                # the last checkpoint so progress detection compares against the
                # paused candidate state rather than an empty set.
                if resume:
                    latest = store.load_latest_checkpoint(self._db, job.job_id)
                    if latest is not None:
                        changed = _json_loads(latest.get("changed_paths_json"))
                        if isinstance(changed, list):
                            self._last_changed_paths[job.job_id] = tuple(changed)

                session_id = job.session_id if resume else None
                slice_index = 0
                while True:
                    if job.cancel_requested:
                        raise asyncio.CancelledError
                    # A fresh job's first slice uses the per-invocation turn limit;
                    # every subsequent (extension or resumed) slice uses the
                    # extension-slice budget.
                    if resume or slice_index > 0:
                        slice_max = self.settings.claude_extension_turns
                    else:
                        slice_max = self.settings.claude_max_turns
                    exec_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(
                        self._executor.execute(
                            project,
                            drafter_text,
                            effort,
                            root=job.worktree.path,
                            resume_session_id=session_id,
                            max_turns=slice_max,
                        )
                    )
                    sampler = asyncio.create_task(self._sampler_loop(job, exec_task))
                    try:
                        result = await exec_task
                    finally:
                        if not exec_task.done():
                            exec_task.cancel()
                        if not sampler.done():
                            sampler.cancel()
                        for task in (exec_task, sampler):
                            try:
                                await task
                            except (asyncio.CancelledError, Exception):
                                pass
                    if job.cancel_requested:
                        raise asyncio.CancelledError

                    self._absorb_result_budget(job, result)
                    await self._record_checkpoint(
                        job, "boundary", event={"slice": slice_index}
                    )
                    # Persist a returned session id on every slice, not only on
                    # completion: a job that pauses (e.g. on a max_turns slice)
                    # still yields a resumable Claude session that ``resume``
                    # must be able to continue.
                    captured_session = result.get("session_id")
                    if _valid_session_id(captured_session):
                        async with self._state_lock:
                            job.session_id = str(captured_session)
                            job.updated_at = _timestamp()
                            self._persist_job(job)
                    self._persist_progress(job)
                    self._maybe_compact(job)

                    claude_status = str(result.get("status", "failed"))
                    terminal_reason = result.get("terminal_reason")

                    if claude_status == "completed":
                        public_result = dict(result)
                        public_result.pop("session_id", None)
                        public_result = _redact(
                            public_result, self._paths_for(project, job)
                        )
                        async with self._state_lock:
                            job.result = public_result
                            job.updated_at = _timestamp()
                            self._persist_job(job)
                            self._persist_progress(job)
                        if project.self_managed:
                            await self._set_status(job, JobStatus.VALIDATING_CANDIDATE)
                            candidate = await self._candidate_checker.check(
                                job.worktree.path
                            )
                            candidate = _redact(
                                candidate, self._paths_for(project, job)
                            )
                            async with self._state_lock:
                                job.candidate = candidate
                                if candidate.get("status") == "healthy":
                                    job.status = JobStatus.COMPLETED
                                else:
                                    job.status = JobStatus.FAILED
                                    job.error = str(
                                        candidate.get(
                                            "error", "candidate health check failed"
                                        )
                                    )[-2000:]
                                job.updated_at = _timestamp()
                                self._persist_job(job)
                                self._persist_progress(job)
                        else:
                            await self._set_status(job, JobStatus.COMPLETED)
                        terminal = job.status
                        applied = True
                        break

                    if claude_status == "timed_out":
                        terminal = JobStatus.TIMED_OUT
                        break

                    # Non-completed: classify against the budget/progress rules
                    # before falling back to a plain candidate failure.
                    if job.tool_failures >= self.settings.claude_max_tool_failures:
                        job.pause_reason = (
                            f"tool failures ({job.tool_failures}) exceeded the limit"
                        )
                        terminal = JobStatus.FAILED_INFRASTRUCTURE
                        break
                    if job.no_progress_turns >= self.settings.claude_max_no_progress_turns:
                        job.pause_reason = (
                            f"no progress for {job.no_progress_turns} turns"
                        )
                        terminal = JobStatus.PAUSED_BUDGET_EXHAUSTED
                        break
                    if job.turns_used >= self.settings.claude_hard_turns:
                        job.pause_reason = (
                            f"hard turn cap reached ({job.turns_used})"
                        )
                        terminal = JobStatus.PAUSED_BUDGET_EXHAUSTED
                        break

                    # Progress-gated in-place extension: only when Claude
                    # explicitly signalled its slice turn limit, the candidate
                    # is still advancing, and extension/progress budget remains.
                    if (
                        terminal_reason == "max_turns"
                        and job.extensions_granted
                        < self.settings.claude_max_extensions
                        and job.no_progress_turns
                        < self.settings.claude_max_no_progress_turns
                        and job.turns_used < self.settings.claude_hard_turns
                        and self._progressed(job)
                    ):
                        job.extensions_granted += 1
                        self._record_event(
                            job,
                            "extension",
                            {
                                "extension": job.extensions_granted,
                                "turns_used": job.turns_used,
                                "no_progress_turns": job.no_progress_turns,
                            },
                        )
                        await self._record_checkpoint(job, "extension")
                        self._persist_progress(job)
                        session_id = job.session_id
                        slice_index += 1
                        await self._set_status(job, JobStatus.RUNNING)
                        continue

                    if claude_status == "failed":
                        terminal = JobStatus.FAILED
                        break

                    # Non-completed and not extendable with no explicit budget
                    # signal: pause rather than discard the in-flight work.
                    job.pause_reason = (
                        f"budget exhausted ({terminal_reason or claude_status})"
                    )
                    terminal = JobStatus.PAUSED_BUDGET_EXHAUSTED
                    break

                if terminal is not None and not applied:
                    await self._set_status(job, terminal)
                    self._record_event(
                        job,
                        "terminal",
                        {
                            "status": terminal.value,
                            "pause_reason": job.pause_reason,
                            "turns_used": job.turns_used,
                            "extensions_granted": job.extensions_granted,
                        },
                    )
                    async with self._state_lock:
                        self._persist_progress(job)
        except asyncio.CancelledError:
            async with self._state_lock:
                job.status = JobStatus.CANCELLED
                job.cancel_requested = True
                job.updated_at = _timestamp()
                self._persist_job(job)
                self._persist_progress(job)
            self._record_event(job, "terminal", {"status": "cancelled"})
            raise
        except Exception as exc:
            safe_error = _redact(str(exc), self._paths_for(project, job))
            async with self._state_lock:
                if not job.cancel_requested:
                    job.status = JobStatus.FAILED
                    job.error = str(safe_error)[-4000:]
                job.updated_at = _timestamp()
                self._persist_job(job)
                self._persist_progress(job)
            self._record_event(
                job, "terminal", {"status": "failed", "error": str(safe_error)[-1000:]}
            )
        finally:
            async with self._state_lock:
                job.task = None
                job.updated_at = _timestamp()
                self._persist_job(job)
                self._persist_progress(job)
