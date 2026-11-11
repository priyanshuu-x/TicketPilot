import json
from typing import Any

import httpx
import streamlit as st

from ticketpilot.config import settings

API = settings.api_url
CUSTOMERS = {
    "Aarav Sharma": "aarav@example.com",
    "Diya Patel": "diya@example.com",
    "Kabir Singh": "kabir@example.com",
}
STATUS_ICONS = {"open": "🟡", "resolved": "🟢", "escalated": "🔴"}

st.set_page_config(page_title="TicketPilot", page_icon="🎫", layout="wide")


def get(path: str) -> Any:
    return httpx.get(f"{API}{path}", timeout=30).raise_for_status().json()


def stream(path: str, body: dict[str, Any]) -> None:
    steps = []
    with st.status("The team is working on it...", expanded=True) as status:
        with httpx.stream("POST", f"{API}{path}", json=body, timeout=600) as response:
            if response.status_code != 200:
                status.update(label=json.loads(response.read())["detail"], state="error")
                return
            for line in response.iter_lines():
                event = json.loads(line)
                if event["type"] == "step":
                    steps.append(f"**{event['node']}** · {event['text'][:300]}")
                    status.write(steps[-1])
                elif event["type"] == "error":
                    steps.append(f"❌ **Stopped:** {event['message']}")
    st.session_state.last_steps = steps
    st.rerun()


try:
    tickets = get("/tickets")
except httpx.HTTPError:
    st.error(f"Can't reach the API at {API}. Start it with: `uv run python -m ticketpilot.api`")
    st.stop()

with st.sidebar:
    st.title("🎫 TicketPilot")
    with st.expander("➕ New ticket"), st.form("new_ticket", clear_on_submit=True):
        customer = st.selectbox("Customer", list(CUSTOMERS))
        subject = st.text_input("Subject", "Refund request")
        message = st.text_area(
            "Message", "My earbuds keep disconnecting, I want a refund for A100."
        )
        if st.form_submit_button("Create ticket") and message:
            body = {"customer_email": CUSTOMERS[customer], "subject": subject, "message": message}
            ticket = httpx.post(f"{API}/tickets", json=body, timeout=30).raise_for_status().json()
            st.session_state.ticket_id = ticket["id"]
            st.session_state.last_steps = []
            st.rerun()

    labels = {
        t["id"]: f"{STATUS_ICONS.get(t['status'], '⚪')} #{t['id']} {t['subject']} · {t['name']}"
        + (" ⏸️" if t["waiting_for_review"] else "")
        for t in tickets
    }
    ids = list(labels)
    selected = st.session_state.get("ticket_id", ids[0])
    ticket_id = st.radio("Tickets", ids, index=ids.index(selected), format_func=labels.get)
    if ticket_id != st.session_state.get("ticket_id"):
        st.session_state.ticket_id, st.session_state.last_steps = ticket_id, []

details = get(f"/tickets/{ticket_id}")
ticket = details["ticket"]
st.header(f"#{ticket['id']} {ticket['subject']}")
st.caption(
    f"{ticket['name']} ({ticket['email']}) · status: {STATUS_ICONS.get(ticket['status'], '')} "
    f"{ticket['status']}" + (f" · intent: {details['intent']}" if details["intent"] else "")
)

if details["summary"]:
    st.info(f"Earlier conversation (summary): {details['summary']}")
conversation = details["conversation"] or [{"role": "customer", "text": ticket["body"]}]
for message in conversation:
    with st.chat_message("user" if message["role"] == "customer" else "assistant"):
        st.write(message["text"])

if review := details["pending_review"]:
    st.warning(
        f"⏸️ **Approval needed:** refund of Rs {review['amount']:,.0f} for order "
        f"{review['order_id']}. Reason: {review['reason']}"
    )
    with st.form("review"):
        amount = st.number_input(
            "Amount to approve (Rs)", value=float(review["amount"]), min_value=1.0
        )
        comment = st.text_input("Comment for the agent", "")
        approve, reject = st.columns(2)
        if approve.form_submit_button("✅ Approve", type="primary"):
            stream(
                f"/tickets/{ticket_id}/review",
                {"action": "approve", "amount": amount, "comment": comment},
            )
        if reject.form_submit_button("❌ Reject"):
            stream(f"/tickets/{ticket_id}/review", {"action": "reject", "comment": comment})
elif details["unfinished"]:
    st.error("The last run stopped midway (e.g. a crash). It can continue from its checkpoint.")
    if st.button("▶️ Resume run"):
        stream(f"/tickets/{ticket_id}/resume", {})
elif not details["conversation"]:
    if st.button("▶️ Run the support team on this ticket", type="primary"):
        stream(f"/tickets/{ticket_id}/run", {})
elif follow_up := st.chat_input("Reply as the customer..."):
    stream(f"/tickets/{ticket_id}/run", {"message": follow_up})

if steps := st.session_state.get("last_steps"):
    with st.expander(f"🧭 Last run: {len(steps)} steps"):
        for step in steps:
            st.markdown(step)
if details["findings"]:
    with st.expander("🔎 What the agents found"):
        for agent, text in details["findings"].items():
            st.markdown(f"**{agent}**\n\n{text}")
if details["tool_log"]:
    with st.expander(f"🛠️ Tool calls ({len(details['tool_log'])})"):
        st.dataframe(details["tool_log"], use_container_width=True)
with st.expander("🕓 Checkpoint history"):
    st.dataframe(get(f"/tickets/{ticket_id}/history"), use_container_width=True)
