from collections.abc import AsyncIterator
from typing import Any, Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from ticketpilot.agents.graph import SupportState
from ticketpilot.memory import thread_config

Graph = CompiledStateGraph[SupportState]
GraphInput = SupportState | Command[Any] | None


class RunError(Exception):
    pass


async def prepare(
    graph: Graph,
    ticket_id: int,
    kind: Literal["run", "review", "resume"],
    message: str | None = None,
    review: dict[str, Any] | None = None,
) -> GraphInput:
    state = await graph.aget_state(thread_config(ticket_id))
    waiting = bool(state.interrupts)
    crashed = bool(state.next) and not waiting

    if waiting and kind != "review":
        raise RunError(f"Ticket {ticket_id} is waiting for a reviewer.")
    if crashed and kind != "resume":
        raise RunError(f"Ticket {ticket_id}'s last run stopped midway. Resume it first.")
    if kind == "run":
        if state.values and not message:
            raise RunError(f"Ticket {ticket_id} was already handled. Send a follow-up message.")
        new: list[AnyMessage] = [HumanMessage(message)] if message else []
        return {"ticket_id": ticket_id, "messages": new}
    if kind == "review":
        if not waiting:
            raise RunError(f"Ticket {ticket_id} has nothing waiting for review.")
        return Command(resume=review)
    if not crashed:
        raise RunError(f"Ticket {ticket_id} has no unfinished run.")
    return None


def describe(change: dict[str, Any]) -> str:
    if "messages" in change and change["messages"]:
        message = change["messages"][-1]
        if isinstance(message, AIMessage) and message.tool_calls:
            return ", ".join(f"-> {c['name']}({c['args']})" for c in message.tool_calls)
        return str(message.text)
    return str(change)


async def events(
    graph: Graph, graph_input: GraphInput, config: RunnableConfig
) -> AsyncIterator[dict[str, Any]]:
    async for part in graph.astream(
        graph_input, config, stream_mode="updates", subgraphs=True, version="v2"
    ):
        if part["type"] == "updates":
            for node, change in part["data"].items():
                if node != "__interrupt__":
                    where = " > ".join([ns.split(":")[0] for ns in part["ns"]] + [node])
                    yield {"type": "step", "node": where, "text": describe(change or {})}
    state = await graph.aget_state(config)
    if state.interrupts:
        yield {"type": "paused", "request": state.interrupts[0].value}
    else:
        yield {"type": "done", "reply": state.values.get("reply", "")}
