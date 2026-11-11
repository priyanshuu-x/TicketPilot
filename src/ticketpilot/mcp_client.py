from typing import Any

import httpx
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from ticketpilot.config import settings


async def get_mcp_tools() -> list[BaseTool]:
    client = MultiServerMCPClient(
        {"support": {"transport": "streamable_http", "url": settings.mcp_url}}
    )
    try:
        return await client.get_tools()
    except* httpx.ConnectError:
        raise ConnectionError(
            f"Can't reach the MCP server at {settings.mcp_url}. Start it first with:\n"
            "  uv run python -m ticketpilot.mcp_server.server"
        ) from None


class ToolError(Exception):
    pass


async def call_tool(tool: BaseTool, **args: Any) -> ToolMessage:
    call = {"type": "tool_call", "name": tool.name, "args": args, "id": tool.name}
    message: ToolMessage = await tool.ainvoke(call)
    return message


async def call_tool_json(tool: BaseTool, **args: Any) -> Any:
    message = await call_tool(tool, **args)
    if message.status == "error":
        raise ToolError(message.text)
    content = message.artifact["structured_content"]
    return content["result"] if list(content) == ["result"] else content
