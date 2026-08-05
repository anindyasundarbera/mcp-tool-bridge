from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

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
    task: asyncio.Task[None] | None = None


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
            if candidate not in self._jobs:
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
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        async with self._state_lock:
            if job.task is task:
                job.task = None
                job.updated_at = _timestamp()
        return await self.get(project, job_id)

    async def _set_status(
        self,
        job: DevelopmentJob,
        status: JobStatus,
    ) -> None:
        async with self._state_lock:
            if not job.cancel_requested:
                job.status = status
            job.updated_at = _timestamp()

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
            raise
        except Exception as exc:
            safe_error = _redact(str(exc), self._paths_for(project, job))
            async with self._state_lock:
                if not job.cancel_requested:
                    job.status = JobStatus.FAILED
                    job.error = str(safe_error)[-4000:]
                job.updated_at = _timestamp()
        finally:
            async with self._state_lock:
                job.task = None
                job.updated_at = _timestamp()
