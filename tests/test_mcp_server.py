import pytest

from ticketpilot.mcp_server import APPROVAL_REQUIRED, server

pytestmark = pytest.mark.usefixtures("fresh_db")


def test_get_ticket_includes_email():
    assert server.get_ticket(3)["email"] == "diya@example.com"


def test_lookup_customer():
    assert server.lookup_customer("AARAV@example.com")["id"] == "C1"
    with pytest.raises(ValueError):
        server.lookup_customer("nobody@example.com")


def test_orders_include_refunds():
    assert len(server.get_order("A105")["refunds"]) == 1
    assert [len(o["refunds"]) for o in server.search_orders("C3")] == [0, 1]


@pytest.mark.parametrize(
    ("query", "policy"),
    [
        ("how long after delivery can I get my money back", "refund"),
        ("my package never arrived", "shipping"),
        ("item arrived broken", "damaged"),
        ("customer wants to talk to a person", "escalation"),
    ],
)
def test_search_kb_finds_relevant_policy(query, policy):
    assert policy in [result["policy"] for result in server.search_kb(query, top_k=3)]


def test_create_and_list_tickets():
    ticket = server.create_ticket("kabir@example.com", "Help", "Where is my cable?")
    assert (ticket["id"], ticket["customer_id"], ticket["status"]) == (6, "C3", "open")
    assert server.list_tickets()[0] == {
        "id": 6,
        "subject": "Help",
        "status": "open",
        "name": "Kabir Singh",
    }


def test_small_refund_is_issued():
    assert server.issue_refund("A100", 1499, "Keeps disconnecting", "C1")["amount"] == 1499
    with pytest.raises(ValueError, match="between 0 and 0"):
        server.issue_refund("A100", 1, "Refunding twice", "C1")


def test_cannot_refund_another_customers_order():
    with pytest.raises(PermissionError, match="does not belong"):
        server.issue_refund("A100", 100, "test", "C2")


@pytest.mark.parametrize(
    ("order_id", "error"),
    [("A101", "refund window"), ("A102", "Only delivered"), ("Z999", "not found")],
)
def test_invalid_refunds_are_rejected(order_id, error):
    owner = {"A101": "C1", "A102": "C2"}.get(order_id, "C1")
    with pytest.raises(ValueError, match=error):
        server.issue_refund(order_id, 100, "test", owner)


@pytest.mark.parametrize(
    ("order_id", "amount", "owner"),
    [("A103", 2499, "C2"), ("A104", 899, "C3")],
)
def test_risky_refund_needs_approval(order_id, amount, owner):
    with pytest.raises(PermissionError, match=APPROVAL_REQUIRED):
        server.issue_refund(order_id, amount, "test", owner)

    approval_id = server.record_approval(order_id, amount)["approval_id"]
    assert server.issue_refund(order_id, amount, "test", owner, approval_id)["amount"] == amount


def test_approval_works_only_once_and_only_for_its_amount():
    approval_id = server.record_approval("A103", 2100)["approval_id"]
    with pytest.raises(PermissionError):
        server.issue_refund("A103", 2499, "test", "C2", approval_id)
    assert server.issue_refund("A103", 2100, "test", "C2", approval_id)["amount"] == 2100
    with pytest.raises(PermissionError):
        server.issue_refund("A103", 300, "test", "C2", approval_id)


def test_update_ticket():
    ticket = server.update_ticket(1, "escalated", "Customer asked for a human")
    assert ticket["status"] == "escalated"
    assert "asked for a human" in ticket["notes"]


@pytest.mark.anyio
async def test_server_exposes_tools_and_policies():
    tools = {tool.name for tool in await server.mcp.list_tools()}
    assert {"lookup_customer", "get_order", "issue_refund", "update_ticket"} <= tools
    contents = await server.mcp.read_resource("policy://refund")
    assert "30 days" in contents[0].content
