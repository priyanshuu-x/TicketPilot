# The agent loop, from first principles

An "agent" is an LLM that decides its own next step. The code around it is a loop,
written by hand in [`react_loop.py`](../src/ticketpilot/react_loop.py).

## ReAct: think → act → observe

```
messages = [system prompt, customer question]
repeat up to max_steps:
    response = llm(messages, tool schemas)        # THINK
    if response has no tool calls: return it      # final answer
    for each tool call:
        result = run the tool (or catch its error)  # ACT
        messages += ToolMessage(result)             # OBSERVE
give up and hand over to a human
```

- **Tools** are plain functions. `@tool` turns each one's name, type hints and docstring into a
  JSON schema. `bind_tools` sends those schemas to the model with every request.
- **The model never runs code.** It only returns *requests* (`tool_calls`). Our loop runs them.
- **State** is just the `messages` list. The model has no memory: everything it "knows" about this
  ticket is in that list, re-sent on every step.
- **Stopping conditions:** a reply with no tool calls means the model is done; `max_steps` caps cost
  and stops infinite loops.
- **Recovery:** a tool error becomes an observation (`"Error: Order Z999 not found"`), so the model
  can correct itself instead of the program crashing.

## Why a while loop is not enough

This loop works, but a production agent also needs things it doesn't have:

| Missing | What breaks | LangGraph feature (later phases) |
|---|---|---|
| Persistent state | A crash loses the whole conversation | Checkpointer saves state after every step |
| Pausing for a human | Can't wait hours for approval inside a `for` loop | `interrupt()` + resume from checkpoint |
| More than one agent | One prompt does everything badly | Graph of nodes with conditional routing |
| Structured state | Plan, customer, decision all buried in messages | Typed `State` with explicit fields |
| Visibility | Only log lines | Every step traced in LangSmith |

LangGraph runs the same think → act → observe cycle. It just makes each step a node in a graph
and saves the state between nodes.
