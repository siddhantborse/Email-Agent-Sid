"""
The proactive half of the agent.

Everything else in this repo reacts: an email arrives, the agent sorts it. But
an inbox generates work that no incoming message announces. Nobody sends you a
message saying "you have not replied to your manager in four days" -- that is a
*situation*, and noticing it is most of what makes an assistant feel proactive
rather than merely responsive.

A situation is a fact about mailbox state: a thread gone quiet, an invoice
approaching its due date, an invite still unanswered, a pile of newsletters.
It gets the same four-lane autonomy decision as an email, through the same
`rules.py` floor and the same calibration ledger.

The interesting property is what is NOT here: no model call.

A situation is derived by our own code from metadata we produced -- thread ids,
timestamps, read flags, existing labels. No attacker-controlled text reaches the
decision. That makes the proactive path structurally safer than the reactive
one: it has no prompt to inject, so `sort` and `check` have nothing to be fooled
about, and the whole surface reduces to a table you can read in one sitting.

Which is the same argument the rest of the project makes, arrived at from the
other direction: the reactive path needs two model calls and a deterministic
referee because it reads untrusted text. This path reads none, so it needs no
referee -- only the floor.
"""

from dataclasses import dataclass, field
from typing import Literal

from . import rules
from .schemas import Decision

Kind = Literal[
    "unanswered_thread",
    "unconfirmed_meeting",
    "invoice_due",
    "renewal_upcoming",
    "newsletter_pile",
    "unread_security_alert",
    "waiting_on_reply",
]


@dataclass
class Situation:
    """
    One noticed fact about the mailbox.

    `sender_email` and `action` are here because they are the calibration key:
    a situation earns autonomy per (counterparty, action) exactly the way an
    email does, so "stop asking me about rescheduling with Priya" is learnable
    on both paths at once rather than twice over.
    """

    id: str
    kind: Kind
    sender_email: str
    subject: str
    detail: str = ""
    age_days: int = 0
    # Set when the underlying thread carried masked content or agent-directed
    # text. Carried forward so a tainted thread cannot earn autonomy sideways
    # through the proactive path after being blocked on the reactive one.
    tainted: bool = False
    tags: list[str] = field(default_factory=list)


# What each kind of situation would do about itself, and the least cautious lane
# it may be handled in. The lane here is a *starting suggestion*; rules.py still
# has the final say, and min_lane_for(action) can only push it up from here.
#
# Read this table as the whole proactive policy, because it is.
PLAYBOOK: dict[Kind, tuple[str, rules.Lane]] = {
    # A stack of unread newsletters is the one genuinely boring case. Filing it
    # is reversible and nobody wants to be told about it.
    "newsletter_pile": ("archive", "SILENT"),
    # Confirming a meeting you already agreed to is the canonical "do it and
    # mention it" case -- the reason the NOTIFY lane exists at all.
    "unconfirmed_meeting": ("reply_scheduling", "NOTIFY"),
    # Someone is waiting on you. Worth surfacing, not worth answering for you:
    # a reply in your voice to a real person is not a thing to do unasked.
    "unanswered_thread": ("reply", "ASK"),
    "waiting_on_reply": ("reply", "ASK"),
    # Money. Both of these end at ESCALATE regardless of what this column says,
    # because no autonomous action near a payment is worth the tail risk. They
    # are listed as `none` to make that explicit rather than incidental.
    "invoice_due": ("none", "ESCALATE"),
    "renewal_upcoming": ("none", "ASK"),
    # A security alert is the one thing you never want handled quietly.
    "unread_security_alert": ("none", "ESCALATE"),
}

# Kinds that must reach the human no matter what the playbook or the ledger
# says. This is the proactive mirror of rules.NEVER: a hard floor expressed in
# situation terms, so that adding a careless PLAYBOOK row cannot lower them.
ALWAYS_ESCALATE: set[Kind] = {"invoice_due", "unread_security_alert"}


