from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
import time
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


def resolve_effort(
    project: ProjectPolicy,
    effort: Effort | None,
    settings: Settings,
) -> Effort:
    """Resolve a possibly-``None`` effort to a concrete value.

    Precedence: the explicit per-call value (if any) → the project's
    ``default_effort`` → the global ``CLAUDE_DEFAULT_EFFORT``. The returned
    value is still subject to ``validate_effort`` (per-project
    ``allow_ultracode``) at the call site, so a global ``ultracode`` default
    against a disallowing project fails closed at job start.
    """
    if effort is not None:
        return effort
    if project.default_effort is not None:
        return project.default_effort
    return settings.claude_default_effort


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

# M-CT-05: tool-use names that signal the drafter is waiting for human input.
# When the executor result carries a ``tool_uses`` list with one of these
# names, the job transitions to BLOCKED_INPUT_REQUIRED (resumable) rather than
# being discarded as a candidate failure.
_INPUT_ASKING_TOOL_NAMES = frozenset(
    {
        "ask_question",
        "request_input",
        "ask_followup_question",
        "ask_user",
        "request_human_input",
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

# M-AL-01: hard ceiling on ``list_development_jobs`` so a project with very many
# durable jobs still returns a bounded response. The caller's ``limit`` is
# clamped to this value before slicing.
MAX_LIST_JOBS = 200


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _new_authority_id() -> str:
    """Return a random authority-record identifier (M-EV-05)."""
    return f"auth-{secrets.token_hex(16)}"


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


def _milestone_summary(milestone: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, redacted summary of one milestone (M-CT-04).

    The terminal report partitions milestones by closure into
    ``completed_milestones`` / ``remaining_milestones``. Each entry exposes only
    the safe identity/closure fields (``id``, ``title``, ``closed`` and, when
    closed, ``closed_at``) — never the ``acceptance_paths`` or
    ``acceptance_command_id`` (which are policy/verification wiring, not report
    data). The flat ``milestones`` field is kept for back-compat.
    """
    summary: dict[str, Any] = {
        "id": milestone.get("id"),
        "title": milestone.get("title"),
        "closed": bool(milestone.get("closed")),
    }
    closed_at = milestone.get("closed_at")
    if closed_at is not None:
        summary["closed_at"] = closed_at
    return summary


def _extract_detail_field(
    result: dict[str, Any] | None,
    event: dict[str, Any] | None,
    field: str,
    stream_event_type: str,
) -> Any:
    """Best-effort extraction of one checkpoint detail field (M-CT-03).

    Priority: a top-level field on the executor result → stream events of the
    matching ``type`` (their ``data`` payload, or the event minus ``type``) →
    a top-level field on the checkpoint event dict. Returns ``None`` when no
    source yields a value. Never raises — a malformed source yields ``None``.
    """
    if result is not None:
        value = result.get(field)
        if value is not None:
            return value
        events = result.get("stream_events")
        if isinstance(events, list):
            collected: list[Any] = []
            for item in events:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != stream_event_type:
                    continue
                data = item.get("data")
                if data is None:
                    data = {k: v for k, v in item.items() if k != "type"}
                collected.append(data)
            if collected:
                return collected
    if event is not None:
        value = event.get(field)
        if value is not None:
            return value
    return None


def _parse_checkpoint_detail(
    event: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> dict[str, str | None]:
    """Parse the rich checkpoint detail fields from executor events (M-CT-03).

    Returns a dict with the five nullable JSON columns
    (``completed_steps_json`` / ``remaining_steps_json`` / ``tests_json`` /
    ``decisions_json`` / ``tool_history_json``). Non-fatal: any parse failure
    yields all-NULL fields so the checkpoint is still recorded — the detail
    row is additive enrichment that must never block checkpointing.
    """
    try:
        completed = _extract_detail_field(
            result, event, "completed_steps", "step_completed"
        )
        remaining = _extract_detail_field(
            result, event, "remaining_steps", "step_remaining"
        )
        tests = _extract_detail_field(result, event, "tests", "test_result")
        decisions = _extract_detail_field(result, event, "decisions", "decision")
        # Tool history: prefer the executor's ``tool_uses`` list, then a
        # top-level ``tool_history`` field, then the checkpoint event.
        tool_history: Any = None
        if result is not None:
            tool_uses = result.get("tool_uses")
            if isinstance(tool_uses, list):
                tool_history = tool_uses
            elif result.get("tool_history") is not None:
                tool_history = result.get("tool_history")
        if tool_history is None and event is not None:
            tool_history = event.get("tool_history")
        return {
            "completed_steps_json": _json_dumps(completed),
            "remaining_steps_json": _json_dumps(remaining),
            "tests_json": _json_dumps(tests),
            "decisions_json": _json_dumps(decisions),
            "tool_history_json": _json_dumps(tool_history),
        }
    except Exception:
        return {
            "completed_steps_json": None,
            "remaining_steps_json": None,
            "tests_json": None,
            "decisions_json": None,
            "tool_history_json": None,
        }


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
    # M-CT-02: cumulative wall-time across all slices (seconds). Persisted in
    # the ``job_progress`` sidecar (additive REAL column) so it round-trips
    # across restart. Bounded by the ``claude_hard_wall_seconds`` ceiling.
    wall_seconds_used: float = 0.0
    milestones: list[dict[str, Any]] | None = None
    # M-CT-01: an optional fetched/pinned base commit the worktree was created
    # from (``origin/<default>`` tip), rather than the local canonical HEAD.
    requested_base: str | None = None
    # M-GV-06: parent/child delegation. ``parent_job_id`` links a child job to
    # the job that delegated to it; ``delegation_mandate`` bounds the child's
    # scope (inputs/outputs/authority). Persisted in the ``job_progress``
    # sidecar (additive) so the ``jobs`` schema is untouched.
    parent_job_id: str | None = None
    delegation_mandate: dict[str, Any] | None = None


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
        requirements: Any = None,
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
        self._projects = projects
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
        # M-EV-01: requirement traceability manager. Shares this manager's DB
        # connection so there is a single write path. Lazily constructed if
        # not provided (lazy import to avoid a module-load-time dependency).
        self._requirements: Any = requirements
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

    def _requirements_manager(self) -> Any:
        """Return the RequirementManager, lazily constructing it if needed.

        M-EV-01: the manager shares this job manager's DB connection so there
        is a single write path and no second WAL-lock competitor. Uses a lazy
        import to avoid a module-load-time dependency on ``requirement``.
        """
        if self._requirements is not None:
            return self._requirements
        from .reader import RepositoryReader
        from .requirement import RequirementManager

        self._requirements = RequirementManager(
            self.settings,
            store,
            RepositoryReader(self.settings),
            db=self._db,
        )
        return self._requirements

    def _project_policies(self) -> tuple[ProjectPolicy, ...]:
        """Return the project policies this manager was constructed with."""
        return self._projects

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
            "wall_seconds_used": job.wall_seconds_used,
            "milestones_json": _json_dumps(job.milestones),
            "parent_job_id": job.parent_job_id,
            "delegation_mandate_json": _json_dumps(job.delegation_mandate),
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
            job.wall_seconds_used = float(row.get("wall_seconds_used", 0.0) or 0.0)
        except (TypeError, ValueError):
            return
        job.soft_warned = bool(row.get("soft_warned", 0))
        milestones = _json_loads(row.get("milestones_json"))
        if isinstance(milestones, list):
            job.milestones = milestones
        job.parent_job_id = row.get("parent_job_id")
        mandate = _json_loads(row.get("delegation_mandate_json"))
        if isinstance(mandate, dict):
            job.delegation_mandate = mandate

    def _record_event(
        self,
        job: DevelopmentJob,
        event_type: str,
        detail: dict[str, Any] | None = None,
        *,
        authority: dict[str, Any] | None = None,
    ) -> None:
        """Append one row to the append-only audit ledger.

        M-EV-05: governance-changing events (status / extension / pause /
        terminal) are stamped with an ``authority`` block carrying the actor,
        authority source, scope, and source classification. The ``authority_id``
        is embedded in the event's ``detail_json`` and the full record is
        persisted to the sidecar ``authority_records`` table so the authority
        behind every governance instruction is durable and queryable.
        """
        detail = dict(detail or {})
        if authority is not None:
            authority_id = authority.get("authority_id") or _new_authority_id()
            detail["authority_id"] = authority_id
            store.insert_authority_record(
                self._db,
                {
                    "authority_id": authority_id,
                    "job_id": job.job_id,
                    "event_id": None,
                    "actor": authority.get("actor", "bridge"),
                    "authority_source": authority.get("authority_source", "bridge_internal"),
                    "scope": authority.get("scope", event_type),
                    "classification": authority.get("classification", "observed"),
                    "recorded_at": _timestamp(),
                    "detail_json": _json_dumps(authority.get("detail") or {}),
                },
            )
        store.insert_event_row(
            self._db,
            {
                "job_id": job.job_id,
                "project_id": job.project_id,
                "occurred_at": _timestamp(),
                "event_type": event_type,
                "detail_json": _json_dumps(detail),
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
        result: dict[str, Any] | None = None,
    ) -> tuple[str, ...]:
        """Append one checkpoint row and update no-progress tracking.

        Computes the current changed-path set and a best-effort partial
        fingerprint over the worktree, persists both as an append-only
        checkpoint, and advances the no-progress counter: it resets to 0 when
        the changed set changes and increments when it is unchanged from the
        last recorded checkpoint. Returns the current changed-path set.

        M-CT-03: when the executor ``result`` is available (boundary /
        extension checkpoints), a sidecar ``job_checkpoint_detail`` row is
        attached carrying the rich fields (completed/remaining steps, tests,
        decisions, tool history) parsed from the result's structured stream
        events. Non-fatal: a parse failure or missing events never prevent the
        checkpoint from being recorded — the detail row simply has NULL
        fields.
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
        checkpoint_id = store.insert_checkpoint_row(
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
        # M-CT-03: attach the rich-field detail sidecar row. Non-fatal: a parse
        # failure or a missing checkpoint_id never blocks checkpointing.
        try:
            detail = _parse_checkpoint_detail(event, result)
            detail["checkpoint_id"] = checkpoint_id
            store.insert_checkpoint_detail_row(self._db, detail)
        except Exception:
            pass
        return paths

    def _progressed(self, job: DevelopmentJob) -> bool:
        """True if the last checkpoint's changed set differs from the prior one."""
        current = self._last_changed_paths.get(job.job_id)
        return bool(current)

    def _blocked_input_signal(self, result: dict[str, Any]) -> str | None:
        """M-CT-05: detect a structured 'input required' signal from the drafter.

        Returns the human-readable blocker message when the executor result
        carries one of the recognized blocked-input signals:

        - a fenced ``<!-- bridge: blocked-input ... -->`` block in its
          ``result`` text;
        - a ``blocked_input`` string field;
        - a stream-json structured ``input_required`` event (a top-level
          ``input_required`` dict, or an entry in ``stream_events`` with
          ``type == "input_required"``, or a tool-use in ``tool_uses`` whose
          name is a recognized input-asking tool).

        Returns ``None`` when no such signal is present, so a normal candidate
        failure stays ``failed``. All detection is additive: the existing
        markers keep working unchanged.
        """
        marker = "<!-- bridge: blocked-input"
        text = result.get("result")
        if isinstance(text, str) and marker in text:
            start = text.index(marker) + len(marker)
            end = text.find("-->", start)
            message = text[start:end].strip() if end != -1 else text[start:].strip()
            return message or "the drafter requested additional input"
        blocked = result.get("blocked_input")
        if isinstance(blocked, str) and blocked.strip():
            return blocked.strip()
        # M-CT-05: stream-json structured "input required" signal. A direct
        # ``input_required`` dict carrying a human-readable message.
        direct = result.get("input_required")
        if isinstance(direct, dict):
            message = direct.get("message") or direct.get("reason")
            if isinstance(message, str) and message.strip():
                return message.strip()
        # A stream-json event list carrying an ``input_required`` event.
        events = result.get("stream_events")
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "input_required":
                    message = event.get("message") or event.get("reason")
                    if isinstance(message, str) and message.strip():
                        return message.strip()
        # A tool-use of an input-asking shape (e.g. ask_question /
        # request_input) — the drafter is waiting on the operator, not
        # exhausting its budget.
        tool_uses = result.get("tool_uses")
        if isinstance(tool_uses, list):
            for use in tool_uses:
                if not isinstance(use, dict):
                    continue
                if use.get("name") in _INPUT_ASKING_TOOL_NAMES:
                    message = use.get("message") or use.get("question") or use.get("input")
                    if isinstance(message, str) and message.strip():
                        return message.strip()
        return None

    async def _close_milestones(
        self,
        job: DevelopmentJob,
        project: ProjectPolicy,
        changed_paths: tuple[str, ...],
    ) -> None:
        """M-CT-04: close any milestone whose acceptance criteria are now met.

        A milestone is a dict with ``id``, ``title``, ``acceptance_paths``
        (ordered list of paths that must all appear in the changed set) and an
        optional ``acceptance_command_id`` (a verification command id that must
        pass). A milestone closes only when its acceptance paths are present
        AND (if a command is named) that command passes in the worktree. Closed
        milestones are marked ``closed: True`` with a ``closed_at`` stamp and
        recorded as a ``milestone`` checkpoint. Unclosed milestones stay open
        and surface in the terminal report as remaining.
        """
        if not job.milestones or job.worktree is None:
            return
        changed = set(changed_paths)
        for milestone in job.milestones:
            if milestone.get("closed"):
                continue
            acceptance_paths = milestone.get("acceptance_paths") or []
            if not isinstance(acceptance_paths, list) or not acceptance_paths:
                continue
            if not all(path in changed for path in acceptance_paths):
                continue
            command_id = milestone.get("acceptance_command_id")
            if command_id is not None:
                command = next(
                    (
                        command
                        for command in project.verification_commands
                        if command.id == command_id
                    ),
                    None,
                )
                if command is None:
                    # Unknown command id: leave the milestone open; do not
                    # fabricate acceptance.
                    continue
                from .verification import _run_command

                try:
                    outcome = await _run_command(self.settings, job.worktree, command)
                except Exception:
                    # A transient acceptance-command failure must not fail the
                    # whole job; leave the milestone open and continue.
                    continue
                if outcome.get("status") != "passed":
                    continue
            milestone["closed"] = True
            milestone["closed_at"] = _timestamp()
            await self._record_checkpoint(
                job, "milestone", milestone=milestone.get("id")
            )
            async with self._state_lock:
                self._persist_progress(job)

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

    def _absorb_result_budget(
        self,
        job: DevelopmentJob,
        result: dict[str, Any],
        *,
        slice_wall_seconds: float = 0.0,
    ) -> None:
        """Accumulate one executor slice's budget counters.

        Turns, tokens, and tool-failures come from the executor result dict.
        Wall-time is accumulated cumulatively across slices (M-CT-02). The
        executor MAY report ``wall_seconds`` (a positive number) in its result;
        when present and positive, that value is preferred (it is the most
        accurate measurement the executor has). When absent or non-positive,
        the caller passes ``slice_wall_seconds`` — the elapsed wall-time
        around the executor call measured by ``time.monotonic()`` in ``_run``.
        This is the robust approach: real executors do not report wall-time
        today, so the monotonic fallback is the default path, but an executor
        that gains a wall-time field is honored automatically.

        Compute budget (M-CT-02): the executor contract exposes no compute
        units (no ``compute_units`` / ``compute_seconds`` field). Rather than
        fabricate a compute metric that is not measured, tokens (``tokens_used``)
        serve as the compute proxy. This is documented here, not invented as a
        new field, so no unmeasured metric enters the snapshot.
        """
        num_turns = result.get("num_turns")
        if isinstance(num_turns, (int, float)) and num_turns > 0:
            job.turns_used += int(num_turns)
        self._accumulate_tokens(job, result.get("usage"))
        tool_failures = result.get("tool_failures")
        if isinstance(tool_failures, int) and tool_failures > 0:
            job.tool_failures += tool_failures
        # M-CT-02: cumulative wall-time. Prefer executor-reported wall_seconds
        # when present and positive; else use the monotonic fallback.
        reported_wall = result.get("wall_seconds")
        if isinstance(reported_wall, (int, float)) and reported_wall > 0:
            job.wall_seconds_used += float(reported_wall)
        elif slice_wall_seconds > 0:
            job.wall_seconds_used += slice_wall_seconds
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

    def _validate_mandate(self, mandate: dict[str, Any] | None) -> dict[str, Any]:
        """Validate a delegation mandate's structure.

        A mandate is a dict with at most ``inputs`` / ``outputs`` / ``authority``
        keys, each a list of non-empty strings (paths or capability names), and
        must declare a non-empty ``outputs`` list (the only paths the child may
        change). Anything else — including a missing mandate — is rejected so a
        malformed, oversized, or fail-open mandate can never be persisted or
        echoed into a snapshot.
        """
        if mandate is None:
            raise DevelopmentJobError("delegation mandate is required")
        if not isinstance(mandate, dict):
            raise DevelopmentJobError("delegation mandate must be a dict")
        allowed = {"inputs", "outputs", "authority"}
        unknown = set(mandate) - allowed
        if unknown:
            raise DevelopmentJobError(
                f"delegation mandate has unsupported keys: {sorted(unknown)}"
            )
        # A delegation mandate must declare its outputs: the child's changed
        # paths are bounded to them, so an output-less mandate would silently
        # disable the scope bound (fail-open). Requiring outputs keeps the
        # enforcement fail-closed.
        if "outputs" not in mandate:
            raise DevelopmentJobError(
                "delegation mandate must declare a non-empty 'outputs' list"
            )
        for key in allowed:
            value = mandate.get(key)
            if value is None:
                continue
            if not isinstance(value, list) or not value:
                raise DevelopmentJobError(
                    f"delegation mandate {key!r} must be a non-empty list"
                )
            if len(value) > 100:
                raise DevelopmentJobError(
                    f"delegation mandate {key!r} exceeds 100 entries"
                )
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    raise DevelopmentJobError(
                        f"delegation mandate {key!r} entries must be non-empty strings"
                    )
        return mandate

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
                # M-CT-02: cumulative wall-time + ceiling. ``hard_wall_seconds``
                # is 0 when disabled (the default). ``wall_seconds_used`` is the
                # cumulative elapsed wall-time across all slices.
                "wall_seconds_used": job.wall_seconds_used,
                "hard_wall_seconds": self.settings.claude_hard_wall_seconds,
            },
            "checkpoint_count": store.checkpoint_count(self._db, job.job_id),
            "milestones": list(job.milestones) if job.milestones else [],
        }
        # M-CT-04: partition milestones by closure for the terminal report.
        # ``completed_milestones`` / ``remaining_milestones`` are additive to
        # the flat ``milestones`` field (kept for back-compat) and surface only
        # stable, redacted summaries (id/title/closed) — never acceptance paths
        # or command ids. An empty milestone list yields empty partitions.
        milestones = job.milestones or []
        snapshot["completed_milestones"] = [
            _milestone_summary(milestone)
            for milestone in milestones
            if milestone.get("closed")
        ]
        snapshot["remaining_milestones"] = [
            _milestone_summary(milestone)
            for milestone in milestones
            if not milestone.get("closed")
        ]
        if job.parent_job_id is not None:
            snapshot["parent_job_id"] = job.parent_job_id
        if job.delegation_mandate is not None:
            snapshot["delegation_mandate"] = job.delegation_mandate
        # M-GV-06: surface the parent/child delegation tree (children of this
        # job, from the delegations ledger) so an orchestrator can query it.
        children = store.load_delegation_rows_for_parent(self._db, job.job_id)
        if children:
            snapshot["children"] = [
                {
                    "child_job_id": row.get("child_job_id"),
                    "mandate": _json_loads(row.get("mandate_json")),
                    "created_at": row.get("created_at"),
                }
                for row in children
            ]
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
            # M-CT-03: surface the rich detail fields (completed/remaining
            # steps, tests, decisions, tool history) from the latest
            # checkpoint's sidecar row so an orchestrator can replay the
            # structured context. Additive: absent when no detail row exists
            # or all fields are NULL. Non-fatal: a read failure is swallowed.
            try:
                detail_rows = store.load_checkpoint_detail_rows(
                    self._db, [int(latest["checkpoint_id"])]
                )
                if detail_rows:
                    detail = detail_rows[0]
                    parsed = {
                        "completed_steps": _json_loads(
                            detail.get("completed_steps_json")
                        ),
                        "remaining_steps": _json_loads(
                            detail.get("remaining_steps_json")
                        ),
                        "tests": _json_loads(detail.get("tests_json")),
                        "decisions": _json_loads(detail.get("decisions_json")),
                        "tool_history": _json_loads(
                            detail.get("tool_history_json")
                        ),
                    }
                    if any(value is not None for value in parsed.values()):
                        snapshot["checkpoint_detail"] = parsed
            except Exception:
                pass
        # M-EV-05: surface the authority records behind this job's governance
        # events (actor, authority source, scope, classification) so an
        # orchestrator can audit who/what authorized each instruction.
        authority_records = store.load_authority_records(self._db, job.job_id)
        if authority_records:
            snapshot["authority_records"] = [
                {
                    "authority_id": row.get("authority_id"),
                    "actor": row.get("actor"),
                    "authority_source": row.get("authority_source"),
                    "scope": row.get("scope"),
                    "classification": row.get("classification"),
                    "recorded_at": row.get("recorded_at"),
                }
                for row in authority_records
            ]
        # M-EV-01: surface a bounded requirements_covered summary so an
        # orchestrator can see covered/uncovered/deferred/contradictory counts
        # and the list of uncovered normative IDs. Non-fatal: a project with
        # no parsed requirements yields an empty summary. Uses the sync
        # variant because ``_snapshot`` is a sync method.
        try:
            req_manager = self._requirements_manager()
            summary = req_manager.requirements_covered_summary_sync(
                job.project_id, job_id=job.job_id
            )
            if summary.get("normative", 0) > 0:
                snapshot["requirements_covered"] = summary
        except Exception:
            pass
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

    def new_job_id(self) -> str:
        """Allocate a unique job ID without starting a Claude run (MCPB-012).

        Working-tree candidate capture needs a job ID before the worktree is
        created (the worktree path is derived from it) and before a job record
        exists, so the capture manager allocates the ID here and later hands it
        to ``register_captured_candidate``. Thread-safe against the in-memory
        and durable stores via the same uniqueness check ``_new_job_id`` uses.
        """
        return self._new_job_id()

    async def register_captured_candidate(
        self,
        project: ProjectPolicy,
        job_id: str,
        worktree: JobWorktree,
        candidate: dict[str, Any],
        *,
        effort: Effort | None = None,
    ) -> DevelopmentJob:
        """Register a retained candidate captured from a dirty working tree.

        MCPB-012: creates a development job directly in the ``COMPLETED``,
        inactive, uncommitted state — never running Claude — so the existing
        ``verification_worktree`` / ``committed_candidate`` / review / verify /
        commit / publish gates accept it unchanged. The capture manager owns
        worktree creation and overlay; this method only durably registers the
        result (job row, progress sidecar, audit events, authority record) so
        the captured candidate is restart-safe and indistinguishable from a
        Claude-produced candidate at the gate boundary.
        """
        effort = resolve_effort(project, effort, self.settings)
        validate_effort(project, effort)
        async with self._state_lock:
            if job_id in self._jobs or store.job_id_exists(self._db, job_id):
                raise DevelopmentJobError("captured job id already exists")
            job = DevelopmentJob(
                job_id=job_id,
                project_id=project.id,
                effort=effort,
                status=JobStatus.COMPLETED,
                worktree=worktree,
                candidate=candidate,
            )
            self._jobs[job.job_id] = job
            self._persist_progress(job)
            self._record_event(
                job,
                "created",
                {"capture": True, "base_head": worktree.base_head},
                authority={
                    "actor": "ChatGPT",
                    "authority_source": "chatgpt_confirmation",
                    "scope": "working_tree_capture",
                    "classification": "observed",
                    "detail": {"base_head": worktree.base_head},
                },
            )
            self._record_event(
                job,
                "terminal",
                {"status": "completed", "capture": True},
                authority={
                    "actor": "bridge",
                    "authority_source": "bridge_internal",
                    "scope": "terminal_transition",
                    "classification": "observed",
                },
            )
            self._persist_job(job)
            return job

    async def start(
        self,
        project: ProjectPolicy,
        text: str,
        effort: Effort | None = None,
        requested_base: str | None = None,
    ) -> dict[str, Any]:
        effort = resolve_effort(project, effort, self.settings)
        specification = self._validate_input(project, text, effort)
        # M-EV-01: scan the project's canonical spec for normative requirement
        # IDs and persist them (idempotent) so the coverage matrix is available
        # before any edit. Failures are non-fatal: a project with no parseable
        # requirements simply has an empty coverage set.
        try:
            req_manager = self._requirements_manager()
            await req_manager.scan(project)
        except Exception:
            pass
        async with self._state_lock:
            job = DevelopmentJob(
                job_id=self._new_job_id(),
                project_id=project.id,
                effort=effort,
                requested_base=requested_base,
            )
            self._jobs[job.job_id] = job
            self._persist_progress(job)
            self._record_event(
                job,
                "created",
                {"effort": effort},
                authority={
                    "actor": "ChatGPT",
                    "authority_source": "chatgpt_confirmation",
                    "scope": "job_creation",
                    "classification": "canonical",
                },
            )
            job.task = asyncio.create_task(
                self._run(job, project, specification, effort, resume=False),
                name=f"development-job-{job.job_id}",
            )
            self._persist_job(job)
            return self._snapshot(job)

    async def delegate(
        self,
        project: ProjectPolicy,
        parent_job_id: str,
        text: str,
        effort: Effort | None = None,
        *,
        mandate: dict[str, Any],
    ) -> dict[str, Any]:
        """M-GV-06: spawn a bounded child job delegated from a parent job.

        The child is bounded to the parent's authorized scope: it must belong
        to the same project, the parent must be a real job, the parent must
        not have exceeded ``MAX_DELEGATIONS_PER_JOB``, and the mandate must
        declare the child's ``outputs`` (the only paths it may change). The
        delegation is recorded in the ``delegations`` ledger (parent -> child)
        with the mandate, and the child's ``parent_job_id`` /
        ``delegation_mandate`` are persisted on its ``job_progress`` sidecar.
        Internal-first: no new MCP tool; a parent job spawns children via this
        manager method.
        """
        effort = resolve_effort(project, effort, self.settings)
        specification = self._validate_input(project, text, effort)
        mandate = self._validate_mandate(mandate)
        async with self._state_lock:
            parent = self._require_job(project, parent_job_id)
            # M-GV-06 depth bound: a child may not itself delegate, so the
            # delegation tree is at most two levels deep (parent -> child).
            if parent.parent_job_id is not None:
                raise DevelopmentJobError(
                    "a delegated child job cannot delegate further; "
                    "delegation is bounded to one level"
                )
            existing = store.load_delegation_rows_for_parent(self._db, parent_job_id)
            if len(existing) >= self.settings.max_delegations_per_job:
                raise DevelopmentJobError(
                    f"parent job {parent_job_id!r} has reached the delegation "
                    f"limit ({self.settings.max_delegations_per_job})"
                )
            child = DevelopmentJob(
                job_id=self._new_job_id(),
                project_id=project.id,
                effort=effort,
                parent_job_id=parent_job_id,
                delegation_mandate=mandate,
            )
            self._jobs[child.job_id] = child
            self._persist_progress(child)
            self._record_event(
                child,
                "created",
                {"effort": effort, "parent_job_id": parent_job_id},
                authority={
                    "actor": "bridge",
                    "authority_source": "parent_delegation",
                    "scope": "job_creation",
                    "classification": "inferred",
                    "detail": {"parent_job_id": parent_job_id},
                },
            )
            store.insert_delegation_row(
                self._db,
                {
                    "parent_job_id": parent_job_id,
                    "child_job_id": child.job_id,
                    "project_id": project.id,
                    "mandate_json": _json_dumps(mandate),
                    "created_at": _timestamp(),
                },
            )
            child.task = asyncio.create_task(
                self._run(child, project, specification, effort, resume=False),
                name=f"development-job-{child.job_id}",
            )
            self._persist_job(child)
            return self._snapshot(child)

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
        effort: Effort | None = None,
    ) -> dict[str, Any]:
        effort = resolve_effort(project, effort, self.settings)
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
                authority={
                    "actor": "ChatGPT",
                    "authority_source": "chatgpt_confirmation",
                    "scope": "resume",
                    "classification": "canonical",
                },
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

    async def committed_job_summary(
        self,
        project_id: str,
        commit_sha: str,
    ) -> dict[str, Any] | None:
        """Return safe fields of the job whose ``committed_sha`` matches (M-AL-01).

        A read-only introspection accessor for canonicalization reconciliation: it
        finds the project's job that produced ``commit_sha`` and returns only safe
        fields (``job_id``, ``status``, ``base_head``, ``committed_sha``). The raw
        worktree path, session id, result, candidate, and error are never exposed.
        Returns ``None`` when no job for this project committed that SHA. Reads
        under the state lock for consistency with the other job accessors.
        """
        target = commit_sha.lower()
        async with self._state_lock:
            for job in self._jobs.values():
                if (
                    job.project_id == project_id
                    and job.committed_sha is not None
                    and job.committed_sha.lower() == target
                ):
                    return {
                        "job_id": job.job_id,
                        "status": job.status.value,
                        "base_head": (
                            job.worktree.base_head if job.worktree is not None else None
                        ),
                        "committed_sha": job.committed_sha,
                    }
        return None

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

    async def list_jobs(
        self,
        project: ProjectPolicy,
        scope: str = "all",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Enumerate this project's durable jobs as sanitized summaries (M-AL-01).

        Returns a bounded list without requiring a job_id: each entry carries
        only safe fields (job_id, project_id, status, execution phase, effort,
        attempt, timestamps, committed_sha, base_head) plus durable governance
        flags (verification/review/publication presence). The raw worktree path,
        Claude session id, candidate/result/error payloads, and
        cancel_requested are never exposed — mirroring the sanitization proven
        by ``stale_resumable_job_summaries``. ``scope`` selects ``all`` (default),
        ``active`` (non-terminal/in-flight), or ``terminal``; ``limit`` is clamped
        to ``MAX_LIST_JOBS`` and the response reports whether it was truncated.
        """
        if scope not in ("all", "active", "terminal"):
            raise DevelopmentJobError(
                f"invalid scope: {scope!r}; expected all/active/terminal"
            )
        if limit < 0:
            raise DevelopmentJobError("limit must be non-negative")

        async with self._state_lock:
            matching = [
                job
                for job in self._jobs.values()
                if job.project_id == project.id
            ]
        if scope == "active":
            matching = [j for j in matching if j.status not in TERMINAL_STATUSES]
        elif scope == "terminal":
            matching = [j for j in matching if j.status in TERMINAL_STATUSES]

        # Deterministic ordering: newest first by created_at, job_id ascending
        # as a stable tiebreaker (Python sort is stable, so sort secondary first).
        matching.sort(key=lambda job: job.job_id)
        matching.sort(key=lambda job: job.created_at, reverse=True)

        effective_limit = min(limit, MAX_LIST_JOBS)
        truncated = len(matching) > effective_limit
        page = matching[:effective_limit] if effective_limit else []

        # One pass over each durable table for this project (3 queries total),
        # indexed by job_id, so each summary's governance flags are O(1).
        verification_by_job: dict[str, list[dict[str, Any]]] = {}
        for row in store.load_record_rows(self._db):
            if row.get("project_id") == project.id:
                verification_by_job.setdefault(row["job_id"], []).append(row)
        review_by_job: dict[str, list[dict[str, Any]]] = {}
        for row in store.load_review_rows(self._db):
            if row.get("project_id") == project.id:
                review_by_job.setdefault(row["job_id"], []).append(row)
        publication_by_job: dict[str, dict[str, Any]] = {}
        for row in store.load_publication_rows(self._db):
            if row.get("project_id") == project.id:
                publication_by_job[row["job_id"]] = row

        entries = [
            self._list_job_entry(
                job,
                verification_by_job.get(job.job_id, []),
                review_by_job.get(job.job_id, []),
                publication_by_job.get(job.job_id),
            )
            for job in page
        ]
        redaction_paths = (
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )
        return _redact(
            {
                "project_id": project.id,
                "scope": scope,
                "count": len(entries),
                "truncated": truncated,
                "limit_applied": effective_limit,
                "jobs": entries,
            },
            redaction_paths,
        )

    def _list_job_entry(
        self,
        job: DevelopmentJob,
        verification_rows: list[dict[str, Any]],
        review_rows: list[dict[str, Any]],
        publication_row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build one sanitized job summary for ``list_jobs``.

        Exposes only safe fields: never the worktree path, session id, result,
        candidate, error, or cancel_requested. ``base_head`` is the worktree's
        base HEAD (a public SHA), safe to surface. Governance flags come from the
        durable tables only (PR state is in-memory and intentionally omitted).
        """
        entry: dict[str, Any] = {
            "job_id": job.job_id,
            "project_id": job.project_id,
            "status": job.status.value,
            "execution_phase": (
                "terminal" if job.status in TERMINAL_STATUSES else "in_flight"
            ),
            "effort": job.effort,
            "attempt": job.attempt,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "committed": job.committed_sha is not None,
            "verification_present": bool(verification_rows),
            "review_present": bool(review_rows),
            "publication_present": publication_row is not None,
        }
        if job.committed_sha is not None:
            entry["committed_sha"] = job.committed_sha
        if job.worktree is not None:
            entry["base_head"] = job.worktree.base_head
        if publication_row is not None:
            entry["publication_branch"] = publication_row.get("branch_name")
        return entry

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

    async def quarantine_provenance(
        self,
        project: ProjectPolicy,
        job_id: str,
        reason: str,
    ) -> None:
        """M-CT-05: transition a job to QUARANTINED_PROVENANCE (terminal, non-resumable).

        Called when verification/publication detects provenance corruption: a
        fingerprint mismatch on an already-verified candidate, or a committed
        state that no longer re-derives. The status is persisted durably (job
        row + progress sidecar) and an audit event is recorded with the
        quarantine reason. The job is terminal and must never be resumed.

        The existing exception path is preserved: the caller still raises
        ``VerificationError`` / ``PublicationError`` so the orchestrator is
        informed; this method durably records the quarantine so the corruption
        is not silent. Genuine infrastructure errors (GitCommandError,
        WorktreeError, etc.) do NOT call this — only provenance corruption
        quarantines.
        """
        async with self._state_lock:
            job = self._jobs.get(job_id)
            if job is None or job.project_id != project.id:
                return
            job.status = JobStatus.QUARANTINED_PROVENANCE
            job.error = reason[-2000:]
            job.pause_reason = reason[-2000:]
            job.updated_at = _timestamp()
            self._persist_job(job)
            self._persist_progress(job)
            self._record_event(
                job,
                "terminal",
                {
                    "status": "quarantined_provenance",
                    "reason": reason[-1000:],
                },
                authority={
                    "actor": "bridge",
                    "authority_source": "bridge_internal",
                    "scope": "provenance_quarantine",
                    "classification": "observed",
                },
            )

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
        # M-CT-03: enrich the resume seed with the rich detail fields
        # (completed/remaining steps, decisions) so the resumed drafter has the
        # structured context from the last checkpoint. Additive: appended only
        # when a detail row exists with non-null fields. Non-fatal.
        try:
            detail_rows = store.load_checkpoint_detail_rows(
                self._db, [int(checkpoint["checkpoint_id"])]
            )
            if detail_rows:
                detail = detail_rows[0]
                completed = _json_loads(detail.get("completed_steps_json"))
                if isinstance(completed, list) and completed:
                    lines.append("Completed steps:")
                    for step in completed[:20]:
                        if isinstance(step, dict):
                            lines.append(f"  - {step.get('step') or step}")
                        else:
                            lines.append(f"  - {step}")
                remaining = _json_loads(detail.get("remaining_steps_json"))
                if isinstance(remaining, list) and remaining:
                    lines.append("Remaining steps:")
                    for step in remaining[:20]:
                        if isinstance(step, dict):
                            lines.append(f"  - {step.get('step') or step}")
                        else:
                            lines.append(f"  - {step}")
                decisions = _json_loads(detail.get("decisions_json"))
                if isinstance(decisions, list) and decisions:
                    lines.append("Prior decisions:")
                    for decision in decisions[:10]:
                        if isinstance(decision, dict):
                            lines.append(f"  - {decision.get('choice') or decision}")
                        else:
                            lines.append(f"  - {decision}")
        except Exception:
            pass
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
                    worktree = await self._worktrees.create(
                        project, job.job_id, requested_base=job.requested_base
                    )
                    async with self._state_lock:
                        job.worktree = worktree
                        job.updated_at = _timestamp()
                        self._persist_job(job)
                        self._record_event(
                            job,
                            "worktree_prepared",
                            {"base_head": worktree.base_head},
                            authority={
                                "actor": "bridge",
                                "authority_source": "bridge_internal",
                                "scope": "worktree_preparation",
                                "classification": "observed",
                            },
                        )

                if job.cancel_requested:
                    raise asyncio.CancelledError
                assert job.worktree is not None
                await self._set_status(job, JobStatus.RUNNING)
                self._record_event(
                    job,
                    "status",
                    {"status": "running"},
                    authority={
                        "actor": "bridge",
                        "authority_source": "bridge_internal",
                        "scope": "status_transition",
                        "classification": "observed",
                    },
                )

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
                    # M-CT-02: measure the slice wall-time around the executor
                    # call as the robust fallback for wall-time accumulation.
                    # ``time.monotonic()`` is not affected by system clock
                    # adjustments and is the right clock for elapsed measurement.
                    slice_start = time.monotonic()
                    try:
                        result = await exec_task
                    finally:
                        slice_wall = time.monotonic() - slice_start
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

                    self._absorb_result_budget(
                        job, result, slice_wall_seconds=slice_wall
                    )
                    boundary_paths = await self._record_checkpoint(
                        job, "boundary", event={"slice": slice_index}, result=result
                    )
                    # M-CT-04: close any milestone whose acceptance criteria are
                    # now met (paths present + optional acceptance command passes).
                    await self._close_milestones(job, project, boundary_paths)
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
                        # M-GV-06 scope enforcement: a delegated child's changed
                        # paths must stay within the mandate's declared outputs.
                        # If the child touched anything outside its mandate, the
                        # candidate is rejected rather than silently promoted.
                        # Fail-closed: an empty changed-path read (transient git
                        # error) rejects the child rather than promoting it.
                        # Gate on parent_job_id (not delegation_mandate) so a
                        # legacy child row with a NULL mandate still enforces.
                        if job.parent_job_id is not None:
                            if job.delegation_mandate is None:
                                # A legacy child row with a NULL mandate cannot
                                # be scope-checked; fail closed rather than
                                # promote it unbounded.
                                async with self._state_lock:
                                    job.status = JobStatus.FAILED
                                    job.error = (
                                        "delegated child has no mandate; "
                                        "refusing to promote an unbounded child"
                                    )
                                    job.updated_at = _timestamp()
                                    self._persist_job(job)
                                    self._persist_progress(job)
                                terminal = job.status
                                applied = True
                                break
                            allowed = set(job.delegation_mandate.get("outputs"))
                            changed = await self._current_changed_paths(job)
                            out_of_scope = [
                                path for path in changed if path not in allowed
                            ]
                            if not changed or out_of_scope:
                                reason = (
                                    "delegated child changed paths outside its "
                                    "mandate: " + ", ".join(sorted(out_of_scope))[:2000]
                                    if out_of_scope
                                    else "delegated child produced no verifiable "
                                    "changed paths at completion"
                                )
                                async with self._state_lock:
                                    job.status = JobStatus.FAILED
                                    job.error = reason
                                    job.updated_at = _timestamp()
                                    self._persist_job(job)
                                    self._persist_progress(job)
                                terminal = job.status
                                applied = True
                                break
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
                        # M-EV-01: persist requirement_links for the candidate's
                        # changed paths so the coverage matrix reflects this
                        # job's contribution. Non-fatal: a project with no
                        # parsed requirements simply has no links to persist.
                        try:
                            req_manager = self._requirements_manager()
                            req_manager.persist_links_for_candidate(
                                project, job.job_id, boundary_paths
                            )
                        except Exception:
                            pass
                        terminal = job.status
                        applied = True
                        break

                    if claude_status == "timed_out":
                        terminal = JobStatus.TIMED_OUT
                        break

                    # M-CT-05: a structured 'input required' signal pauses the
                    # job as BLOCKED_INPUT_REQUIRED (resumable) rather than
                    # discarding it as a candidate failure. This takes
                    # precedence over the tool-failure/no-progress budget
                    # terminations below: a blocked job is waiting on the
                    # operator, not exhausting its budget.
                    blocked_message = self._blocked_input_signal(result)
                    if blocked_message is not None:
                        job.pause_reason = blocked_message
                        terminal = JobStatus.BLOCKED_INPUT_REQUIRED
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
                    # M-CT-02: cumulative wall-time ceiling. Only enforced when
                    # ``claude_hard_wall_seconds`` is positive (opt-in); 0/unset
                    # (the default) leaves existing projects unaffected.
                    if (
                        self.settings.claude_hard_wall_seconds > 0
                        and job.wall_seconds_used
                        >= self.settings.claude_hard_wall_seconds
                    ):
                        job.pause_reason = (
                            "cumulative_wall_budget_exhausted"
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
                            authority={
                                "actor": "bridge",
                                "authority_source": "budget_policy",
                                "scope": "extension",
                                "classification": "inferred",
                            },
                        )
                        await self._record_checkpoint(job, "extension", result=result)
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
                        authority={
                            "actor": "bridge",
                            "authority_source": "bridge_internal",
                            "scope": "terminal_transition",
                            "classification": "observed",
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
            self._record_event(
                job,
                "terminal",
                {"status": "cancelled"},
                authority={
                    "actor": "ChatGPT",
                    "authority_source": "chatgpt_confirmation",
                    "scope": "cancellation",
                    "classification": "canonical",
                },
            )
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
                job,
                "terminal",
                {"status": "failed", "error": str(safe_error)[-1000:]},
                authority={
                    "actor": "bridge",
                    "authority_source": "bridge_internal",
                    "scope": "terminal_transition",
                    "classification": "observed",
                },
            )
        finally:
            async with self._state_lock:
                job.task = None
                job.updated_at = _timestamp()
                self._persist_job(job)
                self._persist_progress(job)
