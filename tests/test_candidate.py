import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from local_dev_mcp.candidate import EXPECTED_CANDIDATE_TOOLS, CandidateHealthChecker
from local_dev_mcp.config import Settings


def settings(base: Path) -> Settings:
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
            "MCP_PORT": "9200",
            "CANDIDATE_MCP_PORT": "9201",
            "CANDIDATE_HEALTH_TIMEOUT_SECONDS": "1",
        }
    )


class FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.pid = 424242
        self.returncode = None
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        self.returncode = 0
        return self._stdout, self._stderr


class CandidateHealthCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "candidate"
        self.root.mkdir()
        (self.root / "server.py").write_text("# candidate\n", encoding="utf-8")
        self.settings = settings(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_healthy_probe_uses_local_separate_port_and_stops_process(self) -> None:
        calls: list[str] = []

        async def probe(url: str) -> list[str]:
            calls.append(url)
            return sorted(EXPECTED_CANDIDATE_TOOLS)

        process = FakeProcess()
        create = AsyncMock(return_value=process)
        with (
            patch.dict(os.environ, {"SECRET_TOKEN": "must-not-reach-candidate"}),
            patch(
                "local_dev_mcp.candidate.asyncio.create_subprocess_exec", create
            ),
            patch("local_dev_mcp.candidate.os.killpg") as killpg,
        ):
            result = asyncio.run(CandidateHealthChecker(self.settings, probe).check(self.root))

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["port"], 9201)
        self.assertEqual(calls, ["http://127.0.0.1:9201/mcp"])
        arguments = create.await_args.args
        keywords = create.await_args.kwargs
        self.assertEqual(Path(arguments[1]), self.root / "server.py")
        self.assertEqual(keywords["cwd"], str(self.root))
        self.assertEqual(keywords["env"]["MCP_HOST"], "127.0.0.1")
        self.assertEqual(keywords["env"]["MCP_PORT"], "9201")
        self.assertNotIn("SECRET_TOKEN", keywords["env"])
        self.assertEqual(
            keywords["env"]["PROJECTS_CONFIG_PATH"],
            str(self.settings.projects_config_path),
        )
        killpg.assert_called_once_with(process.pid, 15)

    def test_missing_tools_and_process_diagnostics_are_bounded(self) -> None:
        async def probe(_: str) -> list[str]:
            return ["list_allowed_projects"]

        process = FakeProcess(stderr=(b"failure" * 1000))
        with (
            patch(
                "local_dev_mcp.candidate.asyncio.create_subprocess_exec",
                AsyncMock(return_value=process),
            ),
            patch("local_dev_mcp.candidate.os.killpg"),
        ):
            result = asyncio.run(CandidateHealthChecker(self.settings, probe).check(self.root))

        self.assertEqual(result["status"], "unhealthy")
        self.assertIn("missing expected tools", result["error"])
        self.assertLessEqual(len(result["diagnostics"]), 4000)

    def test_missing_or_symlinked_entrypoint_fails_before_process_start(self) -> None:
        (self.root / "server.py").unlink()
        create = AsyncMock()
        with patch(
            "local_dev_mcp.candidate.asyncio.create_subprocess_exec", create
        ):
            missing = asyncio.run(CandidateHealthChecker(self.settings).check(self.root))
        self.assertEqual(missing["status"], "unhealthy")
        create.assert_not_awaited()

        outside = self.base / "outside.py"
        outside.write_text("# outside\n", encoding="utf-8")
        (self.root / "server.py").symlink_to(outside)
        with patch(
            "local_dev_mcp.candidate.asyncio.create_subprocess_exec", create
        ):
            linked = asyncio.run(CandidateHealthChecker(self.settings).check(self.root))
        self.assertIn("trusted regular file", linked["error"])
        create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