def decide_situation(
    situation: Situation,
    *,
    store=None,
    user_id: str = "default",
) -> Decision:
    """
    Settle the lane for one situation.

    Deliberately the same shape as `graph._decide_node`: start from a proposed
    lane, only ever raise it, and record every raise with its reason. A reviewer
    who has read that function should find nothing surprising here.
    """
    action, lane = PLAYBOOK.get(situation.kind, ("none", rules.DEFAULT_LANE))
    proposed = lane
    raises: list[str] = []
    hazards: list[str] = []

    def raise_to(target: rules.Lane, why: str, *, hazard: bool = True) -> None:
        """Same contract as graph._decide_node: only up, and say whether it matters."""
        nonlocal lane
        harder = rules.more_cautious(lane, target)
        if harder != lane:
            raises.append(f"{lane} -> {harder}: {why}")
            if hazard:
                hazards.append(why)
            lane = harder

    # An unknown kind is an unknown situation, and unknown means escalate. This
    # matters because `PLAYBOOK.get` above already fell back to DEFAULT_LANE --
    # this branch exists to record *why*, so the log explains itself.
    if situation.kind not in PLAYBOOK:
        raise_to("ESCALATE", f"'{situation.kind}' is not a known situation")

    if situation.kind in ALWAYS_ESCALATE:
        raise_to("ESCALATE", f"'{situation.kind}' always reaches a human")

    if action in rules.NEVER:
        raise_to("ESCALATE", f"'{action}' is on the never-list")
    elif action not in rules.KNOWN_ACTIONS:
        raise_to("ESCALATE", f"'{action}' is not a known action")

    if situation.tainted:
        raise_to("ESCALATE", "the underlying thread was flagged on the reactive path")

    # The same floor the reactive path uses. This is the point of the exercise:
    # there is one authority table, not one per entry point.
    needed = rules.min_lane_for(action)
    if needed != lane:
        raise_to(needed, f"'{action}' is not permitted in {proposed}", hazard=False)

    # Calibration, bounded exactly as it is for email.
    if store is not None:
        from . import memory

        # Same rule as the reactive path: if anything raised this lane, the
        # ledger does not get to lower it again.
        earned, why = memory.earned_lane(
            store, user_id, situation.sender_email, action, lane,
            blocked=situation.tainted or bool(hazards),
        )
        if why:
            raises.append(why)
            lane = earned

    final_action = action if rules.is_allowed(lane, action) else "none"

    return Decision(
        email_id=situation.id,
        sender_email=situation.sender_email,
        subject=situation.subject,
        date=f"(t+{situation.age_days}d)",
        category=situation.kind,
        action=final_action,
        proposed_lane=proposed,
        final_lane=lane,
        raises=raises,
        hazards=hazards,
        sort_reason=situation.detail,
        check_reason="no model call: situations carry no untrusted text",
    )


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------

# How long a thread may sit before it counts as gone quiet. Deliberately a
# constant rather than a model judgement -- "is four days too long" is a
# preference, and preferences belong in the ledger, not in a prompt.
QUIET_AFTER_DAYS = 3
RENEWAL_NOTICE_DAYS = 7


def detect(threads: list[dict]) -> list[Situation]:
    """
    Turn mailbox state into situations.

    `threads` is a list of plain dicts so this stays testable without a mailbox:
    each carries id, sender_email, subject, last_activity_days, and flags. A
    Gmail adapter would produce the same dicts; nothing below knows or cares
    where they came from.
    """
    found: list[Situation] = []

    for t in threads:
        age = int(t.get("last_activity_days", 0))
        common = {
            "sender_email": t.get("sender_email", ""),
            "subject": t.get("subject", ""),
            "age_days": age,
            "tainted": bool(t.get("tainted", False)),
            "tags": list(t.get("tags", [])),
        }

        if t.get("security_alert") and t.get("unread"):
            found.append(Situation(
                id=f"{t['id']}-sec", kind="unread_security_alert",
                detail="an unread security alert is sitting in the inbox", **common,
            ))
            continue

        if t.get("invoice_unpaid"):
            found.append(Situation(
                id=f"{t['id']}-inv", kind="invoice_due",
                detail=f"invoice unpaid, due in {t.get('due_in_days', 0)} days", **common,
            ))
            continue

        if t.get("renewal_in_days") is not None and \
                0 <= int(t["renewal_in_days"]) <= RENEWAL_NOTICE_DAYS:
            found.append(Situation(
                id=f"{t['id']}-ren", kind="renewal_upcoming",
                detail=f"subscription auto-renews in {t['renewal_in_days']} days", **common,
            ))
            continue

        if t.get("invite_unanswered"):
            found.append(Situation(
                id=f"{t['id']}-mtg", kind="unconfirmed_meeting",
                detail="a meeting invite you already agreed to is still unconfirmed",
                **common,
            ))
            continue

        if t.get("newsletter_backlog", 0) >= 20:
            found.append(Situation(
                id=f"{t['id']}-pile", kind="newsletter_pile",
                detail=f"{t['newsletter_backlog']} unread newsletters", **common,
            ))
            continue

        # Direction matters. Them waiting on you is something you might act on;
        # you waiting on them is something you might chase. Different situations,
        # same lane today, but conflating them would make the ledger meaningless.
        if age >= QUIET_AFTER_DAYS and t.get("awaiting_your_reply"):
            found.append(Situation(
                id=f"{t['id']}-quiet", kind="unanswered_thread",
                detail=f"no reply from you in {age} days", **common,
            ))
        elif age >= QUIET_AFTER_DAYS and t.get("awaiting_their_reply"):
            found.append(Situation(
                id=f"{t['id']}-chase", kind="waiting_on_reply",
                detail=f"no reply from them in {age} days", **common,
            ))

    return found
