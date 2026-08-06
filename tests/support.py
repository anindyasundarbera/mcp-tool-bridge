from __future__ import annotations

import json
import subprocess
from pathlib import Path


def initialize_git_repository(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test User"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (root / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "Initial"], check=True)


def write_registry(path: Path, projects: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"version": 1, "projects": projects}),
        encoding="utf-8",
    )


def project_payload(
    root: Path,
    project_id: str = "test-project",
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": project_id,
        "display_name": "Test Project",
        "description": "A test project",
        "root": str(root),
        "enabled": True,
        "require_git": True,
        "capabilities": ["repository_read", "claude_edit"],
        "markdown_write_roots": [],
        "protected_paths": [".git", ".env"],
        "default_effort": "high",
        "allow_ultracode": True,
        "self_managed": False,
    }
    payload.update(overrides)
    return payload

