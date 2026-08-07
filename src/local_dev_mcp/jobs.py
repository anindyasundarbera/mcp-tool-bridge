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


TERMINAL_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.TIMED_OUT,
        JobStatus.CANCELLED,
    }
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
        # Durable state: re-hydrate every job from disk so a server restart no
        # longer amnesiates in-flight or completed work. Done synchronously in
        # __init__ (only filesystem/DB work, no await) so it works before the
        # event loop starts.
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")
        self._reload_jobs()

    def _reload_jobs(self) -> None:
        """Load persisted jobs and reconcile any that were mid-flight at crash.

        A job that was not terminal when the previous process died is marked
        FAILED: its asyncio task and Claude process are gone and cannot be
        reattached. A terminal job whose worktree directory has since vanished
        is also marked FAILED. Both keep the retained worktree content for
        inspection; neither is silently lost. Unparseable rows are skipped.
        """
        changed: list[DevelopmentJob] = []
        for row in store.load_job_rows(self._db):
            job = job_from_row(row)
            if job is None:
                continue
            job.task = None
            mutated = False
            if job.status not in TERMINAL_STATUSES:
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
        for job in changed:
            store.upsert_job_row(self._db, job_to_row(job))

    def _persist_job(self, job: DevelopmentJob) -> None:
        """Persist one job as a single ACID transaction.

        Call this inside ``async with self._state_lock`` after mutating a job
        so the durable store mirrors the in-memory state.
        """
        store.upsert_job_row(self._db, job_to_row(job))

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
                and job.committed_sha is None
            ),
        }
        if job.worktree is not None:
            snapshot["base_head"] = job.worktree.base_head
        if job.result is not None:
            snapshot["result"] = job.result
        if job.candidate is not None:
            snapshot["candidate"] = job.candidate
        if job.error is not None:
            snapshot["error"] = job.error
        if job.committed_sha is not None:
            snapshot["commit_sha"] = job.committed_sha
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
            if job.status not in TERMINAL_STATUSES or job.task is not None:
                raise DevelopmentJobError("only a terminal development job can resume")
            if job.committed_sha is not None:
                raise DevelopmentJobError("a committed development job cannot resume")
            if job.session_id is None:
                raise DevelopmentJobError(
                    "job has no bridge-created Claude session to resume"
                )
            if job.worktree is None:
                raise DevelopmentJobError("job has no retained worktree to resume")
            job.effort = effort
            job.attempt += 1
            job.status = JobStatus.QUEUED
            job.updated_at = _timestamp()
            job.cancel_requested = False
            job.result = None
            job.candidate = None
            job.error = None
            job.task = asyncio.create_task(
                self._run(job, project, specification, effort, resume=True),
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

    async def _run(
        self,
        job: DevelopmentJob,
        project: ProjectPolicy,
        specification: str,
        effort: Effort,
        *,
        resume: bool,
    ) -> None:
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

                if job.cancel_requested:
                    raise asyncio.CancelledError
                assert job.worktree is not None
                await self._set_status(job, JobStatus.RUNNING)
                result = await self._executor.execute(
                    project,
                    specification,
                    effort,
                    root=job.worktree.path,
                    resume_session_id=job.session_id if resume else None,
                )
                if job.cancel_requested:
                    raise asyncio.CancelledError

                claude_status = str(result.get("status", "failed"))
                captured_session = result.get("session_id")
                public_result = dict(result)
                public_result.pop("session_id", None)
                public_result = _redact(
                    public_result,
                    self._paths_for(project, job),
                )

                async with self._state_lock:
                    if claude_status == "completed" and _valid_session_id(captured_session):
                        job.session_id = str(captured_session)
                    job.result = public_result
                    job.updated_at = _timestamp()
                    self._persist_job(job)

                if claude_status == "completed" and project.self_managed:
                    await self._set_status(job, JobStatus.VALIDATING_CANDIDATE)
                    candidate = await self._candidate_checker.check(job.worktree.path)
                    candidate = _redact(candidate, self._paths_for(project, job))
                    async with self._state_lock:
                        job.candidate = candidate
                        if candidate.get("status") == "healthy":
                            job.status = JobStatus.COMPLETED
                        else:
                            job.status = JobStatus.FAILED
                            job.error = str(
                                candidate.get("error", "candidate health check failed")
                            )[-2000:]
                        job.updated_at = _timestamp()
                        self._persist_job(job)
                else:
                    terminal = {
                        "completed": JobStatus.COMPLETED,
                        "timed_out": JobStatus.TIMED_OUT,
                        "failed": JobStatus.FAILED,
                    }.get(claude_status, JobStatus.FAILED)
                    await self._set_status(job, terminal)
        except asyncio.CancelledError:
            async with self._state_lock:
                job.status = JobStatus.CANCELLED
                job.cancel_requested = True
                job.updated_at = _timestamp()
                self._persist_job(job)
            raise
        except Exception as exc:
            safe_error = _redact(str(exc), self._paths_for(project, job))
            async with self._state_lock:
                if not job.cancel_requested:
                    job.status = JobStatus.FAILED
                    job.error = str(safe_error)[-4000:]
                job.updated_at = _timestamp()
                self._persist_job(job)
        finally:
            async with self._state_lock:
                job.task = None
                job.updated_at = _timestamp()
                self._persist_job(job)
