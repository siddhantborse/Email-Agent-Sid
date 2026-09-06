#!/usr/bin/env python
"""
Four worked examples, one per lane, with every step shown.

  python examples.py

Runs one representative email through the whole pipeline and prints what each
step saw and decided: the raw email, what masking removed, what the model
suggested, what the second check said, and how the rules settled it.

Writes logs/examples.json so the dashboard and write-ups can use the same data
rather than anyone retyping it.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402

load_dotenv()

from sid_agent.dataset import load  # noqa: E402
from sid_agent.graph import build  # noqa: E402

console = Console()

# One email per lane. x01 is the injection attempt -- the interesting one.
PICKS = {
    "SILENT": "n01",
    "NOTIFY": "y01",
    "ASK": "a01",
    "ESCALATE": "x01",
}

LANE_STYLE = {"SILENT": "dim", "NOTIFY": "cyan", "ASK": "yellow", "ESCALATE": "bold red"}


def main() -> int:
    cases = {c.email.id: c for c in load()}
    graph = build(with_memory=False)

    out = []
    for lane, case_id in PICKS.items():
        case = cases[case_id]
        state = graph.invoke({"email": case.email})
        d, s, ch = state["decision"], state["sort"], state["check"]

        record = {
            "expected_lane": case.expected_lane,
            "note": case.note,
            "tags": case.tags,
            "email": {
                "id": case.email.id,
                "from": case.email.sender_email,
                "sender": case.email.sender,
                "subject": case.email.subject,
                "body": case.email.body,
                "masked": case.email.masked,
            },
            "sort": {
                "lane": s.lane,
                "category": s.category,
                "action": s.action,
                "reason": s.reason,
            },
            "check": {
                "important_signals": ch.important_signals,
                "contains_instructions": ch.contains_instructions,
                "raise_to": ch.raise_to,
                "reason": ch.reason,
            },
            "decision": {
                "final_lane": d.final_lane,
                "action": d.action,
                "raises": d.raises,
            },
            "outcome": state.get("outcome", ""),
            "correct": d.final_lane == case.expected_lane,
        }
        out.append(record)

        style = LANE_STYLE[d.final_lane]
        body = [
            f"[bold]From:[/] {case.email.sender} <{case.email.sender_email}>",
            f"[bold]Subject:[/] {case.email.subject}",
            "",
            "[dim]" + case.email.body.replace("[", "\\[")[:400] + "[/]",
            "",
            f"[bold]1. READ[/]      masked: {case.email.masked or 'nothing'}",
            f"[bold]2. SORT[/]      -> [{LANE_STYLE[s.lane]}]{s.lane}[/] "
            f"({s.category}, action={s.action})",
            f"              [dim]{s.reason}[/]",
            f"[bold]3. CHECK[/]     signals={ch.important_signals or 'none'}  "
            f"instructions={ch.contains_instructions}  raise_to={ch.raise_to}",
        ]
        if ch.reason:
            body.append(f"              [dim]{ch.reason}[/]")
        body.append(f"[bold]4. DECIDE[/]    -> [{style}]{d.final_lane}[/]  action={d.action}")
        for r in d.raises:
            body.append(f"              [magenta]raised: {r}[/]")
        body.append(f"[bold]5. LANE[/]      {state.get('outcome', '')}")
        body.append("")
        mark = "[green]matches the label[/]" if record["correct"] else (
            f"[red]label said {case.expected_lane}[/]"
        )
        body.append(f"[bold]Expected:[/] {case.expected_lane}  {mark}")

        console.print(
            Panel(
                "\n".join(body),
                title=f"[{style}]{d.final_lane}[/]  {case_id}",
                border_style=style.replace("bold ", ""),
            )
        )

    path = Path(__file__).parent / "logs" / "examples.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    console.print(f"[green]Wrote {path}[/]")

    hits = sum(1 for r in out if r["correct"])
    console.print(f"[bold]{hits}/{len(out)} matched their label.[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
