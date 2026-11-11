import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session

from ticketpilot.agents.react_agent import build_react_agent
from ticketpilot.mcp_server import server

from .fakes import call, fake_llm

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fresh_db")]


async def run(*responses, max_steps=5):
    async with create_connected_server_and_client_session(server.mcp) as session:
        tools = await load_mcp_tools(session)
        agent = build_react_agent(fake_llm(*responses), tools, "prompt", max_steps)
        result = await agent.ainvoke({"messages": [HumanMessage("Help with my order")]})
    return result["messages"]


async def test_agent_calls_mcp_tool_then_answers():
    messages = await run(call("get_order", order_id="A101"), AIMessage("Not eligible."))
    assert "Smart watch" in messages[2].text
    assert messages[-1].text == "Not eligible."


async def test_server_side_refund_guard_reaches_agent():
    messages = await run(
        call("issue_refund", order_id="A103", amount=2499, reason="test", customer_id="C2"),
        AIMessage("Escalating."),
    )
    assert "needs human approval" in messages[2].text


async def test_gives_up_after_max_steps():
    messages = await run(*[call("search_kb", query="refund") for _ in range(3)], max_steps=2)
    assert "human agent" in messages[-1].text
