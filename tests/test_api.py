import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from mcp.shared.memory import create_connected_server_and_client_session

from ticketpilot.agents.graph import build_support_graph
from ticketpilot.api import create_app
from ticketpilot.mcp_server import server

from .fakes import call, fake_llm, grounded
from .test_graph import plan

pytestmark = pytest.mark.usefixtures("fresh_db")


def api(*script):
    llm = fake_llm(*script)

    @asynccontextmanager
    async def setup():
        async with create_connected_server_and_client_session(server.mcp) as session:
            tools = await load_mcp_tools(session)
            graph = build_support_graph(
                tools, InMemoryStore(), InMemorySaver(), lambda configure: configure(llm)
            )
            yield graph, {tool.name: tool for tool in tools}

    return TestClient(create_app(setup))


def events(response):
    assert response.status_code == 200
    return [json.loads(line) for line in response.iter_lines()]


def test_create_ticket_then_run_it():
    script = [plan(), AIMessage("Your phone case has shipped."), grounded(), AIMessage("memo")]
    with api(*script) as client:
        ticket = client.post(
            "/tickets",
            json={
                "customer_email": "diya@example.com",
                "subject": "Order",
                "message": "Where is my case?",
            },
        )
        assert ticket.status_code == 201
        ticket_id = ticket.json()["id"]

        run = events(client.post(f"/tickets/{ticket_id}/run", json={}))
        top_level = [e["node"] for e in run if e["type"] == "step" and ">" not in e["node"]]
        assert top_level == [
            "start", "guard_input", "recall", "supervisor", "resolver",
            "check_reply", "remember", "compact",
        ]  # fmt: skip
        assert run[-1] == {"type": "done", "reply": "Your phone case has shipped."}

        details = client.get(f"/tickets/{ticket_id}").json()
        assert [m["role"] for m in details["conversation"]] == ["customer", "support"]
        assert client.get("/tickets").json()[0]["id"] == ticket_id
        assert client.get(f"/tickets/{ticket_id}/history").json()


def test_refund_pauses_for_review_then_resumes():
    script = [
        plan("resolver"),
        call("issue_refund", order_id="A103", amount=2499, reason="Broken"),
        AIMessage("Your refund is done."),
        grounded(),
        AIMessage("memo"),
    ]
    with api(*script) as client:
        paused = events(client.post("/tickets/3/run", json={}))[-1]
        assert paused["type"] == "paused" and paused["request"]["amount"] == 2499
        assert client.get("/tickets/3").json()["pending_review"]["order_id"] == "A103"
        assert client.get("/tickets").json()[2]["waiting_for_review"]

        conflict = client.post("/tickets/3/run", json={"message": "Hello?"})
        assert conflict.status_code == 409

        done = events(client.post("/tickets/3/review", json={"action": "approve"}))[-1]
        assert done == {"type": "done", "reply": "Your refund is done."}
    assert server.get_order("A103")["refunds"][0]["amount"] == 2499


def test_unknown_ticket_is_404():
    with api() as client:
        assert client.get("/tickets/99").status_code == 404
        assert client.post("/tickets/99/run", json={}).status_code == 404
        assert client.get("/health").json() == {"status": "ok"}
