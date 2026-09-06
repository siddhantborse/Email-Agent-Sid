"""
Calibration memory, held in the LangGraph Store.

This is the part that makes the agent ask you less over time. It is a counter,
not a model: per (sender, action) pair we track how you have responded, and once
you have accepted the same thing enough times in a row the agent earns one step
of autonomy for that pair.

The safety property that makes this sane:

    promotion can never go below rules.min_lane_for(action)

So `reply_scheduling` (floor NOTIFY) can be promoted ASK -> NOTIFY, but
`forward` (floor ASK) can never be promoted at all, and nothing on the
never-list is reachable. The rules table bounds the learning; the learning
cannot rewrite the rules table.
"""

import json
from pathlib import Path
from typing import Any, Literal

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from . import rules

STORE_PATH = Path(__file__).resolve().parents[2] / "logs" / "trust.json"


class FileStore(InMemoryStore):
    """
    An InMemoryStore that survives the process.

    Without this the agent learns during a run and forgets everything on exit,
    which is not learning -- it is a goldfish. Preferences are small and
    write-rarely, so a JSON file is the right amount of machinery.
    """

    def __init__(self, path: Path | None = None):
        super().__init__()
        self._path = Path(path or STORE_PATH)
        self._namespaces: set[tuple[str, ...]] = set()
        self._load()

    def put(self, namespace, key, value, **kwargs):  # noqa: ANN001
        super().put(namespace, key, value, **kwargs)
        self._namespaces.add(tuple(namespace))
        self._flush()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            blob = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # a corrupt ledger means we start over, never that we crash
        for ns_key, items in blob.items():
            ns = tuple(ns_key.split("\x1f"))
            for key, value in items.items():
                super().put(ns, key, value)
            self._namespaces.add(ns)

    def _flush(self) -> None:
        out: dict[str, dict[str, Any]] = {}
        for ns in self._namespaces:
            for item in super().search(ns):
                out.setdefault("\x1f".join(ns), {})[item.key] = item.value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(out, indent=2), encoding="utf-8")

Outcome = Literal["accepted", "edited", "rejected"]

# How many clean accepts in a row before a pair earns a step of autonomy.
PROMOTE_AFTER = 5

# The quietest lane trust can ever buy. Promotion stops here even when the
# action's own floor is lower.
#
# `archive` and `label` have floor SILENT, so without this ceiling a NOTIFY
# decision could be learned all the way down to SILENT -- "handle it and tell
# me" quietly becoming "handle it and say nothing". That is the failure mode
# this whole project is organised around: the one the user never finds out
# about. DESIGN.md claimed this was impossible before it was true.
PROMOTION_CEILING: rules.Lane = "NOTIFY"

# Only these actions can ever earn autonomy. Anything that sends a file out or
# talks to someone new stays a human decision forever.
PROMOTABLE = {"archive", "label", "reply_scheduling", "schedule", "none"}


def _ns(user_id: str) -> tuple[str, str]:
    return ("trust", user_id)


def _key(sender_email: str, action: str) -> str:
    return f"{sender_email.lower()}|{action}"


def get(store: BaseStore, user_id: str, sender_email: str, action: str) -> dict[str, Any]:
    """Current record for one (sender, action) pair. Zeros if never seen."""
    item = store.get(_ns(user_id), _key(sender_email, action))
    if item is None:
        return {"streak": 0, "accepts": 0, "rejects": 0, "edits": 0}
    return dict(item.value)


def record(
    store: BaseStore,
    user_id: str,
    sender_email: str,
    action: str,
    outcome: Outcome,
) -> dict[str, Any]:
    """
    Log one human decision.

    Slow to trust, instant to distrust: an accept adds one to the streak, but a
    single edit or rejection resets it to zero. One bad call costs five good ones.
    """
    rec = get(store, user_id, sender_email, action)

    if outcome == "accepted":
        rec["accepts"] += 1
        rec["streak"] += 1
    elif outcome == "edited":
        rec["edits"] += 1
        rec["streak"] = 0
    else:
        rec["rejects"] += 1
        rec["streak"] = 0

    store.put(_ns(user_id), _key(sender_email, action), rec)
    return rec


def earned_lane(
    store: BaseStore,
    user_id: str,
    sender_email: str,
    action: str,
    current_lane: rules.Lane,
    *,
    blocked: bool = False,
) -> tuple[rules.Lane, str | None]:
    """
    The lane this pair has earned, given its history.

    Returns (lane, reason_if_promoted). Promotes by at most one step, and never
    below the floor that rules.py sets for the action. `blocked=True` (masked
    content, agent-directed text) refuses promotion outright.
    """
    # The floor from rules.py is absolute, and it applies on every path out of
    # this function -- including the ones that decline to promote. Without this
    # clamp, a lane that arrived below its floor would be passed straight back.
    floor = rules.min_lane_for(action)
    current_lane = rules.more_cautious(current_lane, floor)

    if blocked or action not in PROMOTABLE:
        return current_lane, None

    # ESCALATE is a verdict, not an opening bid. Something reached it because a
    # rule fired -- a never-list action, a masked secret, a spoofed domain -- and
    # no amount of unrelated good behaviour from the same sender is evidence
    # against that specific rule. Without this, a sender whose ordinary mail the
    # user waves through five times could soften their *next* escalation one
    # step, because the clamped action `none` has floor SILENT and so looks
    # freely promotable.
    if current_lane == "ESCALATE":
        return current_lane, None

    rec = get(store, user_id, sender_email, action)
    if rec["streak"] < PROMOTE_AFTER:
        return current_lane, None

    rank = rules._RANK[current_lane]
    if rank == 0:
        return current_lane, None  # already at SILENT, nothing to earn

    one_step_down = rules.LANES[rank - 1]
    promoted = rules.more_cautious(one_step_down, floor)
    promoted = rules.more_cautious(promoted, PROMOTION_CEILING)

    if promoted == current_lane:
        return current_lane, None

    return promoted, (
        f"{current_lane} -> {promoted}: {rec['streak']} clean accepts "
        f"for '{action}' from {sender_email}"
    )


def summary(store: BaseStore, user_id: str) -> list[dict[str, Any]]:
    """Everything the agent has learned so far, for inspection."""
    rows = []
    for item in store.search(_ns(user_id)):
        sender, _, action = item.key.partition("|")
        rows.append({"sender": sender, "action": action, **item.value})
    return sorted(rows, key=lambda r: -r["streak"])
