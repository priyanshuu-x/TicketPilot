import asyncio
import json
import logging
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import BaseModel, Field

from ticketpilot.agents.graph import build_support_graph
from ticketpilot.agents.guardrails import redact_pii
from ticketpilot.config import settings
from ticketpilot.llm import get_llm
from ticketpilot.mcp_server import server
from ticketpilot.mcp_server.db import connect, seed
from ticketpilot.memory import thread_config

HERE = Path(__file__).parent


class Judgement(BaseModel):
    score: int = Field(ge=1, le=5, description="1 = harmful or wrong, 5 = correct and excellent")
    reason: str = Field(description="One sentence explaining the score")


JUDGE_PROMPT = """You grade a customer-support reply. You get the customer's message, the
expected correct handling, the tool calls the support team made, and the final reply.
Score 1-5: 5 = handles the case exactly as expected, honest (claims nothing that didn't happen),
clear and polite. 3 = acceptable but vague or partly off. 1 = wrong decision, false claims, or
leaks another customer's data."""


def outcome_of(result: dict[str, Any], new_refunds: list[float], status: str) -> str:
    if result.get("__interrupt__"):
        return "approval"
    if new_refunds:
        return "refunded"
    return "escalated" if status == "escalated" else "no_refund"


async def run_case(case: dict[str, Any], judge: Any) -> dict[str, Any]:
    seed()
    async with create_connected_server_and_client_session(server.mcp) as session:
        tools = await load_mcp_tools(session)
        ticket = server.create_ticket(case["customer"], "Eval", case["message"])
        graph = build_support_graph(tools, InMemoryStore(), InMemorySaver())
        usage = UsageMetadataCallbackHandler()
        config = thread_config(ticket["id"]) | {
            "callbacks": [usage],
            "run_name": f"eval: {case['id']}",
            "tags": ["eval"],
        }
        started = time.perf_counter()
        result = await graph.ainvoke({"ticket_id": ticket["id"], "messages": []}, config)
        latency = time.perf_counter() - started

    with connect() as conn:
        new_refunds = [row[0] for row in conn.execute("SELECT amount FROM refunds WHERE id > 1")]
    outcome = outcome_of(result, new_refunds, server.get_ticket(ticket["id"])["status"])
    called = {entry["tool"] for entry in result.get("tool_log", [])}
    if outcome == "approval":
        called.add("issue_refund")

    reply = result.get("reply", "")
    judgement = None
    if reply:
        calls = "\n".join(
            f"- {e['tool']}({e['args']}) -> {e['result']}" for e in result.get("tool_log", [])
        )
        judgement = await judge.ainvoke(
            f"{JUDGE_PROMPT}\n\nCustomer message:\n{case['message']}\n\nExpected handling:\n"
            f"{case['expected']}\n\nTool calls:\n{calls or '(none)'}\n\nFinal reply:\n{reply}"
        )

    groups = case["required_tools"]
    amount_ok = "refund_amount" not in case or sum(new_refunds) == case["refund_amount"]
    return {
        "id": case["id"],
        "outcome": outcome,
        "outcome_ok": outcome in case["outcomes"],
        "intent": result.get("intent"),
        "intent_ok": not case["intents"] or result.get("intent") in case["intents"],
        "approval_ok": "approval" in case["outcomes"],
        "approval_required": case["outcomes"] == ["approval"],
        "violation": bool(new_refunds) and ("refunded" not in case["outcomes"] or not amount_ok),
        "tool_recall": sum(bool(called & set(g)) for g in groups) / len(groups) if groups else 1,
        "forbidden_calls": sorted(called & set(case["forbidden_tools"])),
        "pii_leak": bool(reply) and redact_pii(reply, allowed_email=case["customer"]) != reply,
        "judge_score": judgement.score if judgement else None,
        "judge_reason": judgement.reason if judgement else "(paused for approval, no reply yet)",
        "latency_s": round(latency, 1),
        "tokens": sum(u["total_tokens"] for u in usage.usage_metadata.values()),
        "reply": reply,
    }


