# Human in the loop and recovery

Code: [`agents/approval.py`](../src/ticketpilot/agents/approval.py),
[`cli.py`](../src/ticketpilot/cli.py), `issue_refund` / `record_approval` in the MCP server.

## The approval flow

```
resolver ── issue_refund(A103, 2499) ──► guarded tool ── try refund ──► MCP server
                                              │                          "needs human approval"
                                              ▼
                                     interrupt({order, amount, reason})
                                     graph pauses, state saved in Postgres
                                              ⋮   (minutes or days later, any process)
reviewer: cli review 3 approve [--amount N] | reject -c "why"
                                              ▼
                              Command(resume={"action": ..., ...})
              approve → record_approval → refund with the one-time approval_id
              reject  → the resolver gets "rejected: <comment>" as the tool result
```

Design decisions:

- **Pause at the consequential action, in code.** The LLM can't skip the check: the resolver
  only has the guarded tool, and the MCP server rejects risky refunds without a valid approval.
- **One source of truth for the rules.** The guarded tool doesn't re-implement "above Rs 2000"
  or "second refund in 30 days". It tries the refund, and the server says whether it needs approval.
- **Approvals are single-use and bound to an order and amount.** An approval for Rs 1500 can't
  be reused or used for Rs 2499. No agent has `record_approval`; only code calls it after a
  person decides (in production that tool would also need auth).
- **Edit = approve a different amount** (e.g. a partial refund). **Reject** goes back to the
  resolver with the reviewer's comment, which then updates the ticket and explains to the customer.
- **Asynchronous by design.** `interrupt()` doesn't block a process: the run ends, the state sits
  in Postgres, and `review` (later, from the API or CLI) resumes it. On resume, LangGraph re-runs
  the interrupted node and `interrupt()` returns the reviewer's answer.

## Recovery

| Failure | What happens |
|---|---|
| LLM error / rate limit | 2 retries with backoff, then the fallback model (`llm.py`) |
| Tool error | Returned to the agent as an observation; it can correct itself |
| Transient MCP/DB error in `start`/`recall` | Node `RetryPolicy`, 3 attempts |
| Agent going in circles | `MAX_STEPS` per agent → give up; `MAX_HANDOFFS` → escalate |
| Process crash mid-run | `cli resume <ticket>` continues from the last checkpoint |
| Risky action | Paused for a human (above) |

Checked live: a run killed right after the investigator finished was refused by `run`
("stopped midway") and finished by `resume`, without repeating the investigator's model or tool
calls, and with exactly one refund issued.
