r"""Requirement traceability management for the local development bridge.

This module implements M-EV-01: Requirement traceability — the highest-value single unblock.
It borrows the spec-kit model: intent → spec (normative requirement IDs) → implementation → tasks.

The RequirementManager extracts normative IDs from Markdown files using:
1. The regex pattern ^[A-Z]+-\d+[A-Z]?-R\d+
2. Fenced normative blocks (```normative ... ```)

It classifies content as normative vs example/commentary and maintains:
- requirements table: requirement_id, project_id, text, source_path, source_line, normative
- requirement_links table: links requirements to jobs, changed paths, test names

The schema changes are additive (requirements + requirement_links tables) and
SCHEMA_VERSION remains 1.

Internal-first implementation: surfaces requirements_covered in existing snapshots,
prepares for a coverage-report tool as NEW SURFACE (to be authorized later).
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


NORMATIVE_ID_PATTERN = re.compile(r"^[A-Z]+-\d+[A-Z]?-R\d+$")
FENCED_NORMATIVE_PATTERN = re.compile(r"^```\s*normative\s*$", re.IGNORECASE)
FENCE_END_PATTERN = re.compile(r"^```\s*$")


@dataclass(frozen=True, slots=True)
class Requirement:
    """A requirement extracted from Markdown documentation."""
    requirement_id: str
    project_id: str
    text: str
    source_path: str
    source_line: int
    normative: bool


@dataclass(frozen=True, slots=True)
class RequirementLink:
    """A link between a requirement and some artifact (job, changed path, test name)."""
    link_id: int
    requirement_id: str
    job_id: str
    link_type: str  # e.g., "job", "changed_path", "test_name"
    link_value: str  # the actual job_id, path, or test name


class RequirementManagerError(RuntimeError):
    """Raised when requirement traceability operations fail."""


class RequirementManager:
    """Extracts and manages requirement traceability from Markdown documentation."""

    def __init__(
        self,
        settings: Settings,
        store: Any,  # store module
        reader: RepositoryReader,
    ) -> None:
        self.settings = settings
        self._store = store
        self._reader = reader
        self._db = store.open_state_db(settings.job_worktree_root / "bridge.db")
        # Cache of requirements by project_id to avoid re-scanning
        self._requirements_cache: dict[str, list[Requirement]] = {}
        # Cache of requirement links to avoid re-querying
        self._links_cache: dict[tuple[str, str], list[RequirementLink]] = {}

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
        current_normative_block_start = 0

        for line_num, line in enumerate(lines, start=1):
            stripped = line.strip()

            # Check for fenced normative blocks
            if FENCED_NORMATIVE_PATTERN.match(stripped):
                in_normative_block = True
                current_normative_block_start = line_num
                continue
            elif FENCE_END_PATTERN.match(stripped) and in_normative_block:
                in_normative_block = False
                continue

            # Check for normative ID pattern at start of line
            if NORMATIVE_ID_PATTERN.match(stripped):
                # Extract the requirement ID and the rest as text
                parts = stripped.split(None, 1)
                requirement_id = parts[0]
                text = parts[1] if len(parts) > 1 else ""

                # Determine if this is normative based on context
                # If we're in a fenced normative block, it's definitely normative
                # Otherwise, we classify based on surrounding context
                normative = in_normative_block or self._is_likely_normative_context(
                    lines, line_num - 1
                )

                requirements.append(Requirement(
                    requirement_id=requirement_id,
                    project_id=project.id,
                    text=text,
                    source_path=source_path,
                    source_line=line_num,
                    normative=normative,
                ))
            elif in_normative_block and stripped:
                # Content inside a fenced normative block is considered normative
                # even if it doesn't match the ID pattern (for descriptive requirements)
                requirements.append(Requirement(
                    requirement_id=f"AUTO-GEN-{len(requirements)+1:04d}",
                    project_id=project.id,
                    text=stripped,
                    source_path=source_path,
                    source_line=line_num,
                    normative=True,
                ))

        return requirements

    def _is_likely_normative_context(self, lines: list[str], line_index: int) -> bool:
        """Heuristic to determine if a requirement ID line is likely normative.

        Looks at surrounding context to distinguish between:
        - Actual requirements (normative)
        - Examples, commentary, or explanatory text (not normative)
        """
        # Look at a few lines before and after for context clues
        start = max(0, line_index - 3)
        end = min(len(lines), line_index + 4)
        context_lines = lines[start:end]

        # Join context for analysis
        context = "\n".join(context_lines).lower()

        # Indicators of example/commentary context
        example_indicators = [
            "example", "for example", "e.g.", "such as", "like",
            "suppose", "imagine", "consider", "see", "refer to",
            "note:", "note ", "caution:", "warning:", "tip:", "hint:",
            "remember", "keep in mind", "as shown", "as illustrated",
            "demo", "demonstration", "sample", "placeholder", "todo",
            "fixme", "xxx", "tbd", "tba"
        ]

        # If we see strong example indicators, it's likely not normative
        for indicator in example_indicators:
            if indicator in context:
                return False

        # Default to treating requirement IDs as normative unless we see
        # clear evidence they're in example/commentary context
        return True

    async def scan_project_for_requirements(
        self,
        project: ProjectPolicy,
    ) -> list[Requirement]:
        """Scan all Markdown files in a project for requirements.

        Uses the RepositoryReader to find and read Markdown files,
        then extracts requirements from their content.
        """
        # Check cache first
        if project.id in self._requirements_cache:
            return self._requirements_cache[project.id]

        requirements: list[Requirement] = []

        # List all Markdown files in the project
        try:
            markdown_list = await self._reader.list_markdown(project, path=".")
            for file_info in markdown_list.get("files", []):
                file_path = file_info["path"]
                try:
                    # Read the full file content
                    markdown_content = await self._reader.read_markdown(
                        project, file_path
                    )
                    content = markdown_content["content"]

                    # Extract requirements from this file
                    file_requirements = self.extract_requirements_from_markdown(
                        project,
                        content,
                        file_path,
                    )
                    requirements.extend(file_requirements)
                except RepositoryReadError:
                    # Skip files we can't read due to policy restrictions
                    continue
                except Exception:
                    # Skip files with other read errors
                    continue
        except Exception:
            # If we can't list files, return empty list
            pass

        # Cache the results
        self._requirements_cache[project.id] = requirements
        return requirements

    async def get_requirements_for_project(
        self,
        project: ProjectPolicy,
    ) -> list[Requirement]:
        """Get all requirements for a project, scanning if necessary."""
        return await self.scan_project_for_requirements(project)

    def get_requirement_by_id(
        self,
        project_id: str,
        requirement_id: str,
    ) -> Requirement | None:
        """Get a specific requirement by ID from the cache."""
        if project_id not in self._requirements_cache:
            return None

        for req in self._requirements_cache[project_id]:
            if req.requirement_id == requirement_id:
                return req
        return None

    def link_requirement_to_job(
        self,
        requirement_id: str,
        job_id: str,
    ) -> None:
        """Link a requirement to a job in the requirement_links table."""
        # First, check if this link already exists to avoid duplicates
        existing = self._db.execute(
            """
            SELECT 1 FROM requirement_links
            WHERE requirement_id = ? AND job_id = ? AND link_type = 'job'
            """,
            (requirement_id, job_id),
        ).fetchone()

        if existing is not None:
            return  # Link already exists

        # Insert the new link
        with self._db:
            self._db.execute(
                """
                INSERT INTO requirement_links (requirement_id, job_id, link_type, link_value)
                VALUES (?, ?, 'job', ?)
                """,
                (requirement_id, job_id, job_id),
            )

    def link_requirement_to_changed_path(
        self,
        requirement_id: str,
        job_id: str,
        changed_path: str,
    ) -> None:
        """Link a requirement to a changed path from a job."""
        # Check if this link already exists
        existing = self._db.execute(
            """
            SELECT 1 FROM requirement_links
            WHERE requirement_id = ? AND job_id = ? AND link_type = 'changed_path' AND link_value = ?
            """,
            (requirement_id, job_id, changed_path),
        ).fetchone()

        if existing is not None:
            return  # Link already exists

        # Insert the new link
        with self._db:
            self._db.execute(
                """
                INSERT INTO requirement_links (requirement_id, job_id, link_type, link_value)
                VALUES (?, ?, 'changed_path', ?)
                """,
                (requirement_id, job_id, changed_path),
            )

    def link_requirement_to_test_name(
        self,
        requirement_id: str,
        job_id: str,
        test_name: str,
    ) -> None:
        """Link a requirement to a test name from a job."""
        # Check if this link already exists
        existing = self._db.execute(
            """
            SELECT 1 FROM requirement_links
            WHERE requirement_id = ? AND job_id = ? AND link_type = 'test_name' AND link_value = ?
            """,
            (requirement_id, job_id, test_name),
        ).fetchone()

        if existing is not None:
            return  # Link already exists

        # Insert the new link
        with self._db:
            self._db.execute(
                """
                INSERT INTO requirement_links (requirement_id, job_id, link_type, link_value)
                VALUES (?, ?, 'test_name', ?)
                """,
                (requirement_id, job_id, test_name),
            )

    def get_links_for_requirement(
        self,
        project_id: str,
        requirement_id: str,
    ) -> list[RequirementLink]:
        """Get all links for a specific requirement."""
        cache_key = (project_id, requirement_id)
        if cache_key in self._links_cache:
            return self._links_cache[cache_key]

        rows = self._db.execute(
            """
            SELECT link_id, requirement_id, job_id, link_type, link_value
            FROM requirement_links
            WHERE requirement_id = ?
            """,
            (requirement_id,),
        ).fetchall()

        links = [
            RequirementLink(
                link_id=row["link_id"],
                requirement_id=row["requirement_id"],
                job_id=row["job_id"],
                link_type=row["link_type"],
                link_value=row["link_value"],
            )
            for row in rows
        ]

        self._links_cache[cache_key] = links
        return links

    def get_links_for_job(
        self,
        project_id: str,
        job_id: str,
    ) -> list[RequirementLink]:
        """Get all requirement links for a specific job."""
        cache_key = (project_id, job_id)
        if cache_key in self._links_cache:
            return self._links_cache[cache_key]

        rows = self._db.execute(
            """
            SELECT link_id, requirement_id, job_id, link_type, link_value
            FROM requirement_links
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchall()

        links = [
            RequirementLink(
                link_id=row["link_id"],
                requirement_id=row["requirement_id"],
                job_id=row["job_id"],
                link_type=row["link_type"],
                link_value=row["link_value"],
            )
            for row in rows
        ]

        self._links_cache[cache_key] = links
        return links

    async def get_coverage_matrix_for_job(
        self,
        project: ProjectPolicy,
        job_id: str,
    ) -> dict[str, Any]:
        """Generate a coverage matrix for a job showing which requirements are covered.

        Returns a dictionary with:
        - total_requirements: total number of requirements found in the project
        - normative_requirements: number of normative requirements
        - covered_requirements: number of requirements linked to this job
        - covered_normative_requirements: number of normative requirements linked to this job
        - requirements_details: list of requirement details with coverage status
        - links_by_type: breakdown of links by type (job, changed_path, test_name)
        """
        # Get all requirements for the project
        requirements = await self.get_requirements_for_project(project)

        # Get all links for this job
        job_links = self.get_links_for_job(project.id, job_id)

        # Build a set of requirement IDs that are linked to this job
        linked_requirement_ids = {link.requirement_id for link in job_links}

        # Categorize requirements
        total_requirements = len(requirements)
        normative_requirements = sum(1 for req in requirements if req.normative)
        covered_requirements = len(linked_requirement_ids)
        covered_normative_requirements = sum(
            1 for req in requirements
            if req.normative and req.requirement_id in linked_requirement_ids
        )

        # Build detailed requirement information
        requirements_details = []
        for req in requirements:
            is_covered = req.requirement_id in linked_requirement_ids
            requirements_details.append({
                "requirement_id": req.requirement_id,
                "text": req.text,
                "source_path": req.source_path,
                "source_line": req.source_line,
                "normative": req.normative,
                "covered": is_covered,
            })

        # Build links breakdown by type
        links_by_type = {}
        for link in job_links:
            link_type = link.link_type
            if link_type not in links_by_type:
                links_by_type[link_type] = []
            links_by_type[link_type].append({
                "requirement_id": link.requirement_id,
                "link_value": link.link_value,
            })

        return {
            "total_requirements": total_requirements,
            "normative_requirements": normative_requirements,
            "covered_requirements": covered_requirements,
            "covered_normative_requirements": covered_normative_requirements,
            "requirements_details": requirements_details,
            "links_by_type": links_by_type,
            "coverage_percentage": (
                covered_requirements / total_requirements * 100
                if total_requirements > 0 else 0
            ),
            "normative_coverage_percentage": (
                covered_normative_requirements / normative_requirements * 100
                if normative_requirements > 0 else 0
            ),
        }

    def clear_cache(self) -> None:
        """Clear all cached data. Useful when Markdown files may have changed."""
        self._requirements_cache.clear()
        self._links_cache.clear()