from __future__ import annotations

import difflib
import errno
import hashlib
import os
import secrets
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .config import Settings
from .git import run_git
from .models import ProjectPolicy
from .reader import _is_within, _protected, _relative_input


WriteMode = Literal["create", "update"]


class RepositoryWriteError(ValueError):
    """Raised when a Markdown write request violates its project policy."""


def _validate_target(
    project: ProjectPolicy,
    requested: str,
) -> tuple[Path, PurePosixPath]:
    normalized = requested.replace("\\", "/").strip()
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise RepositoryWriteError(
            "path must be a normalized relative path without '.', '..', or empty segments"
        )
    try:
        relative = _relative_input(requested, allow_root=False)
    except ValueError as exc:
        raise RepositoryWriteError(str(exc).replace("readable", "writable")) from exc
    if relative.suffix.lower() != ".md":
        raise RepositoryWriteError("only Markdown (.md) files are writable")
    if _protected(project, relative):
        raise RepositoryWriteError("path is protected by project policy")
    if not any(
        _is_within(relative, PurePosixPath(configured))
        for configured in project.markdown_write_roots
    ):
        raise RepositoryWriteError("path is outside the approved Markdown write roots")

    target = project.root / relative
    cursor = project.root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise RepositoryWriteError("symbolic links are not writable")

    parent = target.parent
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_parent.relative_to(project.root)
    except FileNotFoundError as exc:
        raise RepositoryWriteError("parent directory does not exist") from exc
    except ValueError as exc:
        raise RepositoryWriteError("parent directory resolves outside the project root") from exc
    if not resolved_parent.is_dir():
        raise RepositoryWriteError("parent path is not a directory")

    resolved_relative = PurePosixPath(
        (resolved_parent / target.name).relative_to(project.root).as_posix()
    )
    if resolved_relative != relative:
        raise RepositoryWriteError("path does not resolve to the requested project location")
    if _protected(project, resolved_relative):
        raise RepositoryWriteError("resolved path is protected by project policy")
    if any(part.startswith(".") for part in resolved_relative.parts):
        raise RepositoryWriteError("resolved path is hidden")
    return resolved_parent / target.name, relative


def _encode_content(content: str, limit: int) -> bytes:
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RepositoryWriteError("content is not valid UTF-8") from exc
    if len(data) > limit:
        raise RepositoryWriteError(
            f"Markdown content exceeds the configured {limit}-byte write limit"
        )
    return data


def _validate_hash(write_mode: WriteMode, expected_sha256: str | None) -> str | None:
    if write_mode == "create":
        if expected_sha256 is not None:
            raise RepositoryWriteError("expected_sha256 is forbidden for create")
        return None
    if write_mode != "update":
        raise RepositoryWriteError("write_mode must be 'create' or 'update'")
    if expected_sha256 is None:
        raise RepositoryWriteError("expected_sha256 is required for update")
    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in expected_sha256
    ):
        raise RepositoryWriteError("expected_sha256 must be a 64-character hexadecimal hash")
    return expected_sha256.lower()


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise RepositoryWriteError("parent directory cannot be opened safely") from exc


