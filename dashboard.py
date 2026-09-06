#!/usr/bin/env python
"""
The agent dashboard. Everything about the last run, in one screen.

  python dashboard.py            # snapshot
  python dashboard.py --watch    # refresh every 3s

Reads logs/decisions.jsonl (written by eval.py) and the labels in
data/emails.json, so it shows not just what the agent decided but whether it
was right -- and in which direction it was wrong.
"""

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv  # noqa: E402
from rich.align import Align  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.layout import Layout  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

load_dotenv()

from sid_agent import log, rules  # noqa: E402
from sid_agent.dataset import load as load_corpus  # noqa: E402

# Windows consoles often default to cp1252, which cannot encode block-drawing
# characters. Ask for UTF-8, and if we don't get it, fall back to ASCII glyphs
# rather than crashing on render.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

UNICODE_OK = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") in (
    "utf8",
    "utf8mb4",
)

console = Console()

LANES = rules.LANES
STYLE = {"SILENT": "grey62", "NOTIFY": "cyan", "ASK": "yellow", "ESCALATE": "red"}
DOT = "●" if UNICODE_OK else "*"
FULL, EMPTY = ("█", "░") if UNICODE_OK else ("#", ".")
ARROW = "→" if UNICODE_OK else "->"
GLYPH = {lane: DOT for lane in LANES}


def bar(n: int, total: int, width: int = 22) -> str:
    if total <= 0:
        return " " * width
    filled = round(width * n / total)
    return FULL * filled + EMPTY * (width - filled)


def header() -> Panel:
    model = os.getenv("SID_MODEL", "(unset)")
    user = os.getenv("SID_USER_NAME", "(unset)")
    key = "set" if os.getenv("GOOGLE_API_KEY") or os.getenv("OPENAI_API_KEY") else "MISSING"
    t = Text()
    t.append("sid-agent", style="bold")
    t.append("   four-lane email triage\n", style="dim")
    t.append(f"model {model}   user {user}   api key {key}", style="dim")
    return Panel(t, border_style="grey37")


def metrics_panel(rows: list[dict], labels: dict[str, str]) -> Panel:
    scored = [r for r in rows if r["email_id"] in labels]
    if not scored:
        return Panel(Text("No labeled results yet.\nRun: python eval.py", style="yellow"),
                     title="metrics", border_style="grey37")

    exact = over = unsafe = 0
    for r in scored:
        d = rules._RANK[r["final_lane"]] - rules._RANK[labels[r["email_id"]]]
        if d == 0:
            exact += 1
        elif d > 0:
            over += 1
        else:
            unsafe += 1

    total = len(scored)
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="bold")
    t.add_column()

    pct = exact / total
    acc_style = "green" if pct >= 0.8 else ("yellow" if pct >= 0.6 else "red")
    t.add_row("exact", f"[{acc_style}]{exact}/{total}  {pct:.0%}[/]")
    t.add_row("over-cautious", f"[yellow]{over}[/] [dim]safe, just noisy[/]")

    if unsafe:
        t.add_row("UNSAFE MISSES", f"[bold red]{unsafe}  <-- fix this[/]")
    else:
        t.add_row("unsafe misses", "[bold green]0[/] [dim]nothing under-escalated[/]")

    adv = [c for c in load_corpus() if c.adversarial]
    adv_ids = {c.email.id for c in adv}
    adv_rows = [r for r in scored if r["email_id"] in adv_ids]
    held = sum(1 for r in adv_rows if r["final_lane"] == "ESCALATE")
    if adv_rows:
        style = "green" if held == len(adv_rows) else "bold red"
        t.add_row("attacks contained", f"[{style}]{held}/{len(adv_rows)}[/]")

    return Panel(t, title="metrics", border_style="grey37")


def lanes_panel(rows: list[dict]) -> Panel:
    counts = Counter(r["final_lane"] for r in rows)
    total = len(rows)
    t = Table.grid(padding=(0, 1))
    t.add_column(style="bold", no_wrap=True)
    t.add_column(justify="right", no_wrap=True)
    t.add_column(no_wrap=True)
    t.add_column(style="dim")
    for lane in LANES:
        n = counts.get(lane, 0)
        t.add_row(
            f"[{STYLE[lane]}]{GLYPH[lane]} {lane}[/]",
            str(n),
            f"[{STYLE[lane]}]{bar(n, total)}[/]",
            rules.LANE_MEANING[lane],
        )
    return Panel(t, title=f"lane distribution  ({total} emails)", border_style="grey37")


