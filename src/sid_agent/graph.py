"""
The LangGraph graph.

    START -> sort -> check -> decide -> calibrate -> (conditional) -> lane_*  -> END

`sort` and `check` are model calls. `decide` and `calibrate` are plain Python.
The four lane nodes are where execution would eventually hang off -- today they
only record what they *would* do.

Wired for the framework properly:
  - conditional edges, so the four lanes are real branches you can see in Studio
  - a Store, holding the calibration memory (memory.py)
  - a checkpointer, so a run can pause and resume
  - interrupt() on the ASK lane, which is the human-in-the-loop pattern
"""

from typing import Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.types import interrupt

from . import domains, memory, rules
from .check import check_email
from .schemas import Check, Decision, Email, Sort
from .sort import sort_email


class Config(TypedDict, total=False):
    user_id: str
    interactive: bool  # if True, the ASK lane actually pauses for a human


class State(TypedDict, total=False):
    email: Email
    sort: Sort
    check: Check
    decision: Decision
    outcome: str  # what the lane node did (or would have done)
    human: Optional[dict]  # what came back from an interrupt


def _cfg(config: RunnableConfig) -> tuple[str, bool]:
    c = (config or {}).get("configurable", {})
    return c.get("user_id", "default"), bool(c.get("interactive", False))


# --------------------------------------------------------------------------
# model steps
# --------------------------------------------------------------------------

def _sort_node(state: State) -> State:
    return {"sort": sort_email(state["email"])}


def _check_node(state: State) -> State:
    return {"check": check_email(state["email"], state["sort"])}


# --------------------------------------------------------------------------
# the deterministic step. no model. this is the safety layer.
# --------------------------------------------------------------------------

def _decide_node(state: State) -> State:
    email, sort, check = state["email"], state["sort"], state["check"]

    lane = sort.lane
    raises: list[str] = []
    hazards: list[str] = []

    def raise_to(target: rules.Lane, why: str, *, hazard: bool = True) -> None:
        """
        Move the lane up, never down, and record why.

        `hazard=False` marks a raise as mechanical -- the action simply is not
        permitted in the lane the model picked, which is the floor doing its
        ordinary job. Everything else is a judgement about this specific email
        and blocks the ledger from softening it later.
        """
        nonlocal lane
        harder = rules.more_cautious(lane, target)
        if harder != lane:
            raises.append(f"{lane} -> {harder}: {why}")
            if hazard:
                hazards.append(why)
            lane = harder

    if sort.action in rules.NEVER:
        raise_to("ESCALATE", f"'{sort.action}' is on the never-list")
    elif sort.action not in rules.KNOWN_ACTIONS:
        raise_to("ESCALATE", f"'{sort.action}' is not a known action")

    if email.masked:
        raise_to("ESCALATE", f"contains {', '.join(email.masked)}")

    # Sender metadata only -- never the body. A domain that is close to one we
    # trust but not equal to it is impersonation, and no model judgement about
    # the message's tone should be able to talk us out of that.
    impersonating = domains.lookalike_of(email.sender_email)
    if impersonating:
        raise_to("ESCALATE", f"sender domain imitates '{impersonating}'")

    # The friendly name is the part a human actually reads -- most mail clients
    # hide the address behind it entirely. A name claiming an affiliation the
    # address does not support is impersonation even when the domain itself is
    # perfectly ordinary, which is why the check above cannot catch it:
    # gmail.com is not a lookalike of anything.
    claimed = domains.display_name_spoof(email.sender, email.sender_email)
    if claimed and claimed != impersonating:
        raise_to("ESCALATE", f"display name claims '{claimed}', address does not")

    # Mechanical, not a hazard: the model picked a lane its own proposed action
    # is not allowed in. The floor corrects it, and that correction is no
    # evidence about the email's contents.
    needed = rules.min_lane_for(sort.action)
    if needed != lane:
        raise_to(needed, f"'{sort.action}' is not permitted in {sort.lane}", hazard=False)

    if check.raise_to:
        raise_to(check.raise_to, f"second check: {check.reason or 'flagged'}")

    # Text aimed at the agent rather than at you is never legitimate grounds for
    # an autonomous action, whatever lane the sorter picked. This used to raise
    # only to ASK unless the sorter had said SILENT, which let three injections
    # in the corpus land in ASK instead of ESCALATE.
    if check.contains_instructions:
        raise_to("ESCALATE", "contains text aimed at the agent")

    if lane == "SILENT" and check.important_signals:
        raise_to("ASK", f"looks routine but mentions: {', '.join(check.important_signals[:3])}")


    action = sort.action if rules.is_allowed(lane, sort.action) else "none"

    return {
        "decision": Decision(
            email_id=email.id,
            sender_email=email.sender_email,
            subject=email.subject,
            date=email.date,
            category=sort.category,
            action=action,
            proposed_lane=sort.lane,
            proposed_action=sort.action,
            check_raise_to=check.raise_to,
            final_lane=lane,
            raises=raises,
            hazards=hazards,
            sort_reason=sort.reason,
            check_reason=check.reason,
            important_signals=check.important_signals,
            contains_instructions=check.contains_instructions,
            masked=email.masked,
        )
    }


# --------------------------------------------------------------------------
# the one sanctioned place a lane may be relaxed -- and only within the
# bounds rules.py already set.
# --------------------------------------------------------------------------

