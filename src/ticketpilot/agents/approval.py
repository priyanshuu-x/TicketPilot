from typing import Literal

from langchain_core.tools import BaseTool, tool
from langgraph.config import get_config
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from ticketpilot.mcp_client import call_tool, call_tool_json
from ticketpilot.mcp_server import APPROVAL_REQUIRED


class Review(BaseModel):
    """A human reviewer's answer to an approval request."""

    action: Literal["approve", "reject"]
    amount: float | None = Field(None, description="Approve a different (e.g. partial) amount")
    comment: str = ""


def guarded_refund_tool(tools_by_name: dict[str, BaseTool]) -> BaseTool:
    refund, record_approval = tools_by_name["issue_refund"], tools_by_name["record_approval"]

    @tool
    async def issue_refund(order_id: str, amount: float, reason: str) -> str:
        """Refund a delivered order. If the refund needs human approval, this waits for a
        reviewer to approve or reject it and returns their decision."""
        ticket_id = get_config()["configurable"]["ticket_id"]
        ticket = await call_tool_json(tools_by_name["get_ticket"], ticket_id=ticket_id)
        args = {"order_id": order_id, "reason": reason, "customer_id": ticket["customer_id"]}

        result = (await call_tool(refund, amount=amount, **args)).text
        if APPROVAL_REQUIRED not in result:
            return result

        request = {"type": "refund_approval", "order_id": order_id, "amount": amount}
        review = Review.model_validate(interrupt(request | {"reason": reason}))
        if review.action == "reject":
            return f"A human reviewer rejected this refund. Comment: {review.comment or 'none'}"

        approved = review.amount or amount
        approval = await call_tool_json(record_approval, order_id=order_id, amount=approved)
        refunded = await call_tool(
            refund, amount=approved, approval_id=approval["approval_id"], **args
        )
        return f"Approved by a human reviewer ({review.comment or 'no comment'}). {refunded.text}"

    return issue_refund
