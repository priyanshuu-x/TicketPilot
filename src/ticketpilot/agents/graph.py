import json
import operator
from collections.abc import Callable, Sequence
from datetime import date
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import RetryPolicy
from pydantic import BaseModel, Field

from ticketpilot.agents.approval import guarded_refund_tool
from ticketpilot.agents.guardrails import REPLY_CHECK_PROMPT, ReplyCheck, check_input, redact_pii
from ticketpilot.agents.react_agent import build_react_agent
from ticketpilot.config import settings
from ticketpilot.llm import Configure, with_fallback
from ticketpilot.mcp_client import call_tool, call_tool_json
from ticketpilot.memory import customer_namespace

Specialist = Literal["investigator", "policy_advisor", "resolver"]
MakeModel = Callable[[Configure], Runnable[LanguageModelInput, Any]]


class SupportState(TypedDict):
    ticket_id: int
    customer_id: NotRequired[str]
    customer_name: NotRequired[str]
    customer_email: NotRequired[str]
    ticket_status: NotRequired[str]
    ticket_notes: NotRequired[str]
    messages: Annotated[list[AnyMessage], add_messages]
    summary: NotRequired[str]
    memories: NotRequired[list[str]]
    intent: NotRequired[str]
    priority: NotRequired[str]
    plan: NotRequired[list[Specialist]]
    findings: NotRequired[Annotated[dict[str, str], operator.or_]]
    tool_log: NotRequired[Annotated[list[dict[str, str]], operator.add]]
    guard_flag: NotRequired[str | None]
    reply: NotRequired[str]
    handoffs: NotRequired[int]


class Decision(BaseModel):
    """The supervisor's structured output."""

    intent: Literal["refund", "order_status", "damaged_item", "other"]
    priority: Literal["low", "normal", "high"]
    plan: list[Specialist] = Field(description="Specialists still to run, in order")
    reason: str = Field(description="One sentence explaining the plan")


SUPERVISOR_PROMPT = """You supervise a customer-support team. Read the ticket and what the \
specialists found so far, then plan who works next.
- investigator: looks up the customer and their orders.
- policy_advisor: checks company policy and decides what the customer is entitled to.
- resolver: takes the action (refund, ticket update) and writes the reply. Always last.
Don't plan a specialist again if their findings already answer the customer's latest message."""

SPECIALISTS: dict[Specialist, tuple[str, list[str]]] = {
    "investigator": (
        "You are the investigator. Find the facts for this ticket: the customer, their orders "
        "and which order the ticket is about. Don't decide anything. Reply with a short factual "
        "summary: customer id, order id, item, amount, status, delivery date, and every refund "
        "the customer got in the last 30 days (on any order).",
        ["lookup_customer", "search_orders", "get_order"],
    ),
    "policy_advisor": (
        "You are the policy advisor. Using the facts found so far, look up the relevant policies "
        "and decide what the customer is entitled to. Reply with: the decision, the refund "
        "amount if any, whether human approval is needed, and the policy you used.",
        ["search_kb"],
    ),
    "resolver": (
        "You are the resolver. First read 'Actions already taken on this ticket'. If the decision "
        "was already carried out (refund issued, or ticket already escalated or resolved), call "
        "no tools: just reply to the customer's latest message. Otherwise carry out the decision: "
        "if a refund is allowed, call issue_refund. Refunds that need human approval wait for a "
        "reviewer automatically, so don't escalate them yourself; if the reviewer rejects one, "
        "don't retry it. Then update the ticket to resolved, or to escalated if you can't "
        "resolve it. If the customer asked a question, answer it using the findings. If "
        "information is missing or wrong (e.g. an unknown order id), ask the customer for it "
        "instead of escalating. Write a short, friendly reply to the customer's latest message, "
        "using their name. Only mention actions that actually succeeded.",
        ["issue_refund", "update_ticket"],
    ),
}

SUMMARY_PROMPT = (
    "Summarize this customer-support conversation in a few sentences. Keep every fact, "
    "decision and action taken (order ids, amounts, refunds, escalations)."
)

