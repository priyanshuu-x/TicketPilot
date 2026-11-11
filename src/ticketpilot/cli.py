import argparse
import asyncio

from ticketpilot.agents.graph import build_support_graph
from ticketpilot.mcp_client import get_mcp_tools
from ticketpilot.memory import customer_namespace, open_memory, thread_config
from ticketpilot.runs import RunError, events, prepare


async def ticket_command(args: argparse.Namespace) -> None:
    async with open_memory() as (checkpointer, store):
        graph = build_support_graph(await get_mcp_tools(), store, checkpointer)
        config = thread_config(args.ticket_id)

        if args.command == "history":
            async for snapshot in graph.aget_state_history(config):
                next_node = snapshot.next[0] if snapshot.next else "(done)"
                step = snapshot.metadata["step"] if snapshot.metadata else "?"
                print(f"step {step:>3}  {snapshot.created_at}  next: {next_node}")
            return

        review = None
        if args.command == "review":
            review = {"action": args.action, "amount": args.amount, "comment": args.comment}
        try:
            graph_input = await prepare(
                graph, args.ticket_id, args.command, getattr(args, "message", None), review
            )
        except RunError as error:
            print(error)
            return
        async for event in events(graph, graph_input, config):
            if event["type"] == "step":
                print(f"[{event['node']}] {event['text']}".replace("\n", " ")[:300])
            elif event["type"] == "paused":
                print(f"\nPAUSED, waiting for a human reviewer: {event['request']}")
                print("Answer with: cli review <ticket> approve [--amount N] [-c ...] | reject")


async def memories(customer_id: str) -> None:
    async with open_memory() as (_, store):
        for item in await store.asearch(customer_namespace(customer_id), limit=20):
            print(f"[{item.key}] {item.value['text']}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="ticketpilot")
    commands = parser.add_subparsers(dest="command", required=True)
    run_cmd = commands.add_parser("run", help="run the team on a ticket")
    run_cmd.add_argument("ticket_id", type=int)
    run_cmd.add_argument("-m", "--message", help="a follow-up message from the customer")
    review_cmd = commands.add_parser("review", help="approve or reject a paused refund")
    review_cmd.add_argument("ticket_id", type=int)
    review_cmd.add_argument("action", choices=["approve", "reject"])
    review_cmd.add_argument("--amount", type=float, help="approve a different amount")
    review_cmd.add_argument("-c", "--comment", default="", help="note from the reviewer")
    for name, help_text in [("resume", "continue a crashed run"), ("history", "show checkpoints")]:
        commands.add_parser(name, help=help_text).add_argument("ticket_id", type=int)
    commands.add_parser("memories", help="show a customer's memories").add_argument("customer_id")
    args = parser.parse_args()

    coro = memories(args.customer_id) if args.command == "memories" else ticket_command(args)
    asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
