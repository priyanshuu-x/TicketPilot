import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from ticketpilot.agents.approval import Review
from ticketpilot.agents.graph import build_support_graph
from ticketpilot.config import settings
from ticketpilot.mcp_client import ToolError, call_tool_json, get_mcp_tools
from ticketpilot.memory import open_memory, thread_config
from ticketpilot.runs import Graph, RunError, events, prepare

Setup = Callable[[], AbstractAsyncContextManager[tuple[Graph, dict[str, BaseTool]]]]


class NewTicket(BaseModel):
    customer_email: str
    subject: str
    message: str


class FollowUp(BaseModel):
    message: str | None = None


@asynccontextmanager
async def default_setup() -> AsyncIterator[tuple[Graph, dict[str, BaseTool]]]:
    async with open_memory() as (checkpointer, store):
        tools = await get_mcp_tools()
        yield build_support_graph(tools, store, checkpointer), {t.name: t for t in tools}


router = APIRouter()


async def mcp_call(request: Request, tool: str, **args: Any) -> Any:
    try:
        return await call_tool_json(request.app.state.tools[tool], **args)
    except ToolError as error:
        raise HTTPException(404, str(error)) from None


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/tickets")
async def list_tickets(request: Request) -> list[dict[str, Any]]:
    tickets: list[dict[str, Any]] = await mcp_call(request, "list_tickets")
    for ticket in tickets:
        state = await request.app.state.graph.aget_state(thread_config(ticket["id"]))
        ticket["waiting_for_review"] = bool(state.interrupts)
    return tickets


@router.post("/tickets", status_code=201)
async def create_ticket(request: Request, new: NewTicket) -> dict[str, Any]:
    ticket: dict[str, Any] = await mcp_call(
        request,
        "create_ticket",
        customer_email=new.customer_email,
        subject=new.subject,
        body=new.message,
    )
    return ticket


@router.get("/tickets/{ticket_id}")
async def get_ticket(request: Request, ticket_id: int) -> dict[str, Any]:
    ticket = await mcp_call(request, "get_ticket", ticket_id=ticket_id)
    state = await request.app.state.graph.aget_state(thread_config(ticket_id))
    values = state.values
    return {
        "ticket": ticket,
        "conversation": [
            {"role": "customer" if isinstance(m, HumanMessage) else "support", "text": m.text}
            for m in values.get("messages", [])
        ],
        "summary": values.get("summary"),
        "intent": values.get("intent"),
        "findings": values.get("findings", {}),
        "tool_log": values.get("tool_log", []),
        "pending_review": state.interrupts[0].value if state.interrupts else None,
        "unfinished": bool(state.next) and not state.interrupts,
    }


@router.get("/tickets/{ticket_id}/history")
async def history(request: Request, ticket_id: int) -> list[dict[str, Any]]:
    graph: Graph = request.app.state.graph
    return [
        {
            "step": snapshot.metadata["step"] if snapshot.metadata else None,
            "created_at": snapshot.created_at,
            "next": list(snapshot.next),
        }
        async for snapshot in graph.aget_state_history(thread_config(ticket_id))
    ]


async def stream_run(
    request: Request,
    ticket_id: int,
    kind: Literal["run", "review", "resume"],
    message: str | None = None,
    review: dict[str, Any] | None = None,
) -> StreamingResponse:
    graph: Graph = request.app.state.graph
    await mcp_call(request, "get_ticket", ticket_id=ticket_id)
    try:
        graph_input = await prepare(graph, ticket_id, kind, message, review)
    except RunError as error:
        raise HTTPException(409, str(error)) from None

    async def lines() -> AsyncIterator[str]:
        try:
            async for event in events(graph, graph_input, thread_config(ticket_id)):
                yield json.dumps(event, default=str) + "\n"
        except Exception as error:
            yield json.dumps({"type": "error", "message": str(error)}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@router.post("/tickets/{ticket_id}/run")
async def run(request: Request, ticket_id: int, follow_up: FollowUp) -> StreamingResponse:
    return await stream_run(request, ticket_id, "run", message=follow_up.message)


@router.post("/tickets/{ticket_id}/review")
async def review(request: Request, ticket_id: int, decision: Review) -> StreamingResponse:
    return await stream_run(request, ticket_id, "review", review=decision.model_dump())


@router.post("/tickets/{ticket_id}/resume")
async def resume(request: Request, ticket_id: int) -> StreamingResponse:
    return await stream_run(request, ticket_id, "resume")


def create_app(setup: Setup = default_setup) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with setup() as (graph, tools):
            app.state.graph, app.state.tools = graph, tools
            yield

    app = FastAPI(title="TicketPilot", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()

if __name__ == "__main__":
    server = uvicorn.Server(uvicorn.Config(app, host=settings.api_host, port=settings.api_port))
    asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
