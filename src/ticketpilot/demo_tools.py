from datetime import date

from langchain_core.tools import tool

ORDERS = {
    "A100": {
        "item": "Wireless earbuds",
        "amount": 1499,
        "status": "delivered",
        "delivered_on": "2026-10-15",
    },
    "A101": {
        "item": "Smart watch",
        "amount": 3200,
        "status": "delivered",
        "delivered_on": "2026-09-01",
    },
    "A102": {"item": "Phone case", "amount": 299, "status": "shipped", "delivered_on": None},
}

POLICIES = {
    "refund": "Refunds within 30 days of delivery. Above Rs 2000 needs manager approval.",
    "shipping": "Orders ship in 2 days. Not delivered after 10 days: report as lost.",
    "damaged": "Damaged items replaced free if reported within 7 days of delivery.",
}


@tool
def get_order(order_id: str) -> dict[str, object]:
    """Look up an order by its ID (e.g. "A100"). Returns item, amount, status and delivery date."""
    if order_id not in ORDERS:
        raise ValueError(f"Order {order_id} not found")
    return ORDERS[order_id]


@tool
def search_policy(topic: str) -> str:
    """Get the company policy for a topic: "refund", "shipping" or "damaged"."""
    return POLICIES.get(topic.lower(), f"No policy found. Available topics: {', '.join(POLICIES)}")


@tool
def days_since(day: str) -> int:
    """Count the days between a date in YYYY-MM-DD format and today."""
    return (date.today() - date.fromisoformat(day)).days


TOOLS = [get_order, search_policy, days_since]
