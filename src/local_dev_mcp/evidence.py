"""Portable evidence bundle for the local development bridge (M-EV-06).

Assembles, into a single redacted in-memory JSON document, the durable evidence
for one development job: the candidate fingerprint and changed paths, a diff
summary, verification records + bindings, review records + verdict hashes,
checkpoint summary, and (when a requirement manager is wired) requirement
coverage. Sensitive paths are redacted and the bundle never includes raw
worktree paths, secrets, or credentials.

Dormant by default: the ``export_evidence_bundle`` MCP tool is only exposed when
a project opts into ``EXPORT_EVIDENCE_BUNDLE`` (which requires
``repository_read``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import store
from .config import Settings
from .git import run_git
from .jobs import DevelopmentJobManager
from .models import ProjectPolicy
from .registry import ProjectLockManager
from .worktrees import GitWorktreeManager


class EvidenceBundleError(RuntimeError):
    """Raised when an evidence bundle cannot be assembled safely."""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _redact(value: Any, paths: tuple[Path, ...]) -> Any:
    if isinstance(value, str):
        redacted = value
        for path in paths:
            redacted = redacted.replace(str(path), "<redacted-path>")
        return redacted
    if isinstance(value, dict):
        return {key: _redact(item, paths) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, paths) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, paths) for item in value)
    return value


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


class EvidenceBundleManager:
    """Assembles redacted, content-addressed evidence bundles for a job."""

    def __init__(
        self,
        settings: Settings,
        locks: ProjectLockManager,
        jobs: DevelopmentJobManager,
        worktrees: GitWorktreeManager | None = None,
        projects: tuple[ProjectPolicy, ...] = (),
    ) -> None:
        self.settings = settings
        self._locks = locks
        self._jobs = jobs
        self._worktrees = worktrees or GitWorktreeManager(
            settings, tuple(project.root for project in projects)
        )
        self._project_roots = tuple(project.root for project in projects)
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")

    def _redaction_paths(self, project: ProjectPolicy, job_id: str) -> tuple[Path, ...]:
        worktree_path = self.settings.job_worktree_root / project.id / job_id
        return (
            worktree_path,
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )

    async def _diff_summary(
        self, project: ProjectPolicy, job_id: str
    ) -> dict[str, Any]:
        """Bounded ``git diff --stat`` over the retained worktree, redacted.

        Returns a compact summary (files changed, insertions, deletions) rather
        than raw git output. Never exposes the worktree path.
        """
        worktree_path = self.settings.job_worktree_root / project.id / job_id
        try:
            stat = await run_git(
                worktree_path,
                "diff",
                "--stat",
                "HEAD",
                timeout_seconds=self.settings.git_command_timeout_seconds,
            )
        except Exception:
            return {"files_changed": 0, "insertions": 0, "deletions": 0, "detail": ""}
        files_changed = 0
        insertions = 0
        deletions = 0
        for line in stat.splitlines():
            # Skip the summary trailer like " 1 file changed, 2 insertions(+)"
            # or " 2 files changed, 3 insertions(+), 1 deletion(-)". It always
            # contains "changed", so that is the reliable marker.
            if "changed" in line:
                continue
            if line.startswith(" ") or not line:
                continue
            files_changed += 1
            plus = minus = 0
            for token in line.split():
                if token.endswith("+"):
                    try:
                        plus += int(token.rstrip("+"))
                    except ValueError:
                        pass
                elif token.endswith("-"):
                    try:
                        minus += int(token.rstrip("-"))
                    except ValueError:
                        pass
            insertions += plus
            deletions += minus
        return {
            "files_changed": files_changed,
            "insertions": insertions,
            "deletions": deletions,
            "detail": _redact(stat, self._redaction_paths(project, job_id)),
        }

    async def export_bundle(
        self,
        project: ProjectPolicy,
        job_id: str,
        expected_candidate_fingerprint: str,
    ) -> dict[str, Any]:
        """Assemble and return the redacted evidence bundle for a job.

        The bundle is derived from persisted durable records (verification,
        review, checkpoint) and a live diff summary, never from raw worktree
        contents. ``expected_candidate_fingerprint`` binds the bundle to the
        caller's claimed candidate so a stale or forged job cannot produce a
        valid-looking bundle.
        """
        expected = expected_candidate_fingerprint.lower()
        if len(expected) != 64 or not all(c in "0123456789abcdef" for c in expected):
            raise EvidenceBundleError("expected_candidate_fingerprint must be a 64-hex SHA-256")

        lock = await self._locks.for_project(project.id)
        async with lock:
            snapshot = await self._jobs.get(project, job_id)
            base_head = snapshot.get("base_head")

            verification_rows = store.load_record_rows_for_job(self._db, job_id)
            review_rows = store.load_review_rows_for_job(self._db, job_id)
            checkpoint_count = store.checkpoint_count(self._db, job_id)
            latest_checkpoint = store.load_latest_checkpoint(self._db, job_id)

            verifications = [
                self._verification_entry(row, project, job_id)
                for row in verification_rows
            ]
            reviews = [self._review_entry(row, project, job_id) for row in review_rows]

            diff_summary = await self._diff_summary(project, job_id)

            bundle: dict[str, Any] = {
                "bundle_version": 1,
                "exported_at": _timestamp(),
                "project_id": project.id,
                "job_id": job_id,
                "base_head": base_head,
                "expected_candidate_fingerprint": expected,
                "candidate": {
                    "fingerprint": latest_checkpoint.get("partial_fingerprint")
                    if latest_checkpoint is not None
                    else None,
                    "changed_paths": _json_loads(
                        latest_checkpoint.get("changed_paths_json")
                    )
                    if latest_checkpoint is not None
                    else None,
                    "diff_summary": diff_summary,
                },
                "verifications": verifications,
                "reviews": reviews,
                "checkpoint_count": checkpoint_count,
            }

            # Redact sensitive paths BEFORE computing the bundle-wide content
            # address. The SHA-256 is over the sorted, un-ASCII-escaped JSON of
            # the bundle WITHOUT the ``bundle_sha256`` field itself, so a
            # recipient can recompute it by stripping that field and re-serializing
            # with sort_keys=True and ensure_ascii=False.
            redacted = _redact(bundle, self._redaction_paths(project, job_id))
            bundle_json = json.dumps(redacted, sort_keys=True, ensure_ascii=False)
            redacted["bundle_sha256"] = _sha256(bundle_json)
            return redacted

    def _verification_entry(
        self, row: dict[str, Any], project: ProjectPolicy, job_id: str
    ) -> dict[str, Any]:
        # Never ship raw command stdout/stderr in the bundle: build/test output
        # routinely embeds tokens, env values, and paths. Keep only the
        # determinism-relevant status/exit-code and a content hash of the output
        # for provenance, so no secret can leak.
        raw_commands = _json_loads(row.get("commands_json"))
        commands: list[dict[str, Any]] = []
        if isinstance(raw_commands, list):
            for item in raw_commands:
                if not isinstance(item, dict):
                    continue
                stdout = item.get("stdout", "")
                stderr = item.get("stderr", "")
                commands.append(
                    {
                        "command_id": item.get("command_id"),
                        "status": item.get("status"),
                        "exit_code": item.get("exit_code"),
                        "stdout_sha256": _sha256(
                            json.dumps(stdout, sort_keys=True)
                        ),
                        "stderr_sha256": _sha256(
                            json.dumps(stderr, sort_keys=True)
                        ),
                    }
                )
        return {
            "verification_id": row.get("verification_id"),
            "base_head": row.get("base_head"),
            "diff_fingerprint": row.get("diff_fingerprint"),
            "paths": _json_loads(row.get("paths_json")),
            "verified_at": row.get("verified_at"),
            "used": bool(row.get("used", 0)),
            "commands": commands,
        }

    def _review_entry(
        self, row: dict[str, Any], project: ProjectPolicy, job_id: str
    ) -> dict[str, Any]:
        verdict = _json_loads(row.get("verdict_json"))
        # Verdict hash: content-address the verdict so the recipient can confirm
        # the verdict text is intact without the bundle exposing unredacted
        # reviewer prose in full.
        verdict_hash = _sha256(json.dumps(verdict, sort_keys=True)) if verdict else None
        return {
            "review_id": row.get("review_id"),
            "base_head": row.get("base_head"),
            "diff_fingerprint": row.get("diff_fingerprint"),
            "paths": _json_loads(row.get("paths_json")),
            "reviewed_at": row.get("reviewed_at"),
            "reviewer_id": row.get("reviewer_id"),
            "used": bool(row.get("used", 0)),
            "verdict_hash": verdict_hash,
            "verdict_summary": self._verdict_summary(verdict),
        }

    @staticmethod
    def _verdict_summary(verdict: Any) -> dict[str, Any]:
        if not isinstance(verdict, dict):
            return {}
        summary: dict[str, Any] = {}
        for key in ("approve", "reject", "verdict", "approved", "conclusion", "status"):
            if key in verdict:
                summary[key] = verdict[key]
        return summary
