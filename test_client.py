import argparse
import asyncio
import json
import time

from fastmcp import Client


async def main(project_id: str, endpoint: str) -> None:
    async with Client(endpoint) as client:
        tools = await client.list_tools()
        print("Discovered tools:")
        for tool in tools:
            print(f"- {tool.name}")

        projects = await client.call_tool("list_allowed_projects", {})
        print("\nAllowed projects:")
        print(json.dumps(projects.data, indent=2))

        status = await client.call_tool(
            "get_project_status", {"project_id": project_id}
        )
        print("\nProject status:")
        print(json.dumps(status.data, indent=2))

        started = await client.call_tool(
            "send_to_local_dev",
            {
                "project_id": project_id,
                "text": (
                    "This is an MCPB-005 connectivity test. Do not inspect or "
                    "modify files and do not use tools. Respond exactly: MCPB_005_OK"
                ),
                "effort": "high",
            },
        )
        print("\nDevelopment job started:")
        print(json.dumps(started.data, indent=2))

        job_id = started.data["job_id"]
        deadline = time.monotonic() + 1800
        while True:
            result = await client.call_tool(
                "get_development_job",
                {"project_id": project_id, "job_id": job_id},
            )
            if result.data["status"] in {
                "completed",
                "failed",
                "timed_out",
                "cancelled",
            }:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("development job did not finish within 1800 seconds")
            await asyncio.sleep(1)

        print("\nDevelopment job result:")
        print(json.dumps(result.data, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000/mcp")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.project, arguments.endpoint))
