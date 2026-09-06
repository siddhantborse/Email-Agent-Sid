"""
The safety model. This is the whole thing, and it is deliberately boring.

The AI never edits this file. The AI only ever *suggests* a lane. This file
decides what that lane is allowed to do, and it can only ever move an email to
a MORE cautious lane -- never a less cautious one.

Read this file top to bottom and you understand the agent's authority.
"""

from typing import Literal

Lane = Literal["SILENT", "NOTIFY", "ASK", "ESCALATE"]

# Ordered least cautious -> most cautious. Everything below relies on this order.
LANES: list[Lane] = ["SILENT", "NOTIFY", "ASK", "ESCALATE"]
_RANK = {lane: i for i, lane in enumerate(LANES)}

LANE_MEANING = {
    "SILENT": "handle it, say nothing",
    "NOTIFY": "handle it, tell me afterwards",
    "ASK": "stop, ask me, then do what I say",
    "ESCALATE": "touch nothing, just put it in front of me",
}

# What each lane may do. An action not listed here is not permitted in that lane.
ALLOWED: dict[Lane, list[str]] = {
    "SILENT": ["archive", "label", "none"],
    "NOTIFY": ["archive", "label", "reply_scheduling", "none"],
    "ASK": ["reply", "forward", "attach_file", "schedule", "none"],
    "ESCALATE": ["none"],  # nothing at all
}

# No lane unlocks these. No amount of learning unlocks these. Ever.
NEVER = [
    "send_money",
    "share_otp",
    "share_credential",
    "change_password",
    "add_forwarding_rule",
    "add_delegate",
    "change_account_settings",
    "delete_forever",
]

# Where an email goes when we cannot confidently place it.
DEFAULT_LANE: Lane = "ESCALATE"

# Actions the agent may propose at all. Anything else is treated as unknown.
KNOWN_ACTIONS = sorted(
    {a for actions in ALLOWED.values() for a in actions} | set(NEVER)
)


def more_cautious(a: Lane, b: Lane) -> Lane:
    """Return whichever lane is more cautious. This is how we guarantee we never relax."""
    return a if _RANK[a] >= _RANK[b] else b


def min_lane_for(action: str) -> Lane:
    """
    The least cautious lane that is permitted to perform this action.

    If the AI says "SILENT" but proposes "forward", forwarding is not something
    SILENT may do -- so the email moves up to ASK, the first lane that allows it.
    """
    if action in NEVER:
        return "ESCALATE"
    for lane in LANES:
        if action in ALLOWED[lane]:
            return lane
    return DEFAULT_LANE  # unknown action -> escalate


def is_allowed(lane: Lane, action: str) -> bool:
    """Would this lane be permitted to perform this action?"""
    if action in NEVER:
        return False
    return action in ALLOWED.get(lane, [])
