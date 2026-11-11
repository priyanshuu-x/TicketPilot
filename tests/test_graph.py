import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from mcp.shared.memory import create_connected_server_and_client_session

from ticketpilot.agents.graph import Decision, build_support_graph
from ticketpilot.mcp_server import server
from ticketpilot.mcp_server.db import connect
from ticketpilot.memory import customer_namespace, thread_config

from .fakes import call, fake_llm, grounded

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("fresh_db")]


def plan(*specialists):
    return Decision(intent="refund", priority="normal", plan=list(specialists), reason="test")


def memo(text="memo"):
    return AIMessage(text)


def reply(text):
    return [AIMessage(text), grounded(), memo()]


async def run_turns(ticket_id, turns, script, store=None, **limits):
    llm = fake_llm(*script)
    store = store or InMemoryStore()
    async with create_connected_server_and_client_session(server.mcp) as session:
        tools = await load_mcp_tools(session)
        graph = build_support_graph(
            tools, store, InMemorySaver(), lambda configure: configure(llm), **limits
        )
        for message in turns:
            new = [HumanMessage(message)] if message else []
            state = {"ticket_id": ticket_id, "messages": new, "findings": {}}
            result = await graph.ainvoke(state, thread_config(ticket_id))
    return result, store


def ticket_status(ticket_id):
    with connect() as conn:
        return conn.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()[0]


async def test_full_team_resolves_refund_and_remembers_it():
    result, store = await run_turns(
        1,
        [None],
        [
            plan("investigator", "policy_advisor", "resolver"),
            call("get_order", order_id="A100"),
            AIMessage("A100: earbuds, Rs 1499, delivered 8 days ago."),
            plan("policy_advisor", "resolver"),
            AIMessage("Full refund of Rs 1499, no approval needed."),
            plan("resolver"),
            call("issue_refund", order_id="A100", amount=1499, reason="Keeps disconnecting"),
            call("update_ticket", ticket_id=1, status="resolved", note="Refunded"),
            AIMessage("We've refunded Rs 1499."),
            grounded(),
            memo("Earbuds A100 refunded, Rs 1499."),
        ],
    )
    assert result["reply"] == "We've refunded Rs 1499."
    assert set(result["findings"]) == {"investigator", "policy_advisor", "resolver"}
    assert server.get_order("A100")["refunds"][0]["amount"] == 1499
    assert ticket_status(1) == "resolved"
    memory = await store.aget(customer_namespace("C1"), "ticket-1")
    assert "Earbuds A100 refunded, Rs 1499." in memory.value["text"]


async def test_follow_up_continues_the_same_conversation():
    result, _ = await run_turns(
        2,
        [None, "Thanks! When will it arrive?"],
        [plan(), *reply("It has shipped."), plan(), *reply("Within 7 days.")],
    )
    texts = [m.text for m in result["messages"]]
    assert texts[1:] == ["It has shipped.", "Thanks! When will it arrive?", "Within 7 days."]
    assert result["handoffs"] == 1


async def test_each_turn_sees_actions_already_taken():
    result, _ = await run_turns(
        3,
        [None, "Any update?"],
        [
            plan(),
            call("update_ticket", ticket_id=3, status="escalated", note="Needs approval"),
            *reply("Escalated."),
            plan(),
            *reply("Still with the review team."),
        ],
    )
    assert result["ticket_status"] == "escalated"
    assert result["ticket_notes"] == "Needs approval\n"


async def test_long_conversation_is_summarized():
    result, _ = await run_turns(
        2,
        [None, "And when will it arrive?"],
        [plan(), *reply("It has shipped."), plan(), *reply("Within 7 days.")]
        + [AIMessage("Phone case A102 has shipped.")],
        max_messages=2,
    )
    assert result["summary"] == "Phone case A102 has shipped."
    assert [m.text for m in result["messages"]] == ["And when will it arrive?", "Within 7 days."]


async def test_past_tickets_are_recalled():
    store = InMemoryStore()
    await store.aput(customer_namespace("C2"), "ticket-9", {"text": "Earlier case"})
    result, _ = await run_turns(2, [None], [plan(), *reply("Shipped.")], store=store)
    assert result["memories"] == ["Earlier case"]


async def test_escalates_after_too_many_handoffs():
    result, _ = await run_turns(
        3,
        [None],
        [plan("investigator"), AIMessage("facts")] * 2 + [plan("investigator"), memo()],
        max_handoffs=2,
    )
    assert "our team will get back to you" in result["reply"]
    assert ticket_status(3) == "escalated"
