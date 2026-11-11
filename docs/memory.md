# Memory

Code: [`memory.py`](../src/ticketpilot/memory.py), nodes in [`agents/graph.py`](../src/ticketpilot/agents/graph.py).

| Kind | What it holds | Where | Lifetime |
|---|---|---|---|
| Working state | plan, findings, intent, handoffs | `SupportState` | one ticket |
| Short-term memory | the whole state after every node | Postgres checkpointer, thread `ticket-<id>` | one ticket, all turns |
| Context trimming | summary of older messages | `summary` field, `compact` node | one ticket |
| Long-term memory | one summary per past ticket | Postgres store + pgvector, namespace `("customers", <id>)` | forever, per customer |
| Knowledge (RAG) | policy rules | MCP server, `search_kb` (local embeddings) | static |

## Short-term: the checkpointer

`graph.compile(checkpointer=...)` makes LangGraph save the state after **every node**, keyed by
`thread_id`. Each ticket is one thread, which gives three things:

- **Multi-turn conversations.** A follow-up (`cli run 4 -m "..."`) continues from the saved
  state: the supervisor sees the earlier findings and can skip straight to the resolver.
- **History.** `cli history 4` lists every checkpoint: which node ran and when.
- **Recovery** (Phase 6). A crashed run resumes from its last checkpoint instead of starting over.

## Context trimming

The conversation (`messages`) grows with every turn. When it has more than `MAX_MESSAGES`, the
`compact` node summarizes all but the last 2 into `summary` and deletes them with
`RemoveMessage`. Agents read `summary + recent messages`, so the prompt size stays bounded.

Specialists never see each other's tool calls: each one gets `ticket_context()` (conversation,
memories, findings) and returns a single summary. That is the second, structural way context
stays small.

## Long-term: the store

- **Write:** `remember` asks the model for a one-or-two sentence summary of the case and saves it
  under the customer's id (key `ticket-<id>`, rewritten on each turn).
- **Read:** `recall` does a **semantic search** (pgvector, cosine similarity) over the
  customer's past tickets using their latest message, so the team knows e.g.
  "this customer already got a refund for A100 last week".
- Embeddings come from `fastembed` (BAAI/bge-small-en-v1.5, 384 dims), running locally with no
  API calls and no rate limits.

## Knowledge base (RAG)

`search_kb` splits the policy files into one chunk per rule, embeds them once, and returns the
top-k rules by cosine similarity, each with its policy name and score. For 4 small documents an
in-memory numpy index is enough; a real knowledge base would live in pgvector like the memories.

## Reset everything

```bash
uv run python -m ticketpilot.mcp_server.db   # business data
docker compose down -v                       # wipes Postgres: checkpoints + memories
docker compose up -d postgres
```