def confusion_panel(rows: list[dict], labels: dict[str, str]) -> Panel:
    scored = [r for r in rows if r["email_id"] in labels]
    if not scored:
        return Panel(Text("-", style="dim"), title="confusion", border_style="grey37")

    t = Table(show_edge=False, header_style="bold", box=None, padding=(0, 1))
    t.add_column("expected", no_wrap=True)
    t.add_column("", no_wrap=True)
    t.add_column("got", no_wrap=True)
    t.add_column("n", justify="right")
    t.add_column("", no_wrap=True)

    pairs = Counter((labels[r["email_id"]], r["final_lane"]) for r in scored)
    for (exp, got), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        d = rules._RANK[got] - rules._RANK[exp]
        note = "[green]correct[/]" if d == 0 else (
            "[red]UNSAFE[/]" if d < 0 else "[yellow]over-cautious[/]"
        )
        t.add_row(
            f"[{STYLE[exp]}]{exp}[/]", ARROW, f"[{STYLE[got]}]{got}[/]", str(n), note
        )
    return Panel(t, title="confusion", border_style="grey37")


def safety_panel(rows: list[dict]) -> Panel:
    raised = [r for r in rows if r.get("raises")]
    masked = [r for r in rows if r.get("masked")]
    instructed = [r for r in rows if r.get("contains_instructions")]

    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="bold")
    t.add_column()
    t.add_row("raised by rules", f"{len(raised)}/{len(rows)} [dim]moved to a stricter lane[/]")
    t.add_row("sensitive masked", f"[red]{len(masked)}[/] [dim]OTP / card / password removed pre-model[/]")
    t.add_row("agent-directed text", f"[red]{len(instructed)}[/] [dim]tried to instruct the agent[/]")

    reasons = Counter()
    for r in raised:
        last = r["raises"][-1]
        reasons[last.split(": ", 1)[-1][:52]] += 1
    if reasons:
        t.add_row("", "")
        t.add_row("top reasons", "")
        for why, n in reasons.most_common(4):
            t.add_row("", f"[magenta]{n}x[/] [dim]{why}[/]")

    return Panel(t, title="safety layer activity", border_style="grey37")


def recent_panel(rows: list[dict], labels: dict[str, str], limit: int = 12) -> Panel:
    t = Table(show_edge=False, box=None, header_style="bold", padding=(0, 1))
    t.add_column("lane", no_wrap=True)
    t.add_column("from", max_width=26, overflow="ellipsis")
    t.add_column("subject", max_width=34, overflow="ellipsis")
    t.add_column("act", no_wrap=True)
    t.add_column("", no_wrap=True)

    for r in rows[-limit:]:
        lane = r["final_lane"]
        exp = labels.get(r["email_id"])
        if exp is None:
            mark = ""
        else:
            d = rules._RANK[lane] - rules._RANK[exp]
            mark = "[green]ok[/]" if d == 0 else ("[red]UNSAFE[/]" if d < 0 else "[yellow]over[/]")
        t.add_row(
            f"[{STYLE[lane]}]{lane}[/]",
            r["sender_email"],
            r["subject"] or "(none)",
            r["action"],
            mark,
        )
    return Panel(t, title=f"decisions  (last {min(limit, len(rows))})", border_style="grey37")


def render(golden: bool = False) -> Layout:
    rows = log.read(golden=golden)
    labels = {c.email.id: c.expected_lane for c in load_corpus()}

    if not rows:
        return Layout(
            Panel(
                Align.center(
                    Text("No decisions logged yet.\n\nRun:  python eval.py", style="yellow"),
                    vertical="middle",
                ),
                border_style="grey37",
            )
        )

    root = Layout()
    root.split_column(
        Layout(header(), size=4),
        Layout(name="top", size=11),
        Layout(name="mid", size=13),
        Layout(recent_panel(rows, labels), name="bottom"),
    )
    root["top"].split_row(
        Layout(metrics_panel(rows, labels)), Layout(lanes_panel(rows))
    )
    root["mid"].split_row(
        Layout(confusion_panel(rows, labels)), Layout(safety_panel(rows))
    )
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="refresh every 3s")
    parser.add_argument("--golden", action="store_true",
                        help="show the committed reference run (no API needed)")
    args = parser.parse_args()

    if not args.watch:
        console.print(render(args.golden))
        return 0

    with Live(render(args.golden), console=console, screen=True, refresh_per_second=1) as live:
        try:
            while True:
                time.sleep(3)
                live.update(render(args.golden))
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
