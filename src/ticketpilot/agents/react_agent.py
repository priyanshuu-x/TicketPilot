import asyncio
import sys
from collections.abc import Sequence
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from ticketpilot.config import settings
from ticketpilot.llm import with_fallback
from ticketpilot.mcp_client import get_mcp_tools

SYSTEM_PROMPT = (
    "You are a customer-support agent. Use the tools to look up customers, orders and "
    "policies; never guess. Follow the policies exactly. When you have enough information, "
    "reply to the customer briefly."
)


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    steps: NotRequired[int]


def build_react_agent(
    model: Runnable[LanguageModelInput, AIMessage],
    tools: Sequence[BaseTool],
    prompt: str,
    max_steps: int = settings.max_steps,
) -> CompiledStateGraph[AgentState]:
    async def agent(state: AgentState) -> dict[str, Any]:
        response = await model.ainvoke([SystemMessage(prompt), *state["messages"]])
        return {"messages": [response], "steps": state.get("steps", 0) + 1}

    def give_up(state: AgentState) -> dict[str, Any]:
        reply = "Sorry, I couldn't resolve this. A human agent will follow up."
        return {"messages": [AIMessage(reply)]}

    def route(state: AgentState) -> str:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return END
        if state.get("steps", 0) >= max_steps:
            return "give_up"
        return "tools"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("give_up", give_up)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", "give_up", END])
    graph.add_edge("tools", "agent")
    graph.add_edge("give_up", END)
    return graph.compile(name="support-agent")


async def main(question: str) -> None:
    tools = await get_mcp_tools()
    agent = build_react_agent(with_fallback(lambda m: m.bind_tools(tools)), tools, SYSTEM_PROMPT)
    async for update in agent.astream(
        {"messages": [HumanMessage(question)]}, stream_mode="updates"
    ):
        for node, change in update.items():
            for message in change["messages"]:
                if isinstance(message, AIMessage) and message.tool_calls:
                    for tool_call in message.tool_calls:
                        print(f"[{node}] -> {tool_call['name']}({tool_call['args']})")
                else:
                    print(f"[{node}] {message.text[:300]}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
