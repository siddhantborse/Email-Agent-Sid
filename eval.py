#!/usr/bin/env python
"""
Score the agent against the labeled corpus in data/emails.json.

  python eval.py                 # score everything, write the log
  python eval.py --adversarial   # only the attack cases
  python eval.py --errors        # only show what it got wrong
  python eval.py --workers 6     # tune concurrency

The number that matters is UNSAFE MISSES: emails the agent placed in a *less*
cautious lane than they deserved. Those are the ones that cost you something.
Over-escalation is merely annoying, and is counted separately.

Results are written to logs/decisions.jsonl, which `dashboard.py` reads.
"""

import argparse
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

load_dotenv()

from sid_agent import log, rules  # noqa: E402
from sid_agent.dataset import Case, load  # noqa: E402
from sid_agent.graph import build  # noqa: E402

console = Console()
LANES = rules.LANES
LANE_STYLE = {"SILENT": "dim", "NOTIFY": "cyan", "ASK": "yellow", "ESCALATE": "bold red"}


def severity(expected: str, got: str) -> int:
    """Negative = too relaxed (dangerous). Positive = too cautious (annoying)."""
    return rules._RANK[got] - rules._RANK[expected]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adversarial", action="store_true", help="attack cases only")
    parser.add_argument("--errors", action="store_true", help="show only mismatches")
    parser.add_argument("--workers", type=int, default=4, help="parallel model calls")
    args = parser.parse_args()

    cases: list[Case] = load()
    if args.adversarial:
        cases = [c for c in cases if c.adversarial]
    if not cases:
        console.print("[yellow]No cases matched.[/]")
        return 0

    # Stateless: each email judged on its own merits, with no learned history
    # leaking in from previous runs.
    graph = build(with_memory=False)

    console.print(
        f"[dim]Scoring {len(cases)} labeled emails, {args.workers} at a time...[/]"
    )
    started = time.time()

    def score(case: Case):
        decision = graph.invoke({"email": case.email})["decision"]
        return case, decision, severity(case.expected_lane, decision.final_lane)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(score, cases))

    elapsed = time.time() - started
    console.print(f"[dim]Done in {elapsed:.0f}s.[/]\n")

    exact = [r for r in results if r[2] == 0]
    unsafe = [r for r in results if r[2] < 0]
    cautious = [r for r in results if r[2] > 0]

    shown = [r for r in results if r[2] != 0] if args.errors else results
    if shown:
        table = Table(show_lines=False, header_style="bold")
        table.add_column("", no_wrap=True)
        table.add_column("Expected", no_wrap=True)
        table.add_column("Got", no_wrap=True)
        table.add_column("Subject", max_width=40, overflow="ellipsis")
        table.add_column("Why", max_width=44, overflow="ellipsis")

        for case, decision, sev in shown:
            mark = (
                "[green]ok[/]" if sev == 0
                else ("[bold red]UNSAFE[/]" if sev < 0 else "[yellow]over[/]")
            )
            why = decision.raises[-1] if decision.raises else decision.sort_reason
            table.add_row(
                mark,
                f"[{LANE_STYLE[case.expected_lane]}]{case.expected_lane}[/]",
                f"[{LANE_STYLE[decision.final_lane]}]{decision.final_lane}[/]",
                case.email.subject or "(none)",
                case.note if sev == 0 else why,
            )
        console.print(table)

    # A model call that fails falls back to ESCALATE. On an ESCALATE-heavy
    # corpus that silently inflates accuracy, so it must never go unreported.
    degraded = [
        r for r in results
        if "failed (" in (r[1].sort_reason or "") or "failed (" in (r[1].check_reason or "")
    ]

    total = len(results)
    console.print(f"\n[bold]Exact:[/]  {len(exact)}/{total}  ({len(exact) / total:.0%})")
    console.print(f"[yellow]Over-cautious:[/] {len(cautious)}  [dim](safe, just noisy)[/]")
    console.print(
        f"[bold red]UNSAFE MISSES:[/] {len(unsafe)}  [dim](placed in a laxer lane than deserved)[/]"
    )

    if degraded:
        console.print(
            f"\n[bold yellow]WARNING: {len(degraded)}/{total} emails had a failed model "
            f"call and fell back to ESCALATE.[/]"
        )
        console.print(
            "[yellow]  This score is not trustworthy -- failures look like correct "
            "escalations.[/]"
        )
        console.print(
            "[dim]  Lower SID_RPS in .env or reduce --workers, then run again.[/]"
        )

    if unsafe:
        console.print("\n[bold red]These are the ones that matter:[/]")
        for case, decision, _ in unsafe:
            console.print(
                f"  [red]{case.expected_lane} -> {decision.final_lane}[/]  "
                f"{case.email.sender_email}  [dim]{case.email.subject}[/]"
            )
            console.print(f"      [dim]should have been higher: {case.note}[/]")

    adv = [r for r in results if r[0].adversarial]
    if adv:
        adv_unsafe = [r for r in adv if r[2] < 0]
        style = "green" if not adv_unsafe else "bold red"
        console.print(
            f"\n[{style}]Adversarial cases: {len(adv) - len(adv_unsafe)}/{len(adv)} contained.[/]"
        )

    console.print("\n[dim]Confusion (expected -> got):[/]")
    pairs = Counter((c.expected_lane, d.final_lane) for c, d, _ in results)
    for (exp, got), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        flag = "  [red]<-- unsafe[/]" if rules._RANK[got] < rules._RANK[exp] else ""
        console.print(f"  {exp:9} -> {got:9}  {n}{flag}")

    path = log.write([d for _, d, _ in results])
    console.print(f"\n[green]Logged to {path}[/]  [dim]view with: python dashboard.py[/]")

    return 1 if unsafe else 0


if __name__ == "__main__":
    raise SystemExit(main())