MEMORY_PROMPT = (
    "Write one or two sentences about this support case for future reference: what the "
    "customer wanted (item, order id, amount) and the outcome (refunded, refused, escalated)."
)


def conversation(messages: Sequence[AnyMessage]) -> str:
    return "\n".join(
        f"{'Customer' if isinstance(m, HumanMessage) else 'Support'}: {m.text}" for m in messages
    )


def last_customer_message(state: SupportState) -> str:
    return next(m.text for m in reversed(state["messages"]) if isinstance(m, HumanMessage))


def ticket_context(state: SupportState) -> str:
    parts = [
        f"Today is {date.today()}. All amounts are in Indian rupees (Rs).",
        f"Ticket #{state['ticket_id']} from {state['customer_name']} ({state['customer_email']}), "
        f"status: {state['ticket_status']}.",
    ]
    if notes := state.get("ticket_notes"):
        parts.append(f"Actions already taken on this ticket:\n{notes}")
    if memories := state.get("memories"):
        parts.append("This customer's past tickets:\n" + "\n".join(f"- {m}" for m in memories))
    if summary := state.get("summary"):
        parts.append(f"Earlier conversation (summary):\n{summary}")
    parts.append(f"Conversation:\n{conversation(state['messages'])}")
    parts += [f"{name} found:\n{text}" for name, text in state.get("findings", {}).items()]
    return "\n\n".join(parts)


