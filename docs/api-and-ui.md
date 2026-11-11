# API and UI

```
Streamlit (ui.py) ──HTTP──► FastAPI (api.py) ──► runs.py ──► LangGraph graph ──► MCP server
   :8501                       :8000                 ▲                │
                                                    cli.py             └─► Postgres (memory)
```

The UI only talks to the API, and the API owns the graph. `runs.py` holds the logic for
starting and continuing runs, so the CLI and the API behave the same.

## Endpoints

Interactive docs at http://127.0.0.1:8000/docs.

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | liveness check |
| GET | `/tickets` | all tickets, newest first, with `waiting_for_review` |
| POST | `/tickets` | open a ticket `{customer_email, subject, message}` (doesn't run it) |
| GET | `/tickets/{id}` | ticket, conversation, findings, tool calls, `pending_review`, `unfinished` |
| GET | `/tickets/{id}/history` | the run's checkpoints |
| POST | `/tickets/{id}/run` | run the team; `{"message": "..."}` for a customer follow-up |
| POST | `/tickets/{id}/review` | answer a paused refund `{action, amount?, comment}` |
| POST | `/tickets/{id}/resume` | continue a run that stopped midway |

`run`, `review` and `resume` **stream** their progress as NDJSON, one JSON object per line:

```
{"type": "step", "node": "investigator > agent", "text": "-> search_orders({'customer_id': 'C2'})"}
{"type": "paused", "request": {"type": "refund_approval", "order_id": "A103", "amount": 2499.0, ...}}
{"type": "done", "reply": "Hi Diya, ..."}
{"type": "error", "message": "..."}      (the checkpoint is kept, so the run can be resumed)
```

Why NDJSON instead of Server-Sent Events: it's the same idea with less ceremony, and any HTTP
client can read it line by line (`httpx.stream(...).iter_lines()`).

Errors use plain HTTP status codes: 404 for an unknown ticket or customer, and **409 Conflict**
when the ticket's state doesn't allow the request, e.g. running a ticket that is waiting for a
reviewer.

## Design decisions

- **The graph is built once at startup** (FastAPI lifespan), with one Postgres connection for
  checkpoints and memories. Requests share it; each ticket is its own thread.
- **`create_app(setup)`** takes the startup function as a parameter, so tests plug in a fake
  LLM and the MCP server in memory (`tests/test_api.py`). Nothing is mocked inside the app.
- **The UI is stateless.** Everything shown comes from the API, which reads the checkpointer,
  so a refreshed page, another browser, or the CLI all see the same ticket state.
- **Human in the loop over HTTP.** A paused run is just saved state. The approval panel appears
  whenever `pending_review` is set, and `POST /review` resumes the run, so a reviewer can answer
  hours later from any device.
