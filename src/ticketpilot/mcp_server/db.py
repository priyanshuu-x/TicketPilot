import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta

from ticketpilot.config import settings

SCHEMA = """
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS refunds;
DROP TABLE IF EXISTS tickets;
DROP TABLE IF EXISTS approvals;

CREATE TABLE customers (id TEXT PRIMARY KEY, name TEXT, email TEXT UNIQUE, tier TEXT);
CREATE TABLE orders (
    id TEXT PRIMARY KEY, customer_id TEXT, item TEXT, amount REAL, status TEXT, delivered_on TEXT
);
CREATE TABLE refunds (
    id INTEGER PRIMARY KEY, order_id TEXT, amount REAL, reason TEXT, created_on TEXT
);
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY, customer_id TEXT, subject TEXT, body TEXT, status TEXT,
    notes TEXT DEFAULT ''
);
-- Written by the API when a human approves a refund; used once by issue_refund.
CREATE TABLE approvals (id TEXT PRIMARY KEY, order_id TEXT, amount REAL);
"""


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


CUSTOMERS = [
    ("C1", "Aarav Sharma", "aarav@example.com", "gold"),
    ("C2", "Diya Patel", "diya@example.com", "standard"),
    ("C3", "Kabir Singh", "kabir@example.com", "standard"),
]
ORDERS = [
    ("A100", "C1", "Wireless earbuds", 1499, "delivered", days_ago(8)),
    ("A101", "C1", "Smart watch", 3200, "delivered", days_ago(52)),
    ("A102", "C2", "Phone case", 299, "shipped", None),
    ("A103", "C2", "Bluetooth speaker", 2499, "delivered", days_ago(5)),
    ("A104", "C3", "Laptop stand", 899, "delivered", days_ago(12)),
    ("A105", "C3", "USB-C cable", 349, "delivered", days_ago(20)),
]
REFUNDS = [("A105", 349, "Cable stopped working", days_ago(10))]
TICKETS = [
    ("C1", "Refund request", "My earbuds keep disconnecting, I want a refund for A100.", "open"),
    ("C2", "Where is my order?", "My phone case A102 hasn't arrived yet.", "open"),
    ("C2", "Speaker broken", "My Bluetooth speaker stopped working. Can I get a refund?", "open"),
    ("C3", "Refund please", "The laptop stand wobbles, I'd like a refund for order A104.", "open"),
    ("C1", "Watch refund", "I don't use my smart watch (A101) any more, please refund it.", "open"),
]


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.support_db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def seed() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", CUSTOMERS)
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", ORDERS)
        conn.executemany(
            "INSERT INTO refunds (order_id, amount, reason, created_on) VALUES (?, ?, ?, ?)",
            REFUNDS,
        )
        conn.executemany(
            "INSERT INTO tickets (customer_id, subject, body, status) VALUES (?, ?, ?, ?)",
            TICKETS,
        )


if __name__ == "__main__":
    seed()
    print(f"Seeded {settings.support_db_path}")