def _read_regular_file(
    directory_fd: int,
    name: str,
    limit: int,
) -> tuple[bytes, int]:
    try:
        initial_metadata = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise RepositoryWriteError("update target does not exist") from exc
    if stat.S_ISLNK(initial_metadata.st_mode):
        raise RepositoryWriteError("symbolic links are not writable")
    if not stat.S_ISREG(initial_metadata.st_mode):
        raise RepositoryWriteError("update target is not a regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise RepositoryWriteError("update target does not exist") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RepositoryWriteError("symbolic links are not writable") from exc
        raise RepositoryWriteError("update target cannot be opened safely") from exc
    try:
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RepositoryWriteError("update target is not a regular file")
        if metadata.st_size > limit:
            raise RepositoryWriteError(
                f"current Markdown file exceeds the configured {limit}-byte write limit"
            )
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(file_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise RepositoryWriteError(
                f"current Markdown file exceeds the configured {limit}-byte write limit"
            )
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepositoryWriteError("current Markdown file is not valid UTF-8") from exc
        return data, stat.S_IMODE(metadata.st_mode)
    finally:
        os.close(file_fd)


def _write_all(file_fd: int, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = os.write(file_fd, view[offset:])
        if written == 0:
            raise OSError("short write while saving Markdown content")
        offset += written
    os.fsync(file_fd)


def _diff_lines(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"
    return lines


def _bounded_diff(
    relative: PurePosixPath,
    before: str | None,
    after: str,
    limit: int,
) -> tuple[str, bool]:
    diff = "".join(
        difflib.unified_diff(
            [] if before is None else _diff_lines(before),
            _diff_lines(after),
            fromfile="/dev/null" if before is None else f"a/{relative.as_posix()}",
            tofile=f"b/{relative.as_posix()}",
            lineterm="\n",
        )
    )
    encoded = diff.encode("utf-8")
    if len(encoded) <= limit:
        return diff, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _create(directory_fd: int, name: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    created = False
    file_fd: int | None = None
    try:
        file_fd = os.open(name, flags, 0o644, dir_fd=directory_fd)
        created = True
        _write_all(file_fd, data)
    except FileExistsError as exc:
        raise RepositoryWriteError("create target already exists") from exc
    except OSError as exc:
        if created:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        raise RepositoryWriteError("failed to create Markdown file safely") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _update(
    directory_fd: int,
    name: str,
    before: bytes,
    mode: int,
    data: bytes,
    limit: int,
) -> None:
    temporary_name: str | None = None
    file_fd: int | None = None
    try:
        for _ in range(128):
            candidate = f".{name}.{secrets.token_hex(12)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                file_fd = os.open(candidate, flags, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_name is None or file_fd is None:
            raise RepositoryWriteError("could not allocate a temporary file safely")
        os.fchmod(file_fd, mode)
        _write_all(file_fd, data)
        os.close(file_fd)
        file_fd = None

        current, _ = _read_regular_file(directory_fd, name, limit)
        if current != before:
            raise RepositoryWriteError("update target changed during the write operation")
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
    except RepositoryWriteError:
        raise
    except OSError as exc:
        raise RepositoryWriteError("failed to replace Markdown file safely") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


class RepositoryWriter:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def write(
        self,
        project: ProjectPolicy,
        path: str,
        content: str,
        write_mode: WriteMode = "create",
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        target, relative = _validate_target(project, path)
        expected = _validate_hash(write_mode, expected_sha256)
        after_data = _encode_content(content, self.settings.max_markdown_write_bytes)
        directory_fd = _open_directory(target.parent)
        try:
            before_data: bytes | None = None
            before_mode: int | None = None
            if write_mode == "update":
                before_data, before_mode = _read_regular_file(
                    directory_fd,
                    target.name,
                    self.settings.max_markdown_write_bytes,
                )
                before_sha256 = hashlib.sha256(before_data).hexdigest()
                if expected != before_sha256:
                    raise RepositoryWriteError(
                        "expected_sha256 does not match the current file"
                    )
            else:
                before_sha256 = None

            before_text = None if before_data is None else before_data.decode("utf-8")
            diff, diff_truncated = _bounded_diff(
                relative,
                before_text,
                content,
                self.settings.max_markdown_diff_bytes,
            )

            if write_mode == "create":
                _create(directory_fd, target.name, after_data)
            else:
                assert before_data is not None and before_mode is not None
                _update(
                    directory_fd,
                    target.name,
                    before_data,
                    before_mode,
                    after_data,
                    self.settings.max_markdown_write_bytes,
                )
        finally:
            os.close(directory_fd)

        git_status = await run_git(
            project.root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            relative.as_posix(),
        )
        return {
            "project_id": project.id,
            "path": relative.as_posix(),
            "write_mode": write_mode,
            "before_sha256": before_sha256,
            "after_sha256": hashlib.sha256(after_data).hexdigest(),
            "size_bytes": len(after_data),
            "line_count": len(content.splitlines()),
            "git_status": git_status,
            "diff": diff,
            "diff_truncated": diff_truncated,
        }
