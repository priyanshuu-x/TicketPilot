import logging
import sys

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from ticketpilot.config import settings
from ticketpilot.demo_tools import TOOLS
from ticketpilot.llm import get_llm

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a customer-support agent. Use the tools to look up facts; never guess order "
    "details or policies. When you have enough information, reply to the customer briefly."
)


def run_agent(llm: BaseChatModel, tools: list[BaseTool], question: str, max_steps: int) -> str:
    tools_by_name = {t.name: t for t in tools}
    model = llm.bind_tools(tools)
    messages: list[BaseMessage] = [SystemMessage(SYSTEM_PROMPT), HumanMessage(question)]

    for step in range(1, max_steps + 1):
        response = model.invoke(messages)
        messages.append(response)

        if not response.tool_calls:
            logger.info("step %d: final answer", step)
            return response.text

        for call in response.tool_calls:
            logger.info("step %d: %s(%s)", step, call["name"], call["args"])
            tool = tools_by_name.get(call["name"])
            try:
                if tool is None:
                    raise ValueError(f"Unknown tool {call['name']}")
                observation = str(tool.invoke(call["args"]))
            except Exception as e:
                observation = f"Error: {e}"
            logger.info("step %d: observation: %s", step, observation)
            messages.append(ToolMessage(observation, tool_call_id=call["id"]))

    logger.warning("stopped after %d steps without a final answer", max_steps)
    return "Sorry, I couldn't resolve this. A human agent will follow up."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(run_agent(get_llm(), TOOLS, sys.argv[1], settings.max_steps))
