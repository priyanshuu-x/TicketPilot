from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from langgraph.store.postgres.base import PostgresIndexConfig

from ticketpilot.config import settings
from ticketpilot.embeddings import EMBED_DIMS, embed


@asynccontextmanager
async def open_memory() -> AsyncIterator[tuple[AsyncPostgresSaver, AsyncPostgresStore]]:
    index: PostgresIndexConfig = {"dims": EMBED_DIMS, "embed": embed, "fields": ["text"]}
    async with (
        AsyncPostgresSaver.from_conn_string(settings.database_url) as checkpointer,
        AsyncPostgresStore.from_conn_string(settings.database_url, index=index) as store,
    ):
        await checkpointer.setup()
        await store.setup()
        yield checkpointer, store


def thread_config(ticket_id: int) -> RunnableConfig:
    return {"configurable": {"thread_id": f"ticket-{ticket_id}", "ticket_id": ticket_id}}


def customer_namespace(customer_id: str) -> tuple[str, str]:
    return ("customers", customer_id)
