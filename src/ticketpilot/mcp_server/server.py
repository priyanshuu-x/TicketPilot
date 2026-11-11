import asyncio
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import numpy as np
from mcp.server.fastmcp import FastMCP

from ticketpilot.config import settings
from ticketpilot.embeddings import embed
from ticketpilot.mcp_server import APPROVAL_REQUIRED
from ticketpilot.mcp_server.db import connect, days_ago, seed

REFUND_WINDOW_DAYS = 30
POLICY_DIR = Path(__file__).parent / "policies"

mcp = FastMCP(
    "support-desk",
    instructions="Customer-support tools: customers, orders, refunds, tickets and policies.",
    host=settings.mcp_host,
    port=settings.mcp_port,
)


@mcp.tool()
def get_ticket(ticket_id: int) -> dict[str, Any]:
    """Get a support ticket, including the customer's name and email."""
    with connect() as conn:
        row = conn.execute(
            "SELECT tickets.*, customers.name, customers.email FROM tickets "
            "JOIN customers ON customers.id = tickets.customer_id WHERE tickets.id = ?",
            (ticket_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"Ticket {ticket_id} not found")
    return dict(row)


@mcp.tool()
def create_ticket(customer_email: str, subject: str, body: str) -> dict[str, Any]:
    """Open a new support ticket for a customer. Used by the app, not by agents."""
    customer = lookup_customer(customer_email)
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO tickets (customer_id, subject, body, status) VALUES (?, ?, ?, 'open')",
            (customer["id"], subject, body),
        )
    ticket: dict[str, Any] = get_ticket(cursor.lastrowid or 0)
    return ticket


@mcp.tool()
def list_tickets() -> list[dict[str, Any]]:
    """All tickets, newest first, with the customer's name. Used by the app, not by agents."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT tickets.id, tickets.subject, tickets.status, customers.name FROM tickets "
            "JOIN customers ON customers.id = tickets.customer_id ORDER BY tickets.id DESC"
        )
        return [dict(row) for row in rows]


@mcp.tool()
def lookup_customer(email: str) -> dict[str, Any]:
    """Find a customer by email. Returns id, name, email and tier."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM customers WHERE email = ?", (email.lower(),)).fetchone()
    if row is None:
        raise ValueError(f"No customer with email {email}")
    return dict(row)


@mcp.tool()
def search_orders(customer_id: str) -> list[dict[str, Any]]:
    """List all orders of a customer (e.g. "C1"), each with its refunds."""
    with connect() as conn:
        rows = conn.execute("SELECT id FROM orders WHERE customer_id = ?", (customer_id,))
        order_ids = [row["id"] for row in rows]
    return [get_order(order_id) for order_id in order_ids]


@mcp.tool()
def get_order(order_id: str) -> dict[str, Any]:
    """Get one order (e.g. "A100") with the refunds already made on it."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise ValueError(f"Order {order_id} not found")
        refunds = conn.execute("SELECT * FROM refunds WHERE order_id = ?", (order_id,))
        return dict(row) | {"refunds": [dict(r) for r in refunds]}


@cache
def policy_index() -> tuple[list[tuple[str, str]], np.ndarray]:
    chunks = [
        (path.stem, line.lstrip("- "))
        for path in POLICY_DIR.glob("*.md")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]
    vectors = np.array(embed([f"{policy} policy: {rule}" for policy, rule in chunks]))
    return chunks, vectors


@mcp.tool()
def search_kb(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Search the policy knowledge base. Returns the most relevant policy rules."""
    chunks, vectors = policy_index()
    scores = vectors @ np.array(embed([query])[0])
    best = scores.argsort()[::-1][:top_k]
    return [
        {"policy": chunks[i][0], "rule": chunks[i][1], "score": round(float(scores[i]), 2)}
        for i in best
    ]


@mcp.tool()
def issue_refund(
    order_id: str, amount: float, reason: str, customer_id: str, approval_id: str | None = None
) -> dict[str, Any]:
    """Refund a delivered order of the given customer. Refunds above the approval limit, or a
    customer's second refund within 30 days, fail unless a human reviewer has approved them."""
    with connect() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise ValueError(f"Order {order_id} not found")
        if order["customer_id"] != customer_id:
            raise PermissionError(f"Order {order_id} does not belong to customer {customer_id}")
        if order["status"] != "delivered":
            raise ValueError("Only delivered orders can be refunded")
        if (date.today() - date.fromisoformat(order["delivered_on"])).days > REFUND_WINDOW_DAYS:
            raise ValueError(f"Order is outside the {REFUND_WINDOW_DAYS}-day refund window")

        refunded = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM refunds WHERE order_id = ?", (order_id,)
        ).fetchone()[0]
        if not 0 < amount <= order["amount"] - refunded:
            raise ValueError(f"Amount must be between 0 and {order['amount'] - refunded}")

        recent_refunds = conn.execute(
            "SELECT COUNT(*) FROM refunds JOIN orders ON orders.id = refunds.order_id "
            "WHERE orders.customer_id = ? AND refunds.created_on >= ?",
            (order["customer_id"], days_ago(REFUND_WINDOW_DAYS)),
        ).fetchone()[0]
        if amount > settings.refund_approval_limit or recent_refunds:
            approval = conn.execute(
                "DELETE FROM approvals WHERE id = ? AND order_id = ? AND amount = ? RETURNING id",
                (approval_id, order_id, amount),
            ).fetchone()
            if approval is None:
                raise PermissionError(APPROVAL_REQUIRED)

        cursor = conn.execute(
            "INSERT INTO refunds (order_id, amount, reason, created_on) VALUES (?, ?, ?, ?)",
            (order_id, amount, reason, date.today().isoformat()),
        )
    return {"refund_id": cursor.lastrowid, "order_id": order_id, "amount": amount}


@mcp.tool()
def record_approval(order_id: str, amount: float) -> dict[str, str]:
    """Record a human reviewer's approval for one refund. Called by the app after a person
    approves; no agent is given this tool. (In production, protect it with auth.)"""
    approval_id = uuid4().hex
    with connect() as conn:
        conn.execute("INSERT INTO approvals VALUES (?, ?, ?)", (approval_id, order_id, amount))
    return {"approval_id": approval_id}


@mcp.tool()
def update_ticket(
    ticket_id: int, status: Literal["open", "resolved", "escalated"], note: str
) -> dict[str, Any]:
    """Change a ticket's status and add a note. Use "escalated" to hand it to a human agent."""
    with connect() as conn:
        row = conn.execute(
            "UPDATE tickets SET status = ?, notes = notes || ? WHERE id = ? RETURNING *",
            (status, note + "\n", ticket_id),
        ).fetchone()
    if row is None:
        raise ValueError(f"Ticket {ticket_id} not found")
    return dict(row)


@mcp.resource("policy://{name}", mime_type="text/markdown")
def policy(name: str) -> str:
    """A full policy document: refund, shipping, damaged or escalation."""
    path = POLICY_DIR / f"{name}.md"
    if not path.exists():
        raise ValueError(f"No policy named {name}")
    return path.read_text(encoding="utf-8")


if __name__ == "__main__":
    if not Path(settings.support_db_path).exists():
        seed()
    asyncio.run(mcp.run_streamable_http_async(), loop_factory=asyncio.SelectorEventLoop)
