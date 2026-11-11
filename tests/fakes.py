from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from ticketpilot.agents.guardrails import ReplyCheck

CRASH = AIMessage("CRASH")


class FakeLLM(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        result = super()._generate(*args, **kwargs)
        if result.generations[0].message.text == CRASH.text:
            raise RuntimeError("Simulated crash")
        return result

    def with_structured_output(self, schema, **kwargs):
        return RunnableLambda(lambda _: next(self.messages))


def fake_llm(*responses):
    return FakeLLM(messages=iter(responses))


def grounded():
    return ReplyCheck(grounded=True, problem="", corrected_reply="")


def call(name, **args):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": name}])
