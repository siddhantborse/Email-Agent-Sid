#!/usr/bin/env python
"""
The human-in-the-loop lane and the calibration it feeds.

  python hitl.py --demo    # walk through how a pair earns autonomy (no API key needed)
  python hitl.py           # interactive: answer real ASK-lane emails

`--demo` exercises the calibration ledger directly, so it runs with no model and
no network. It shows the two things worth understanding: that trust is earned
slowly and lost instantly, and that the rules table bounds what can ever be
learned.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

load_dotenv()

from langgraph.store.memory import InMemoryStore  # noqa: E402
from langgraph.types import Command  # noqa: E402

from sid_agent import memory, rules  # noqa: E402
from sid_agent.dataset import load  # noqa: E402
from sid_agent.graph import build  # noqa: E402

console = Console()
USER = "sid"


def demo() -> int:
    store = InMemoryStore()
    sender = "priya@knowncompany.example"

    console.print("\n[bold]1. A scheduling request from a known colleague.[/]")
    console.print(
        f"   It lands in [yellow]ASK[/] the first time. "
        f"Floor for 'reply_scheduling' is [cyan]{rules.min_lane_for('reply_scheduling')}[/], "
        f"so there is one step of autonomy available to earn.\n"
    )

    table = Table(header_style="bold")
    table.add_column("You said", no_wrap=True)
    table.add_column("Streak", justify="right")
    table.add_column("Lane next time", no_wrap=True)
    table.add_column("", max_width=44, overflow="ellipsis")

    script = ["accepted"] * 4 + ["rejected"] + ["accepted"] * 5
    for outcome in script:
        rec = memory.record(store, USER, sender, "reply_scheduling", outcome)
        lane, why = memory.earned_lane(
            store, USER, sender, "reply_scheduling", "ASK"
        )
        style = "cyan" if lane == "NOTIFY" else "yellow"
        table.add_row(
            f"[{'green' if outcome == 'accepted' else 'red'}]{outcome}[/]",
            str(rec["streak"]),
            f"[{style}]{lane}[/]",
            (why.split(": ", 1)[1] if why else ""),
        )
    console.print(table)
    console.print(
        f"   [dim]Five clean accepts promote it. The single rejection at step 5 "
        f"reset the streak to zero -- slow to trust, instant to distrust.[/]\n"
    )

    console.print("[bold]2. What can never be learned.[/]\n")
    bounds = Table(header_style="bold")
    bounds.add_column("Action", no_wrap=True)
    bounds.add_column("Floor", no_wrap=True)
    bounds.add_column("After 50 accepts", no_wrap=True)
    bounds.add_column("", max_width=40, overflow="ellipsis")

    for action, note in [
        ("reply_scheduling", "promotable, one step"),
        ("forward", "floor is ASK - never promotable"),
        ("attach_file", "floor is ASK - never promotable"),
        ("send_money", "on the never-list"),
    ]:
        s2 = InMemoryStore()
        for _ in range(50):
            memory.record(s2, USER, sender, action, "accepted")
        start: rules.Lane = "ASK"
        lane, _ = memory.earned_lane(s2, USER, sender, action, start)
        moved = lane != start
        bounds.add_row(
            action,
            rules.min_lane_for(action),
            f"[{'cyan' if moved else 'yellow'}]{lane}[/]",
            note,
        )
    console.print(bounds)
    console.print(
        "   [dim]The rules table bounds the learning. The learning cannot "
        "rewrite the rules table.[/]\n"
    )

    console.print("[bold]3. Promotion is refused outright when anything is off.[/]\n")
    s3 = InMemoryStore()
    for _ in range(20):
        memory.record(s3, USER, sender, "reply_scheduling", "accepted")
    blocked, _ = memory.earned_lane(
        s3, USER, sender, "reply_scheduling", "ASK", blocked=True
    )
    console.print(
        f"   20 accepts, but the email carried masked content or agent-directed "
        f"text: [yellow]{blocked}[/] [dim](no promotion)[/]\n"
    )
    return 0


def interactive() -> int:
    """Run real emails and stop on anything that lands in ASK.

    Uses the persistent store, so what you teach it here is still there next
    time. `--earn` and `--demo` use a throwaway store so they stay reproducible.
    """
    store = memory.FileStore()
    graph = build(store=store)
    cases = load()

    console.print(f"[dim]Running {len(cases)} emails. Stopping on ASK.[/]\n")

    for case in cases:
        cfg = {
            "configurable": {
                "thread_id": f"hitl-{case.email.id}",
                "user_id": USER,
                "interactive": True,
            }
        }
        result = graph.invoke({"email": case.email}, cfg)

        if "__interrupt__" not in result:
            d = result["decision"]
            console.print(f"[dim]{d.final_lane:9} {case.email.subject[:60]}[/]")
            continue

        payload = result["__interrupt__"][0].value
        console.print(f"\n[yellow bold]ASK[/]  {payload['question']}")
        console.print(f"     proposed: [bold]{payload['proposed_action']}[/]")
        console.print(f"     why: {payload['why']}")
        for r in payload.get("raised", []):
            console.print(f"     [magenta]{r}[/]")

        answer = console.input("     [a]ccept / [e]dit / [r]eject / [q]uit > ").strip().lower()
        if answer.startswith("q"):
            break
        outcome = {"a": "accepted", "e": "edited", "r": "rejected"}.get(answer[:1], "rejected")

        graph.invoke(Command(resume={"outcome": outcome}), cfg)
        rec = memory.record(store, USER, case.email.sender_email, result["decision"].action, outcome)
        console.print(f"     [dim]recorded: {outcome}, streak now {rec['streak']}[/]")

    learned = memory.summary(store, USER)
    if learned:
        console.print("\n[bold]What it learned:[/]")
        for row in learned:
            console.print(
                f"  {row['sender']}  {row['action']}  "
                f"streak={row['streak']} accepts={row['accepts']} rejects={row['rejects']}"
            )
    return 0


def earn() -> int:
    """
    The same email, six times, through the real graph -- watch the lane change.

    The two model steps are deterministic at temperature 0, so they run once and
    are reused across rounds. Nothing about the email changes between rounds.
    The only thing that changes is what the user said last time.
    """
    store = InMemoryStore()
    graph = build(store=store)
    case = next(c for c in load() if c.email.id == "y01")

    console.print(
        f"\n[bold]{case.email.sender} <{case.email.sender_email}>[/]  "
        f"[dim]{case.email.subject}[/]"
    )
    console.print("[dim]Same email every round. You accept it each time.[/]\n")

    table = Table(header_style="bold")
    table.add_column("Round", justify="right", no_wrap=True)
    table.add_column("Streak", justify="right", no_wrap=True)
    table.add_column("Lane", no_wrap=True)
    table.add_column("What the agent would do", max_width=46, overflow="ellipsis")

    for rnd in range(1, 7):
        cfg = {"configurable": {"thread_id": f"earn-{rnd}", "user_id": USER}}
        state = graph.invoke({"email": case.email}, cfg)
        d = state["decision"]

        promoted = any("clean accepts" in r for r in d.raises)
        lane_style = "cyan" if d.final_lane == "NOTIFY" else "yellow"
        rec = memory.get(store, USER, case.email.sender_email, "reply_scheduling")

        table.add_row(
            str(rnd),
            str(rec["streak"]),
            f"[{lane_style}]{d.final_lane}[/]",
            ("[cyan]handles it, tells you after[/]" if promoted
             else state.get("outcome", "")),
        )

        # You accept whatever it proposed. That is the feedback.
        memory.record(store, USER, case.email.sender_email, "reply_scheduling", "accepted")

    console.print(table)
    console.print(
        "\n[dim]Five clean accepts, and it stops asking. It cannot go quieter than "
        f"NOTIFY for this action -- the floor for 'reply_scheduling' is "
        f"[cyan]{rules.min_lane_for('reply_scheduling')}[/].[/]\n"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="scripted, no API key needed")
    parser.add_argument("--earn", action="store_true", help="watch a lane change through the real graph")
    args = parser.parse_args()
    if args.demo:
        raise SystemExit(demo())
    raise SystemExit(earn() if args.earn else interactive())