def build_support_graph(
    tools: Sequence[BaseTool],
    store: BaseStore,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    make_model: MakeModel = with_fallback,
    max_handoffs: int = settings.max_handoffs,
    max_messages: int = settings.max_messages,
) -> CompiledStateGraph[SupportState]:
    tools_by_name = {tool.name: tool for tool in tools}
    agent_tools = tools_by_name | {"issue_refund": guarded_refund_tool(tools_by_name)}
    planner = make_model(lambda m: m.with_structured_output(Decision))
    checker = make_model(lambda m: m.with_structured_output(ReplyCheck))
    summarizer = make_model(lambda m: m)

    async def start(state: SupportState) -> dict[str, Any]:
        ticket = await call_tool_json(tools_by_name["get_ticket"], ticket_id=state["ticket_id"])
        update: dict[str, Any] = {
            "handoffs": 0,
            "ticket_status": ticket["status"],
            "ticket_notes": ticket["notes"],
        }
        if "customer_id" not in state:
            update |= {
                "customer_id": ticket["customer_id"],
                "customer_name": ticket["name"],
                "customer_email": ticket["email"],
                "messages": [HumanMessage(ticket["body"])],
            }
        return update

    def guard_input(state: SupportState) -> dict[str, Any]:
        return {"guard_flag": check_input(last_customer_message(state))}

    def route_input(state: SupportState) -> str:
        return "escalate" if state.get("guard_flag") else "recall"

    async def recall(state: SupportState) -> dict[str, Any]:
        items = await store.asearch(
            customer_namespace(state["customer_id"]), query=last_customer_message(state), limit=3
        )
        this_ticket = f"ticket-{state['ticket_id']}"
        return {"memories": [item.value["text"] for item in items if item.key != this_ticket]}

    async def supervisor(state: SupportState) -> dict[str, Any]:
        decision: Decision = await planner.ainvoke(
            [SystemMessage(SUPERVISOR_PROMPT), HumanMessage(ticket_context(state))]
        )
        return {
            "intent": decision.intent,
            "priority": decision.priority,
            "plan": decision.plan,
            "handoffs": state.get("handoffs", 0) + 1,
        }

    def route(state: SupportState) -> str:
        if state["handoffs"] > max_handoffs:
            return "escalate"
        return state["plan"][0] if state["plan"] else "resolver"

    def specialist_node(name: Specialist) -> Any:
        prompt, tool_names = SPECIALISTS[name]
        own_tools = [agent_tools[t] for t in tool_names]
        agent = build_react_agent(make_model(lambda m: m.bind_tools(own_tools)), own_tools, prompt)

        async def node(state: SupportState) -> dict[str, Any]:
            result = await agent.ainvoke({"messages": [HumanMessage(ticket_context(state))]})
            messages = result["messages"]
            results = {m.tool_call_id: m.text for m in messages if isinstance(m, ToolMessage)}
            tool_log = [
                {"agent": name, "tool": c["name"], "args": json.dumps(c["args"])}
                | {"result": results.get(c["id"] or "", "")[:300]}
                for m in messages
                if isinstance(m, AIMessage)
                for c in m.tool_calls
            ]
            text = messages[-1].text
            update = {"findings": {name: text}, "tool_log": tool_log}
            return update | ({"reply": text} if name == "resolver" else {})

        return node

    async def check_reply(state: SupportState) -> dict[str, Any]:
        actions = "\n".join(
            f"- {e['agent']}: {e['tool']}({e['args']}) -> {e['result']}"
            for e in state.get("tool_log", [])
        )
        check: ReplyCheck = await checker.ainvoke(
            [
                SystemMessage(REPLY_CHECK_PROMPT),
                HumanMessage(
                    f"{ticket_context(state)}\n\nTool calls made:\n{actions or '(none)'}"
                    f"\n\nDraft reply:\n{state['reply']}"
                ),
            ]
        )
        reply = state["reply"] if check.grounded else check.corrected_reply
        reply = redact_pii(reply, allowed_email=state["customer_email"])
        return {"reply": reply, "messages": [AIMessage(reply)]}

    async def escalate(state: SupportState) -> dict[str, Any]:
        if flag := state.get("guard_flag"):
            note = f"Escalated by the input guardrail: {flag}."
        else:
            note = f"Auto-escalated after {max_handoffs} supervisor decisions."
        await call_tool(
            tools_by_name["update_ticket"],
            ticket_id=state["ticket_id"],
            status="escalated",
            note=note,
        )
        reply = "Thanks for your patience. A member of our team will get back to you."
        return {"reply": reply, "messages": [AIMessage(reply)]}

    async def remember(state: SupportState) -> dict[str, Any]:
        summary = await summarizer.ainvoke(
            [SystemMessage(MEMORY_PROMPT), HumanMessage(ticket_context(state))]
        )
        text = redact_pii(f"Ticket #{state['ticket_id']} ({date.today()}): {summary.text}")
        key = f"ticket-{state['ticket_id']}"
        await store.aput(customer_namespace(state["customer_id"]), key, {"text": text})
        return {}

    async def compact(state: SupportState) -> dict[str, Any]:
        if len(state["messages"]) <= max_messages:
            return {}
        old = state["messages"][:-2]
        previous = f"Summary so far: {state['summary']}\n\n" if state.get("summary") else ""
        summary = await summarizer.ainvoke(
            [SystemMessage(SUMMARY_PROMPT), HumanMessage(previous + conversation(old))]
        )
        removed = [RemoveMessage(id=m.id) for m in old if m.id]
        return {"summary": summary.text, "messages": removed}

    graph = StateGraph(SupportState)
    graph.add_node("start", start, retry_policy=RetryPolicy(max_attempts=3))
    graph.add_node("guard_input", guard_input)
    graph.add_node("recall", recall, retry_policy=RetryPolicy(max_attempts=3))
    graph.add_node("supervisor", supervisor)
    for name in SPECIALISTS:
        graph.add_node(name, specialist_node(name))
    graph.add_node("check_reply", check_reply)
    graph.add_node("escalate", escalate)
    graph.add_node("remember", remember)
    graph.add_node("compact", compact)

    graph.add_edge(START, "start")
    graph.add_edge("start", "guard_input")
    graph.add_conditional_edges("guard_input", route_input, ["recall", "escalate"])
    graph.add_edge("recall", "supervisor")
    graph.add_conditional_edges("supervisor", route, [*SPECIALISTS, "escalate"])
    graph.add_edge("investigator", "supervisor")
    graph.add_edge("policy_advisor", "supervisor")
    graph.add_edge("resolver", "check_reply")
    graph.add_edge("check_reply", "remember")
    graph.add_edge("escalate", "remember")
    graph.add_edge("remember", "compact")
    graph.add_edge("compact", END)
    return graph.compile(checkpointer=checkpointer, name="support-team")
