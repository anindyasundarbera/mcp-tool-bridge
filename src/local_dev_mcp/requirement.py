r"""Requirement traceability management for the local development bridge.

M-EV-01: Requirement traceability — extracts normative requirement IDs from
Markdown documentation, classifies them as normative vs example/commentary,
builds a requirement→code→test coverage matrix, detects contradictions,
records deferrals with authority, and surfaces ``requirements_covered`` in the
existing job/project snapshots.

The schema changes are additive (``requirements`` + ``requirement_links``
tables plus additive columns via ``_ADDITIVE_COLUMNS``) and
``SCHEMA_VERSION`` remains 1.

Internal-first implementation: no new MCP tool, no new ``ProjectCapability``.
The coverage summary is surfaced via the existing ``get_development_job`` /
``get_project_status`` snapshots.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store
from .config import Settings
from .models import ProjectPolicy
from .reader import RepositoryReader, RepositoryReadError


NORMATIVE_ID_PATTERN = re.compile(r"^[A-Z]+-\d+[A-Z]?-R\d+(?:\s|$)")
FENCED_NORMATIVE_PATTERN = re.compile(r"^```\s*normative\s*$", re.IGNORECASE)
FENCE_END_PATTERN = re.compile(r"^```\s*$")

# Heuristic indicators for example/commentary context vs normative.
_EXAMPLE_INDICATORS = (
    "example", "for example", "e.g.", "such as", "like",
    "suppose", "imagine", "consider", "see", "refer to",
    "note:", "note ", "caution:", "warning:", "tip:", "hint:",
    "remember", "keep in mind", "as shown", "as illustrated",
    "demo", "demonstration", "sample", "placeholder", "todo",
    "fixme", "xxx", "tbd", "tba",
)

# Contradiction detection: one requirement asserts, another negates.
_ASSERTION_WORDS = ("must", "shall", "always", "required", "mandatory")
_NEGATION_PHRASES = ("must not", "shall not", "never", "prohibited", "forbidden")
_STOP_WORDS = frozenset((
    "the", "a", "an", "is", "are", "be", "to", "of", "in", "on", "for",
    "and", "or", "not", "with", "by", "at", "from", "as", "that", "this",
    "it", "its", "if", "then", "else", "when", "where", "which", "who",
    "whom", "whose", "will", "would", "should", "could", "can", "may",
    "might", "must", "shall", "always", "never", "all", "any", "each",
    "every", "some", "no", "none", "true", "false", "null",
))


@dataclass(frozen=True, slots=True)
class Requirement:
    """A requirement extracted from Markdown documentation."""
    requirement_id: str
    project_id: str
    text: str
    source_path: str
    source_line: int
    normative: bool
    kind: str  # normative|example|commentary
    status: str  # implemented|unimplemented|deferred
    deferred_authority: str | None


@dataclass(frozen=True, slots=True)
class RequirementLink:
    """A link between a requirement and some artifact (job, changed path, test)."""
    link_id: int
    requirement_id: str
    job_id: str
    link_type: str  # job|changed_path|test_name
    link_value: str


@dataclass(frozen=True, slots=True)
class Contradiction:
    """A pair of normative requirements whose statements conflict."""
    requirement_id_a: str
    requirement_id_b: str
    reason: str


class RequirementManagerError(RuntimeError):
    """Raised when requirement traceability operations fail."""


def _timestamp() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


def _kind_for(normative: bool) -> str:
    return "normative" if normative else "example"


class RequirementManager:
    """Extracts and manages requirement traceability from Markdown documentation.

    The manager shares the durable-state database connection with the job
    manager (passed in as ``db``) so there is a single write path and no
    second connection competing for WAL locks. When constructed standalone
    (for tests), it opens its own connection to the provided path.
    """

    def __init__(
        self,
        settings: Settings,
        store_module: Any,
        reader: RepositoryReader,
        *,
        db: sqlite3.Connection | None = None,
        db_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self._store = store_module
        self._reader = reader
        if db is not None:
            self._db = db
            self._owns_db = False
        elif db_path is not None:
            self._db = store_module.open_state_db(db_path)
            self._owns_db = True
        else:
            # Fallback: open the standard state DB path. This path is only
            # correct when the caller ensures ``.state`` exists; prefer
            # passing ``db`` from the job manager.
            state_root = settings.job_worktree_root / ".state"
            state_root.mkdir(parents=True, exist_ok=True)
            self._db = store_module.open_state_db(state_root / "bridge.db")
            self._owns_db = True
        # Cache of requirements by project_id to avoid re-scanning.
        self._requirements_cache: dict[str, list[Requirement]] = {}
        # Cache of requirement links keyed by (project_id, job_id).
        self._links_cache: dict[tuple[str, str], list[RequirementLink]] = {}

    def close(self) -> None:
        """Close the DB connection if this manager owns it."""
        if self._owns_db:
            self._db.close()

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def extract_requirements_from_markdown(
        self,
        project: ProjectPolicy,
        content: str,
        source_path: str,
    ) -> list[Requirement]:
        """Extract requirements from Markdown content.

        Returns a list of Requirement objects found in the content.
        Implements the spec-kit model:
        - Normative IDs matching ^[A-Z]+-\\d+[A-Z]?-R\\d+
        - Content in fenced normative blocks (```normative ... ```)
        """
        requirements: list[Requirement] = []
        lines = content.splitlines()
        in_normative_block = False

        for line_num, line in enumerate(lines, start=1):
            stripped = line.strip()

            if FENCED_NORMATIVE_PATTERN.match(stripped):
                in_normative_block = True
                continue
            elif FENCE_END_PATTERN.match(stripped) and in_normative_block:
                in_normative_block = False
                continue

            if NORMATIVE_ID_PATTERN.match(stripped):
                parts = stripped.split(None, 1)
                requirement_id = parts[0]
                text = parts[1] if len(parts) > 1 else ""
                normative = in_normative_block or self._is_likely_normative_context(
                    lines, line_num - 1
                )
                kind = _kind_for(normative)
                requirements.append(Requirement(
                    requirement_id=requirement_id,
                    project_id=project.id,
                    text=text,
                    source_path=source_path,
                    source_line=line_num,
                    normative=normative,
                    kind=kind,
                    status="unimplemented",
                    deferred_authority=None,
                ))
            elif in_normative_block and stripped:
                # Content inside a fenced normative block without an ID is
                # descriptive; skip it rather than fabricating auto-gen IDs
                # (the original implementation's AUTO-Gen IDs polluted the
                # normative set with non-ID entries).
                continue

        return requirements

    def _is_likely_normative_context(self, lines: list[str], line_index: int) -> bool:
        """Heuristic: distinguish normative requirements from examples/commentary."""
        start = max(0, line_index - 3)
        end = min(len(lines), line_index + 4)
        context = "\n".join(lines[start:end]).lower()
        for indicator in _EXAMPLE_INDICATORS:
            if indicator in context:
                return False
        return True

    # ------------------------------------------------------------------
    # Scanning + persisting
    # ------------------------------------------------------------------

    async def parse(
        self,
        project: ProjectPolicy,
    ) -> list[Requirement]:
        """Scan the project's Markdown for normative IDs and persist them.

        Idempotent: requirements are upserted. Existing status/deferred_authority
        are preserved via COALESCE in the upsert SQL. Returns the full list
        of requirements for the project (from the cache or freshly scanned).
        """
        requirements = await self._scan_project(project)
        for req in requirements:
            self._store.upsert_requirement_row(
                self._db,
                {
                    "requirement_id": req.requirement_id,
                    "project_id": req.project_id,
                    "text": req.text,
                    "source_path": req.source_path,
                    "source_line": req.source_line,
                    "normative": 1 if req.normative else 0,
                    "kind": req.kind,
                    "status": req.status,
                    "deferred_authority": req.deferred_authority,
                    "updated_at": _timestamp(),
                },
            )
        self._requirements_cache[project.id] = requirements
        return requirements

    async def _scan_project(
        self,
        project: ProjectPolicy,
    ) -> list[Requirement]:
        """Scan all Markdown files in a project for requirements (no persist)."""
        if project.id in self._requirements_cache:
            return self._requirements_cache[project.id]

        requirements: list[Requirement] = []
        try:
            markdown_list = await self._reader.list_markdown(project, path=".")
            for file_info in markdown_list.get("files", []):
                file_path = file_info["path"]
                try:
                    markdown_content = await self._reader.read_markdown(
                        project, file_path
                    )
                    content = markdown_content["content"]
                    file_requirements = self.extract_requirements_from_markdown(
                        project, content, file_path
                    )
                    requirements.extend(file_requirements)
                except RepositoryReadError:
                    continue
                except Exception:
                    continue
        except Exception:
            pass

        self._requirements_cache[project.id] = requirements
        return requirements

    async def scan(
        self,
        project: ProjectPolicy,
    ) -> list[Requirement]:
        """Alias for ``parse`` — scan + persist (M-EV-01 naming convention)."""
        return await self.parse(project)

    def get_requirements_for_project(
        self,
        project_id: str,
    ) -> list[Requirement]:
        """Get all persisted requirements for a project from the DB."""
        rows = self._store.load_requirements_for_project(self._db, project_id)
        return [self._requirement_from_row(row) for row in rows]

    def _requirement_from_row(self, row: dict[str, Any]) -> Requirement:
        return Requirement(
            requirement_id=row["requirement_id"],
            project_id=row["project_id"],
            text=row.get("text", ""),
            source_path=row.get("source_path", ""),
            source_line=int(row.get("source_line", 0)),
            normative=bool(row.get("normative", 0)),
            kind=row.get("kind") or _kind_for(bool(row.get("normative", 0))),
            status=row.get("status") or "unimplemented",
            deferred_authority=row.get("deferred_authority"),
        )

    # ------------------------------------------------------------------
    # Linking
    # ------------------------------------------------------------------

    def link_requirement_to_job(
        self,
        requirement_id: str,
        job_id: str,
    ) -> None:
        """Link a requirement to a job (idempotent)."""
        existing = self._db.execute(
            "SELECT 1 FROM requirement_links WHERE requirement_id = ? "
            "AND job_id = ? AND link_type = 'job'",
            (requirement_id, job_id),
        ).fetchone()
        if existing is not None:
            return
        self._store.upsert_requirement_link_row(
            self._db,
            {
                "requirement_id": requirement_id,
                "job_id": job_id,
                "link_type": "job",
                "link_value": job_id,
                "artifact_kind": "spec",
                "artifact_path": None,
                "evidence_json": None,
                "linked_at": _timestamp(),
            },
        )

    def link_requirement_to_changed_path(
        self,
        requirement_id: str,
        job_id: str,
        changed_path: str,
    ) -> None:
        """Link a requirement to a changed path from a job (idempotent)."""
        existing = self._db.execute(
            "SELECT 1 FROM requirement_links WHERE requirement_id = ? "
            "AND job_id = ? AND link_type = 'changed_path' AND link_value = ?",
            (requirement_id, job_id, changed_path),
        ).fetchone()
        if existing is not None:
            return
        self._store.upsert_requirement_link_row(
            self._db,
            {
                "requirement_id": requirement_id,
                "job_id": job_id,
                "link_type": "changed_path",
                "link_value": changed_path,
                "artifact_kind": "code",
                "artifact_path": changed_path,
                "evidence_json": None,
                "linked_at": _timestamp(),
            },
        )

    def link_requirement_to_test_name(
        self,
        requirement_id: str,
        job_id: str,
        test_name: str,
    ) -> None:
        """Link a requirement to a test name from a job (idempotent)."""
        existing = self._db.execute(
            "SELECT 1 FROM requirement_links WHERE requirement_id = ? "
            "AND job_id = ? AND link_type = 'test_name' AND link_value = ?",
            (requirement_id, job_id, test_name),
        ).fetchone()
        if existing is not None:
            return
        self._store.upsert_requirement_link_row(
            self._db,
            {
                "requirement_id": requirement_id,
                "job_id": job_id,
                "link_type": "test_name",
                "link_value": test_name,
                "artifact_kind": "test",
                "artifact_path": test_name,
                "evidence_json": None,
                "linked_at": _timestamp(),
            },
        )

    def get_links_for_job(
        self,
        project_id: str,
        job_id: str,
    ) -> list[RequirementLink]:
        """Get all requirement links for a specific job."""
        cache_key = (project_id, job_id)
        if cache_key in self._links_cache:
            return self._links_cache[cache_key]
        rows = self._store.load_requirement_links_for_job(self._db, job_id)
        links = [self._link_from_row(row) for row in rows]
        self._links_cache[cache_key] = links
        return links

    def _link_from_row(self, row: dict[str, Any]) -> RequirementLink:
        return RequirementLink(
            link_id=int(row["link_id"]),
            requirement_id=row["requirement_id"],
            job_id=row["job_id"],
            link_type=row["link_type"],
            link_value=row["link_value"],
        )

    # ------------------------------------------------------------------
    # Deferrals
    # ------------------------------------------------------------------

    def record_deferral(
        self,
        requirement_id: str,
        authority: str,
    ) -> None:
        """Record an intentional deferral with its authority (M-EV-01).

        Marks the requirement's status as ``deferred`` and records the
        authority string (e.g. ``"chatgpt:2026-08-26"``). The authority
        travels with the requirement so an orchestrator can audit who
        authorized the deferral.
        """
        self._store.update_requirement_status(
            self._db,
            requirement_id,
            status="deferred",
            deferred_authority=authority,
        )

    # ------------------------------------------------------------------
    # Contradiction detection
    # ------------------------------------------------------------------

    @staticmethod
    def detect_contradictions(
        requirements: list[Requirement],
    ) -> list[Contradiction]:
        """Detect contradictory pairs of normative requirements.

        Two normative requirements are contradictory when one asserts a
        positive obligation (``must``/``shall``/``always``/``required``)
        and another negates it (``must not``/``shall not``/``never``/
        ``prohibited``) and they share at least one significant content
        keyword (length > 3, not a stop word).
        """
        normative = [req for req in requirements if req.normative and req.text]
        contradictions: list[Contradiction] = []
        seen: set[frozenset[str]] = set()
        for i, req_a in enumerate(normative):
            text_a = req_a.text.lower()
            asserts_a = any(w in text_a for w in _ASSERTION_WORDS)
            negates_a = any(p in text_a for p in _NEGATION_PHRASES)
            if not asserts_a and not negates_a:
                continue
            keywords_a = {
                word for word in text_a.split()
                if len(word) > 3 and word not in _STOP_WORDS
            }
            for req_b in normative[i + 1:]:
                text_b = req_b.text.lower()
                asserts_b = any(w in text_b for w in _ASSERTION_WORDS)
                negates_b = any(p in text_b for p in _NEGATION_PHRASES)
                # One asserts, the other negates.
                if not ((asserts_a and negates_b) or (negates_a and asserts_b)):
                    continue
                keywords_b = {
                    word for word in text_b.split()
                    if len(word) > 3 and word not in _STOP_WORDS
                }
                shared = keywords_a & keywords_b
                if not shared:
                    continue
                pair = frozenset((req_a.requirement_id, req_b.requirement_id))
                if pair in seen:
                    continue
                seen.add(pair)
                contradictions.append(Contradiction(
                    requirement_id_a=req_a.requirement_id,
                    requirement_id_b=req_b.requirement_id,
                    reason=(
                        f"conflicting obligation over: {', '.join(sorted(shared)[:5])}"
                    ),
                ))
        return contradictions

    # ------------------------------------------------------------------
    # Matrix
    # ------------------------------------------------------------------

    async def matrix(
        self,
        project: ProjectPolicy,
    ) -> dict[str, Any]:
        """Produce a requirement→code→test coverage matrix for a project.

        Returns a dict with:
        - total: total number of requirements
        - normative: number of normative requirements
        - covered: normative requirements with at least one link
        - uncovered: normative requirements with no links (and not deferred)
        - deferred: normative requirements with status ``deferred``
        - contradictory: list of Contradiction pairs
        - details: per-requirement detail with coverage status
        """
        requirements = await self._scan_project(project)
        # Reload from DB to pick up any persisted status/deferral updates.
        persisted = self.get_requirements_for_project(project.id)
        persisted_by_id = {req.requirement_id: req for req in persisted}
        # Merge: persisted status/deferred_authority take precedence.
        merged: list[Requirement] = []
        for req in requirements:
            pr = persisted_by_id.get(req.requirement_id)
            if pr is not None and (pr.status != "unimplemented" or pr.deferred_authority):
                merged.append(Requirement(
                    requirement_id=req.requirement_id,
                    project_id=req.project_id,
                    text=req.text,
                    source_path=req.source_path,
                    source_line=req.source_line,
                    normative=req.normative,
                    kind=req.kind,
                    status=pr.status,
                    deferred_authority=pr.deferred_authority,
                ))
            else:
                merged.append(req)

        # Gather all links across all jobs for this project.
        all_linked_ids: set[str] = set()
        for req in merged:
            rows = self._store.load_requirement_links_for_requirement(
                self._db, req.requirement_id
            )
            if rows:
                all_linked_ids.add(req.requirement_id)

        normative_reqs = [req for req in merged if req.normative]
        covered = [
            req for req in normative_reqs
            if req.requirement_id in all_linked_ids and req.status != "deferred"
        ]
        deferred = [req for req in normative_reqs if req.status == "deferred"]
        uncovered = [
            req for req in normative_reqs
            if req.requirement_id not in all_linked_ids and req.status != "deferred"
        ]
        contradictions = self.detect_contradictions(merged)

        return {
            "total": len(merged),
            "normative": len(normative_reqs),
            "covered": len(covered),
            "uncovered": len(uncovered),
            "deferred": len(deferred),
            "contradictory": len(contradictions),
            "contradictions": [
                {
                    "requirement_id_a": c.requirement_id_a,
                    "requirement_id_b": c.requirement_id_b,
                    "reason": c.reason,
                }
                for c in contradictions
            ],
            "details": [
                {
                    "requirement_id": req.requirement_id,
                    "text": req.text,
                    "source_path": req.source_path,
                    "source_line": req.source_line,
                    "normative": req.normative,
                    "kind": req.kind,
                    "status": req.status,
                    "deferred_authority": req.deferred_authority,
                    "covered": req.requirement_id in all_linked_ids,
                }
                for req in merged
            ],
        }

    async def get_coverage_matrix_for_job(
        self,
        project: ProjectPolicy,
        job_id: str,
    ) -> dict[str, Any]:
        """Generate a coverage matrix for a job showing which requirements are covered.

        Returns a dictionary with covered/uncovered/deferred/contradictory
        counts plus per-requirement details and links-by-type.
        """
        requirements = await self._scan_project(project)
        persisted = self.get_requirements_for_project(project.id)
        persisted_by_id = {req.requirement_id: req for req in persisted}
        merged: list[Requirement] = []
        for req in requirements:
            pr = persisted_by_id.get(req.requirement_id)
            if pr is not None and (pr.status != "unimplemented" or pr.deferred_authority):
                merged.append(Requirement(
                    requirement_id=req.requirement_id,
                    project_id=req.project_id,
                    text=req.text,
                    source_path=req.source_path,
                    source_line=req.source_line,
                    normative=req.normative,
                    kind=req.kind,
                    status=pr.status,
                    deferred_authority=pr.deferred_authority,
                ))
            else:
                merged.append(req)

        job_links = self.get_links_for_job(project.id, job_id)
        linked_requirement_ids = {link.requirement_id for link in job_links}

        normative_reqs = [req for req in merged if req.normative]
        covered_normative = [
            req for req in normative_reqs
            if req.requirement_id in linked_requirement_ids and req.status != "deferred"
        ]
        deferred_normative = [req for req in normative_reqs if req.status == "deferred"]
        uncovered_normative = [
            req for req in normative_reqs
            if req.requirement_id not in linked_requirement_ids and req.status != "deferred"
        ]
        contradictions = self.detect_contradictions(merged)

        links_by_type: dict[str, list[dict[str, Any]]] = {}
        for link in job_links:
            links_by_type.setdefault(link.link_type, []).append({
                "requirement_id": link.requirement_id,
                "link_value": link.link_value,
            })

        return {
            "total_requirements": len(merged),
            "normative_requirements": len(normative_reqs),
            "covered": len(covered_normative),
            "uncovered": len(uncovered_normative),
            "deferred": len(deferred_normative),
            "contradictory": len(contradictions),
            "contradictions": [
                {
                    "requirement_id_a": c.requirement_id_a,
                    "requirement_id_b": c.requirement_id_b,
                    "reason": c.reason,
                }
                for c in contradictions
            ],
            "requirements_details": [
                {
                    "requirement_id": req.requirement_id,
                    "text": req.text,
                    "source_path": req.source_path,
                    "source_line": req.source_line,
                    "normative": req.normative,
                    "kind": req.kind,
                    "status": req.status,
                    "deferred_authority": req.deferred_authority,
                    "covered": req.requirement_id in linked_requirement_ids,
                }
                for req in merged
            ],
            "links_by_type": links_by_type,
        }

    # ------------------------------------------------------------------
    # Snapshot summary
    # ------------------------------------------------------------------

    async def requirements_covered_summary(
        self,
        project: ProjectPolicy,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a bounded ``requirements_covered`` summary for snapshots.

        When ``job_id`` is provided, coverage is scoped to that job's links.
        Otherwise the project-wide matrix is used. Returns only safe,
        redacted fields: counts (covered/uncovered/deferred/contradictory)
        and the list of uncovered normative requirement IDs (bounded).
        """
        if job_id is not None:
            matrix = await self.get_coverage_matrix_for_job(project, job_id)
        else:
            matrix = await self.matrix(project)
        return self._summary_from_matrix(matrix)

    def requirements_covered_summary_sync(
        self,
        project_id: str,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Synchronous variant of ``requirements_covered_summary`` for use in
        sync snapshot code (M-EV-01).

        Uses only persisted DB state (no Markdown scan). When ``job_id`` is
        provided, coverage is scoped to that job's links. Returns counts and
        the list of uncovered normative IDs.
        """
        requirements = self.get_requirements_for_project(project_id)
        if job_id is not None:
            links = self._store.load_requirement_links_for_job(self._db, job_id)
            linked_ids = {row["requirement_id"] for row in links}
        else:
            linked_ids: set[str] = set()
            for req in requirements:
                rows = self._store.load_requirement_links_for_requirement(
                    self._db, req.requirement_id
                )
                if rows:
                    linked_ids.add(req.requirement_id)
        normative_reqs = [r for r in requirements if r.normative]
        covered = [r for r in normative_reqs if r.requirement_id in linked_ids and r.status != "deferred"]
        deferred = [r for r in normative_reqs if r.status == "deferred"]
        uncovered = [r for r in normative_reqs if r.requirement_id not in linked_ids and r.status != "deferred"]
        contradictions = self.detect_contradictions(requirements)
        return {
            "total": len(requirements),
            "normative": len(normative_reqs),
            "covered": len(covered),
            "uncovered": len(uncovered),
            "deferred": len(deferred),
            "contradictory": len(contradictions),
            "uncovered_ids": [r.requirement_id for r in uncovered][:50],
        }

    @staticmethod
    def _summary_from_matrix(matrix: dict[str, Any]) -> dict[str, Any]:
        uncovered_ids = [
            d["requirement_id"]
            for d in matrix.get("details", [])
            if d.get("normative") and not d.get("covered") and d.get("status") != "deferred"
        ]
        return {
            "total": matrix.get("total", 0),
            "normative": matrix.get("normative", 0),
            "covered": matrix.get("covered", 0),
            "uncovered": matrix.get("uncovered", 0),
            "deferred": matrix.get("deferred", 0),
            "contradictory": matrix.get("contradictory", 0),
            "uncovered_ids": uncovered_ids[:50],
        }

    # ------------------------------------------------------------------
    # Link persistence from a completed candidate
    # ------------------------------------------------------------------

    def persist_links_for_candidate(
        self,
        project: ProjectPolicy,
        job_id: str,
        changed_paths: tuple[str, ...],
        test_names: tuple[str, ...] | None = None,
    ) -> None:
        """Persist requirement_links for a candidate's changed paths / test names.

        Links each requirement to the job, to each changed path (artifact_kind=
        ``code``), and to each test name (artifact_kind= ``test``). Idempotent:
        existing links are not re-inserted.
        """
        requirements = self.get_requirements_for_project(project.id)
        for req in requirements:
            if not req.normative:
                continue
            self.link_requirement_to_job(req.requirement_id, job_id)
            for path in changed_paths:
                self.link_requirement_to_changed_path(
                    req.requirement_id, job_id, path
                )
            if test_names:
                for test_name in test_names:
                    self.link_requirement_to_test_name(
                        req.requirement_id, job_id, test_name
                    )

    def clear_cache(self) -> None:
        """Clear all cached data. Useful when Markdown files may have changed."""
        self._requirements_cache.clear()
        self._links_cache.clear()