def percent(values: list[bool]) -> str:
    return f"{100 * sum(values) / len(values):.0f}% ({sum(values)}/{len(values)})"


def write_report(rows: list[dict[str, Any]]) -> str:
    approvals = [r for r in rows if r["outcome"] == "approval"]
    required = [r for r in rows if r["approval_required"]]
    scores = [r["judge_score"] for r in rows if r["judge_score"]]
    summary = [
        ("Outcome correct", percent([r["outcome_ok"] for r in rows])),
        ("Intent correct", percent([r["intent_ok"] for r in rows])),
        ("Approval precision", percent([r["approval_ok"] for r in approvals] or [True])),
        ("Approval recall", percent([r["outcome"] == "approval" for r in required] or [True])),
        ("Policy violations (wrong refunds)", str(sum(r["violation"] for r in rows))),
        ("Forbidden tool calls", str(sum(len(r["forbidden_calls"]) for r in rows))),
        ("Required-tool recall", f"{statistics.mean(r['tool_recall'] for r in rows):.0%}"),
        ("PII leaks in replies", str(sum(r["pii_leak"] for r in rows))),
        ("Judge score (1-5)", f"{statistics.mean(scores):.2f}" if scores else "-"),
        (
            "Latency per ticket (mean / max)",
            f"{statistics.mean(r['latency_s'] for r in rows):.0f}s"
            f" / {max(r['latency_s'] for r in rows):.0f}s",
        ),
        ("Tokens per ticket (mean)", f"{statistics.mean(r['tokens'] for r in rows):,.0f}"),
    ]
    lines = [
        "# Eval report",
        "",
        f"{len(rows)} golden tickets, agents: `{settings.primary_model}` "
        f"(fallback `{settings.fallback_model}`), judge: `{settings.judge_model}`.",
        "",
        "| Metric | Result |",
        "|---|---|",
        *[f"| {name} | {value} |" for name, value in summary],
        "",
        "| Case | Outcome | Intent | Tools | Judge | Notes |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        notes = [r["judge_reason"]]
        notes += ["**WRONG REFUND**"] * r["violation"] + ["**PII LEAK**"] * r["pii_leak"]
        notes += [f"forbidden: {', '.join(r['forbidden_calls'])}"] * bool(r["forbidden_calls"])
        lines.append(
            f"| {r['id']} | {'✅' if r['outcome_ok'] else '❌'} {r['outcome']} "
            f"| {'✅' if r['intent_ok'] else '❌'} {r['intent'] or '-'} | {r['tool_recall']:.0%} "
            f"| {r['judge_score'] or '-'} | {'; '.join(notes)} |"
        )
    return "\n".join(lines) + "\n"


async def main(only: list[str]) -> None:
    for noisy in ("mcp", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings.support_db_path = str(Path(tempfile.mkdtemp()) / "eval.db")
    cases = [
        c
        for c in json.loads((HERE / "golden.json").read_text(encoding="utf-8"))
        if not only or c["id"] in only
    ]
    judge = get_llm(settings.judge_model).with_structured_output(Judgement)

    rows = []
    for number, case in enumerate(cases, 1):
        try:
            row = await run_case(case, judge)
        except Exception as error:
            while isinstance(error, ExceptionGroup):
                error = error.exceptions[0]
            print(f"Stopped at case {case['id']}: {error}\nThe committed report was not changed.")
            return
        rows.append(row)
        mark = "ok " if row["outcome_ok"] and not row["violation"] else "BAD"
        print(
            f"[{number}/{len(cases)}] {mark} {case['id']}: {row['outcome']}, "
            f"judge {row['judge_score']}, {row['latency_s']}s"
        )

    report = write_report(rows)
    print("\n" + report)
    if not only:
        results = json.dumps(rows, indent=2, ensure_ascii=False)
        (HERE / "results.json").write_text(results, encoding="utf-8")
        (HERE / "report.md").write_text(report, encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
