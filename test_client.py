import asyncio
import json

from fastmcp import Client


async def main() -> None:
    async with Client("http://127.0.0.1:8000/mcp") as client:
        tools = await client.list_tools()
        print("Discovered tools:")
        for tool in tools:
            print(f"- {tool.name}")

        result = await client.call_tool(
            "send_to_local_dev",
            {
                "text": (
                    "This is an MCP bridge connectivity test. "
                    "Do not inspect or modify any files and do not use tools. "
                    "Respond with exactly: MCP_BRIDGE_OK"
                )
            },
        )

        print("\nTool result:")
        print(json.dumps(result.data, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
