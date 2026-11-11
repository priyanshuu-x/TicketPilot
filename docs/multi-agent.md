# Multi-agent design

Code: [`agents/graph.py`](../src/ticketpilot/agents/graph.py).

```
START → load_ticket → supervisor ─► investigator ───┐
                       ▲   │    ├─► policy_advisor ─┤ (back to supervisor)
                       └───┼────┼───────────────────┘
                           │    ├─► resolver ─► END
                           │    └─► escalate ─► END
```

## Roles

| Node | Kind | Tools | Output |
|---|---|---|---|
| `load_ticket` | plain code | `get_ticket` | customer email + message |
| `supervisor` | LLM, structured output | none | `intent`, `priority`, `plan` |
| `investigator` | ReAct agent | `lookup_customer`, `search_orders`, `get_order` | facts |
| `policy_advisor` | ReAct agent | `search_kb` | decision + whether approval is needed |
| `resolver` | ReAct agent | `issue_refund`, `update_ticket` | actions + customer reply |
| `escalate` | plain code | `update_ticket` | hand-off to a human |

## Design decisions

- **Supervisor with a re-planned list.** After each specialist the supervisor sees the new
  findings and returns the *remaining* plan. The plan is explicit state you can inspect, and it
  can change (e.g. skip the policy advisor for "where is my order?").
- **Structured output for routing.** The supervisor returns a Pydantic `Decision`, so routing is
  a field lookup, not parsing free text.
- **Least privilege.** Each specialist only gets the tools for its job. Only the resolver can move
  money, and the MCP server still enforces the refund rules itself.
- **Shared, explicit state instead of a shared chat.** Specialists don't see each other's tool
  calls, only `findings` (one summary per specialist). Each agent's context stays small and
  focused, which matters on free-tier models.
- **Facts from code, not the model.** The ticket id comes from graph state, so the resolver
  updates the right ticket. In Phase 3 the single agent invented `ticket_id=0`.
- **Stopping conditions:** the resolver always ends the run (every ticket gets a reply), the
  supervisor is limited to `MAX_HANDOFFS` decisions (then `escalate`), and each specialist is
  limited to `MAX_STEPS` model calls.

## Found in live runs

Ticket 4 (Kabir's second refund within 30 days): the investigator first only checked the ticket's
order, so the policy advisor approved the refund without review. The **MCP server refused it**
and the resolver escalated, so defense in depth worked. Fix: `search_orders` now returns
refunds and the investigator reports all refunds of the last 30 days. After the fix the policy
advisor flags it for approval itself.
