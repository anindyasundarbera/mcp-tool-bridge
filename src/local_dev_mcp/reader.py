from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .config import Settings
from .git import run_git
from .models import ProjectPolicy


class RepositoryReadError(ValueError):
    """Raised when a repository read request violates its project policy."""


_ATX_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SETEXT_HEADING = re.compile(r"^[ \t]*(=+|-+)[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


def _relative_input(value: str, *, allow_root: bool) -> PurePosixPath:
    normalized = value.replace("\\", "/").strip()
    if allow_root and normalized in {"", "."}:
        return PurePosixPath(".")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or path == PurePosixPath(".")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RepositoryReadError(
            "path must be a normalized relative path without '.', '..', or empty segments"
        )
    if any(part.startswith(".") for part in path.parts):
        raise RepositoryReadError("hidden paths are not readable")
    return path


def _is_within(path: PurePosixPath, parent: PurePosixPath) -> bool:
    return path == parent or parent in path.parents


def _protected(project: ProjectPolicy, relative: PurePosixPath) -> bool:
    return any(
        _is_within(relative, PurePosixPath(configured))
        for configured in project.protected_paths
    )


def resolve_project_path(
    project: ProjectPolicy,
    requested: str,
    *,
    allow_root: bool = False,
    require_markdown: bool = False,
    require_file: bool = False,
    require_directory: bool = False,
) -> tuple[Path, PurePosixPath]:
    relative = _relative_input(requested, allow_root=allow_root)
    if _protected(project, relative):
        raise RepositoryReadError("path is protected by project policy")
    candidate = project.root if relative == PurePosixPath(".") else project.root / relative
    cursor = project.root
    for part in relative.parts:
        if part == ".":
            continue
        cursor /= part
        if cursor.is_symlink():
            raise RepositoryReadError("symbolic links are not readable")
    try:
        resolved = candidate.resolve(strict=True)
        resolved_relative = PurePosixPath(resolved.relative_to(project.root).as_posix())
    except FileNotFoundError as exc:
        raise RepositoryReadError(f"path does not exist: {relative.as_posix()}") from exc
    except ValueError as exc:
        raise RepositoryReadError("path resolves outside the project root") from exc
    if resolved_relative != PurePosixPath(".") and _protected(project, resolved_relative):
        raise RepositoryReadError("resolved path is protected by project policy")
    if any(part.startswith(".") for part in resolved_relative.parts):
        raise RepositoryReadError("resolved path is hidden")
    if require_markdown and resolved.suffix.lower() != ".md":
        raise RepositoryReadError("only Markdown (.md) files are readable")
    if require_file and not resolved.is_file():
        raise RepositoryReadError("path is not a file")
    if require_directory and not resolved.is_dir():
        raise RepositoryReadError("path is not a directory")
    return resolved, resolved_relative


def _read_markdown_bytes(path: Path, limit: int) -> bytes:
    size = path.stat().st_size
    if size > limit:
        raise RepositoryReadError(
            f"Markdown file exceeds the configured {limit}-byte limit"
        )
    data = path.read_bytes()
    if len(data) > limit:
        raise RepositoryReadError(
            f"Markdown file exceeds the configured {limit}-byte limit"
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepositoryReadError("Markdown file is not valid UTF-8") from exc
    return data


def _line_count(text: str) -> int:
    return len(text.splitlines())


async def _git_file_status(project: ProjectPolicy, relative: PurePosixPath) -> str:
    result = await run_git(
        project.root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        relative.as_posix(),
    )
    return result


async def markdown_metadata(
    project: ProjectPolicy,
    path: Path,
    relative: PurePosixPath,
    *,
    data: bytes | None = None,
) -> dict[str, Any]:
    content = data if data is not None else _read_markdown_bytes(path, path.stat().st_size + 1)
    stat = path.stat()
    text = content.decode("utf-8")
    return {
        "path": relative.as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "line_count": _line_count(text),
        "modified_at": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
        "git_status": await _git_file_status(project, relative),
    }


class RepositoryReader:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def list_markdown(
        self,
        project: ProjectPolicy,
        path: str = ".",
        max_depth: int = 3,
        max_results: int = 100,
    ) -> dict[str, Any]:
        if not 0 <= max_depth <= 20:
            raise RepositoryReadError("max_depth must be between 0 and 20")
        if not 1 <= max_results <= self.settings.max_markdown_list_results:
            raise RepositoryReadError(
                "max_results exceeds the configured Markdown listing limit"
            )
        directory, directory_relative = resolve_project_path(
            project, path, allow_root=True, require_directory=True
        )
        files: list[dict[str, Any]] = []
        truncated = False
        base_depth = len(directory.parts)
        for current, directories, names in os.walk(directory, followlinks=False):
            current_path = Path(current)
            depth = len(current_path.parts) - base_depth
            directories[:] = sorted(
                name
                for name in directories
                if not name.startswith(".")
                and not (current_path / name).is_symlink()
            )
            if depth >= max_depth:
                directories[:] = []
            for name in sorted(names):
                candidate = current_path / name
                if name.startswith(".") or candidate.is_symlink() or candidate.suffix.lower() != ".md":
                    continue
                relative = PurePosixPath(candidate.relative_to(project.root).as_posix())
                if _protected(project, relative):
                    continue
                if len(files) >= max_results:
                    truncated = True
                    break
                stat = candidate.stat()
                files.append(
                    {
                        "path": relative.as_posix(),
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                )
            if truncated:
                break
        return {
            "project_id": project.id,
            "path": directory_relative.as_posix(),
            "max_depth": max_depth,
            "files": files,
            "truncated": truncated,
        }

    async def read_markdown(self, project: ProjectPolicy, path: str) -> dict[str, Any]:
        resolved, relative = resolve_project_path(
            project, path, require_markdown=True, require_file=True
        )
        data = _read_markdown_bytes(resolved, self.settings.max_markdown_file_bytes)
        return {
            "project_id": project.id,
            **await markdown_metadata(project, resolved, relative, data=data),
            "content": data.decode("utf-8"),
        }

    async def read_range(
        self,
        project: ProjectPolicy,
        path: str,
        start_line: int,
        end_line: int,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        if start_line < 1 or end_line < start_line:
            raise RepositoryReadError("line range must satisfy 1 <= start_line <= end_line")
        if end_line - start_line + 1 > self.settings.max_markdown_range_lines:
            raise RepositoryReadError("requested line range exceeds the configured limit")
        resolved, relative = resolve_project_path(
            project, path, require_markdown=True, require_file=True
        )
        data = _read_markdown_bytes(resolved, self.settings.max_markdown_file_bytes)
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and expected_sha256.lower() != digest:
            raise RepositoryReadError("expected_sha256 does not match the current file")
        text = data.decode("utf-8")
        lines = text.splitlines(keepends=True)
        if start_line > len(lines):
            raise RepositoryReadError("start_line is beyond the end of the file")
        actual_end = min(end_line, len(lines))
        return {
            "project_id": project.id,
            "path": relative.as_posix(),
            "sha256": digest,
            "total_lines": len(lines),
            "start_line": start_line,
            "end_line": actual_end,
            "content": "".join(lines[start_line - 1 : actual_end]),
        }

    async def outline(self, project: ProjectPolicy, path: str) -> dict[str, Any]:
        resolved, relative = resolve_project_path(
            project, path, require_markdown=True, require_file=True
        )
        data = _read_markdown_bytes(resolved, self.settings.max_markdown_file_bytes)
        lines = data.decode("utf-8").splitlines()
        headings: list[dict[str, Any]] = []
        fence_character: str | None = None
        fence_length = 0
        previous: tuple[int, str] | None = None
        for number, line in enumerate(lines, start=1):
            fence = _FENCE.match(line)
            if fence:
                marker = fence.group(1)
                if fence_character is None:
                    fence_character, fence_length = marker[0], len(marker)
                elif marker[0] == fence_character and len(marker) >= fence_length:
                    fence_character = None
                previous = None
                continue
            if fence_character is not None:
                continue
            match = _ATX_HEADING.match(line)
            if match:
                headings.append(
                    {"level": len(match.group(1)), "text": match.group(2).strip(), "line": number}
                )
                previous = None
                continue
            setext = _SETEXT_HEADING.match(line)
            if setext and previous is not None and previous[1].strip():
                headings.append(
                    {
                        "level": 1 if setext.group(1).startswith("=") else 2,
                        "text": previous[1].strip(),
                        "line": previous[0],
                    }
                )
                previous = None
                continue
            previous = (number, line) if line.strip() else None
        return {
            "project_id": project.id,
            "path": relative.as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "headings": headings,
        }

    async def metadata(self, project: ProjectPolicy, path: str) -> dict[str, Any]:
        resolved, relative = resolve_project_path(
            project, path, require_markdown=True, require_file=True
        )
        data = _read_markdown_bytes(resolved, self.settings.max_markdown_file_bytes)
        return {
            "project_id": project.id,
            **await markdown_metadata(project, resolved, relative, data=data),
        }

    async def search(
        self,
        project: ProjectPolicy,
        query: str,
        path: str = ".",
        max_results: int = 30,
    ) -> dict[str, Any]:
        needle = query.strip()
        if not needle:
            raise RepositoryReadError("query must not be empty")
        if len(needle) > 500:
            raise RepositoryReadError("query exceeds the 500-character limit")
        if not 1 <= max_results <= self.settings.max_markdown_search_results:
            raise RepositoryReadError(
                "max_results exceeds the configured Markdown search limit"
            )
        target, target_relative = resolve_project_path(
            project, path, allow_root=True
        )
        if target.is_file() and target.suffix.lower() != ".md":
            raise RepositoryReadError("search target must be a directory or Markdown file")
        arguments = [
            "rg",
            "--json",
            "--fixed-strings",
            "--ignore-case",
            "--glob",
            "*.md",
            "--glob",
            "!.*",
            "--max-columns",
            "2000",
            "--max-filesize",
            str(self.settings.max_markdown_file_bytes),
        ]
        for protected in project.protected_paths:
            arguments.extend(("--glob", f"!{protected}/**"))
            arguments.extend(("--glob", f"!{protected}"))
        arguments.extend(("--", needle, target_relative.as_posix()))
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                cwd=str(project.root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=262144,
            )
        except FileNotFoundError as exc:
            raise RepositoryReadError("ripgrep (rg) is required for Markdown search") from exc
        assert process.stdout is not None
        matches: list[dict[str, Any]] = []
        truncated = False
        while line := await process.stdout.readline():
            event = json.loads(line)
            if event.get("type") != "match":
                continue
            data = event["data"]
            relative = PurePosixPath(data["path"]["text"])
            if any(part.startswith(".") for part in relative.parts) or _protected(project, relative):
                continue
            for submatch in data.get("submatches", []):
                if len(matches) >= max_results:
                    truncated = True
                    process.terminate()
                    break
                matches.append(
                    {
                        "path": relative.as_posix(),
                        "line": data["line_number"],
                        "column": submatch["start"] + 1,
                        "excerpt": data["lines"]["text"].rstrip("\r\n")[:500],
                    }
                )
            if truncated:
                break
        _, stderr = await process.communicate()
        if process.returncode not in {0, 1, -15}:
            detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
            raise RepositoryReadError(f"Markdown search failed: {detail}")
        return {
            "project_id": project.id,
            "query": needle,
            "path": target_relative.as_posix(),
            "matches": matches,
            "truncated": truncated,
        }
