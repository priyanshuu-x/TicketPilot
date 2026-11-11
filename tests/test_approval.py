from contextlib import asynccontextmanager

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from mcp.shared.memory import create_connected_server_and_client_session

from ticketpilot.agents.graph import Decision, build_support_graph
from ticketpilot.mcp_server import server
from ticketpilot.memory import thread_config

from .fakes import CRASH, call, fake_llm, grounded

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fresh_db")]

TICKET = 3
CONFIG = thread_config(TICKET)
START = {"ticket_id": TICKET, "messages": [], "findings": {}}


def plan(*specialists):
    return Decision(intent="refund", priority="normal", plan=list(specialists), reason="test")


@asynccontextmanager
async def team(*script):
    llm = fake_llm(*script)
    async with create_connected_server_and_client_session(server.mcp) as session:
        tools = await load_mcp_tools(session)
        yield build_support_graph(
            tools, InMemoryStore(), InMemorySaver(), lambda configure: configure(llm)
        )


def refunds():
    return [r["amount"] for r in server.get_order("A103")["refunds"]]


async def pause_for_approval(graph):
    result = await graph.ainvoke(START, CONFIG)
    assert result["__interrupt__"][0].value == {
        "type": "refund_approval",
        "order_id": "A103",
        "amount": 2499,
        "reason": "Stopped working",
    }
    assert (await graph.aget_state(CONFIG)).interrupts
    assert refunds() == []


REFUND_CALL = call("issue_refund", order_id="A103", amount=2499, reason="Stopped working")


async def test_approved_refund_is_issued_after_resume():
    async with team(
        plan("resolver"),
        REFUND_CALL,
        call("update_ticket", ticket_id=TICKET, status="resolved", note="Refunded"),
        AIMessage("Your Rs 2499 refund is done."),
        grounded(),
        AIMessage("memo"),
    ) as graph:
        await pause_for_approval(graph)
        result = await graph.ainvoke(Command(resume={"action": "approve"}), CONFIG)
    assert refunds() == [2499]
    assert result["reply"] == "Your Rs 2499 refund is done."


async def test_reviewer_can_change_the_amount():
    async with team(
        plan("resolver"), REFUND_CALL, AIMessage("Rs 1500 refunded."), grounded(), AIMessage("memo")
    ) as graph:
        await pause_for_approval(graph)
        await graph.ainvoke(
            Command(resume={"action": "approve", "amount": 1500, "comment": "partial"}), CONFIG
        )
    assert refunds() == [1500]


async def test_rejected_refund_is_not_issued_and_resolver_is_told_why():
    tool_results = []
    async with team(
        plan("resolver"), REFUND_CALL, AIMessage("Sorry, no refund."), grounded(), AIMessage("memo")
    ) as graph:
        await pause_for_approval(graph)
        reject = Command(resume={"action": "reject", "comment": "Water damage"})
        async for _, update in graph.astream(reject, CONFIG, stream_mode="updates", subgraphs=True):
            for change in update.values():
                messages = (change or {}).get("messages", [])
                tool_results += [m.text for m in messages if isinstance(m, ToolMessage)]
        reply = (await graph.aget_state(CONFIG)).values["reply"]
    assert refunds() == []
    assert "rejected" in tool_results[0] and "Water damage" in tool_results[0]
    assert reply == "Sorry, no refund."


async def test_crashed_run_resumes_from_last_checkpoint():
    async with team(
        plan("investigator"),
        CRASH,
        AIMessage("facts"),
        plan(),
        AIMessage("Reply after recovery."),
        grounded(),
        AIMessage("memo"),
    ) as graph:
        with pytest.raises(RuntimeError, match="Simulated crash"):
            await graph.ainvoke(START, CONFIG)
        assert (await graph.aget_state(CONFIG)).next == ("investigator",)
        result = await graph.ainvoke(None, CONFIG)
    assert result["reply"] == "Reply after recovery."
