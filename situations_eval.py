#!/usr/bin/env python
"""
Scores the proactive path against data/situations.json.

  python situations_eval.py
  python situations_eval.py --json

No API key, no network, no model -- the proactive path makes no model call by
construction, so its eval needs nothing either. This is the same discipline
verify.py follows and it is not an accident: a reviewer should be able to check
the claims in this repo before deciding whether to trust it with a key.

It reports the same three numbers eval.py does, because they mean the same thing
here. An over-cautious situation costs a glance. An unsafe one is the agent
quietly doing something about your money while you were not looking.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from sid_agent import rules  # noqa: E402
from sid_agent.situations import decide_situation, detect  # noqa: E402

console = Console()

DATA = Path(__file__).parent / "data" / "situations.json"

LANE_STYLE = {"SILENT": "dim", "NOTIFY": "cyan", "ASK": "yellow", "ESCALATE": "bold red"}


def _rank(lane: str | None) -> int:
    return rules._RANK[lane] if lane else -1


def main() -> int:
    threads = json.loads(DATA.read_text(encoding="utf-8"))["threads"]
    expected = {t["id"]: t for t in threads}

    # detect() is given the whole mailbox at once, the way it would see it.
    found = {s.id.rsplit("-", 1)[0]: s for s in detect(threads)}

    rows, exact, cautious, unsafe = [], 0, 0, 0

    for t in threads:
        want = t["expected_lane"]
        situation = found.get(t["id"])

        if situation is None:
            got, kind, raised = None, "-- nothing noticed --", []
        else:
            d = decide_situation(situation)
            got, kind, raised = d.final_lane, situation.kind, d.raises

        if got == want:
            exact += 1
            verdict, style = "exact", "green"
        elif _rank(got) > _rank(want):
            cautious += 1
            verdict, style = "over-cautious", "yellow"
        else:
            unsafe += 1
            verdict, style = "UNSAFE", "bold red"

        rows.append((t["id"], kind, want, got, verdict, style, raised))

    table = Table(box=None, pad_edge=False)
    for col in ("id", "situation", "expected", "got", ""):
        table.add_column(col)
    for sid, kind, want, got, verdict, style, _ in rows:
        table.add_row(
            sid,
            kind,
            f"[{LANE_STYLE.get(want, 'dim')}]{want or 'nothing'}[/]",
            f"[{LANE_STYLE.get(got, 'dim')}]{got or 'nothing'}[/]",
            f"[{style}]{verdict}[/]",
        )

    total = len(rows)
    console.print("\n[bold]proactive path[/] -- situations noticed without an incoming message\n")
    console.print(table)
    console.print(
        f"\n  exact          {exact}/{total} ({exact / total:.0%})"
        f"\n  over-cautious  {cautious}"
        f"\n  [bold]UNSAFE         {unsafe}[/]"
    )

    # The floor is the claim worth checking mechanically rather than by eye.
    violations = [
        (sid, got) for sid, _, _, got, _, _, _ in rows
        if got and _rank(got) < _rank(rules.min_lane_for("none"))
    ]
    console.print(
        f"\n  no model calls made. floor violations: {len(violations)}"
    )

    if unsafe:
        console.print("\n[bold red]  Unsafe results are regressions. Fix before shipping.[/]\n")
        return 1
    console.print("\n[green]  Nothing landed in a laxer lane than it deserved.[/]\n")
    return 0


if __name__ == "__main__":
    if "--json" in sys.argv:
        threads = json.loads(DATA.read_text(encoding="utf-8"))["threads"]
        found = {s.id.rsplit("-", 1)[0]: s for s in detect(threads)}
        out = []
        for t in threads:
            s = found.get(t["id"])
            out.append({
                "id": t["id"],
                "expected": t["expected_lane"],
                "got": decide_situation(s).final_lane if s else None,
                "kind": s.kind if s else None,
            })
        print(json.dumps(out, indent=2))
        raise SystemExit(0)
    raise SystemExit(main())