def _calibrate_node(state: State, config: RunnableConfig, *, store: BaseStore) -> State:
    d = state["decision"]
    user_id, _ = _cfg(config)

    # Anything the rules had to intervene on is off-limits to the ledger.
    #
    # `masked` and `contains_instructions` were the original two, and they were
    # not enough: a lookalike domain, a never-list action, an unknown action and
    # a raise from `check` all left `blocked` False, so a farmed streak could
    # soften the very decision those rules had just made. The general form is
    # simpler than enumerating them -- if `decide` raised the lane at all, that
    # raise is a considered judgement about *this* email, and a run of accepts
    # on unrelated mail from the same sender is not evidence against it.
    #
    # The ordinary promotion path is untouched: an email that flowed through
    # `decide` without tripping a single rule has an empty `raises` list.
    # Hazards block the ledger; mechanical floor corrections do not.
    #
    # The first cut blocked on `bool(d.raises)`, which was too blunt: "'schedule'
    # is not permitted in NOTIFY" is the most common raise in the corpus and it
    # froze calibration outright. `masked` and `contains_instructions` stay named
    # explicitly because they must block even if a future edit forgets to record
    # them as hazards.
    #
    # Stake signals are the one conditional case, and the condition is whether
    # anything can actually happen. Promotion never changes *what* is done -- it
    # only changes how loudly the user is told -- so when the proposed action is
    # already `none`, promoting ASK -> NOTIFY moves an email from "ask me about
    # this" to "tell me about this" and executes nothing either way. Refusing to
    # learn there means a user can decline the same thing twenty times and still
    # be asked a twenty-first, which is the exact behaviour calibration exists to
    # fix. When an action *would* execute, stakes block it.
    acts = d.action != "none"
    blocked = (
        bool(d.masked)
        or d.contains_instructions
        or bool(d.hazards)
        or (bool(d.important_signals) and acts)
    )

    earned, why = memory.earned_lane(
        store, user_id, d.sender_email, d.action, d.final_lane, blocked=blocked
    )
    if why:
        d = d.model_copy(update={"final_lane": earned, "raises": d.raises + [why]})
        # a promoted lane must still permit the action
        if not rules.is_allowed(d.final_lane, d.action):
            d = d.model_copy(update={"action": "none"})

    return {"decision": d}


# --------------------------------------------------------------------------
# the four lanes
# --------------------------------------------------------------------------

def _route(state: State) -> str:
    return "lane_" + state["decision"].final_lane.lower()


def _lane_silent(state: State) -> State:
    return {"outcome": f"would {state['decision'].action} quietly"}


def _lane_notify(state: State) -> State:
    d = state["decision"]
    return {"outcome": f"would {d.action}, then tell you about {d.sender_email}"}


def _lane_ask(state: State, config: RunnableConfig) -> State:
    """The human-in-the-loop lane. Pauses the graph when interactive."""
    d = state["decision"]
    _, interactive = _cfg(config)

    if not interactive:
        return {"outcome": f"would ask you before '{d.action}'"}

    answer = interrupt(
        {
            "question": f"{d.sender_email} - {d.subject}",
            "proposed_action": d.action,
            "why": d.sort_reason,
            "raised": d.raises,
            "options": ["accepted", "edited", "rejected"],
        }
    )
    return {"human": answer if isinstance(answer, dict) else {"outcome": answer}}


def _lane_escalate(state: State) -> State:
    d = state["decision"]
    reason = d.raises[-1] if d.raises else d.sort_reason
    return {"outcome": f"no action taken - {reason}"}


# --------------------------------------------------------------------------

def build(*, store: BaseStore | None = None, checkpointer=None, with_memory: bool = True):
    """
    Compile the graph.

    `with_memory=False` gives a stateless graph (used by eval, where every email
    must be judged on its own merits with no learned history).
    """
    g = StateGraph(State, config_schema=Config)

    g.add_node("sort", _sort_node)
    g.add_node("check", _check_node)
    g.add_node("decide", _decide_node)
    g.add_node("lane_silent", _lane_silent)
    g.add_node("lane_notify", _lane_notify)
    g.add_node("lane_ask", _lane_ask)
    g.add_node("lane_escalate", _lane_escalate)

    g.add_edge(START, "sort")
    g.add_edge("sort", "check")
    g.add_edge("check", "decide")

    if with_memory:
        g.add_node("calibrate", _calibrate_node)
        g.add_edge("decide", "calibrate")
        branch_from = "calibrate"
    else:
        branch_from = "decide"

    g.add_conditional_edges(
        branch_from,
        _route,
        {
            "lane_silent": "lane_silent",
            "lane_notify": "lane_notify",
            "lane_ask": "lane_ask",
            "lane_escalate": "lane_escalate",
        },
    )

    for lane in ["lane_silent", "lane_notify", "lane_ask", "lane_escalate"]:
        g.add_edge(lane, END)

    kwargs = {}
    if with_memory:
        kwargs["store"] = store or InMemoryStore()
        kwargs["checkpointer"] = checkpointer or MemorySaver()

    return g.compile(**kwargs)


def run_one(graph, email: Email, *, user_id: str = "default", thread: str | None = None) -> Decision:
    """Run a single email through and hand back just the decision."""
    config = {
        "configurable": {
            "thread_id": thread or f"email-{email.id}",
            "user_id": user_id,
        }
    }
    return graph.invoke({"email": email}, config)["decision"]


# For `langgraph dev` / LangGraph Studio.
graph = build()
