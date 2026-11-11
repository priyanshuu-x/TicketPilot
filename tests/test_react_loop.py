from langchain_core.messages import AIMessage

from ticketpilot.demo_tools import TOOLS
from ticketpilot.react_loop import run_agent

from .fakes import call, fake_llm


def run(*responses, max_steps=5):
    return run_agent(fake_llm(*responses), TOOLS, "Where is my order?", max_steps)


def test_tool_call_then_final_answer():
    answer = run(call("get_order", order_id="A100"), AIMessage("Your earbuds were delivered."))
    assert answer == "Your earbuds were delivered."


def test_tool_error_is_fed_back_to_model(caplog):
    caplog.set_level("INFO")
    answer = run(call("get_order", order_id="Z999"), AIMessage("I couldn't find that order."))
    assert answer == "I couldn't find that order."
    assert "Error: Order Z999 not found" in caplog.text


def test_unknown_tool_does_not_crash():
    assert run(call("delete_database"), AIMessage("Done.")) == "Done."


def test_stops_at_max_steps():
    answer = run(*[call("search_policy", topic="refund")] * 3, max_steps=3)
    assert "human agent" in answer
