#!/usr/bin/env python
"""
Prove the safety layer works. Needs no API key and makes no network calls.

Everything here tests the deterministic half of the agent -- masking and
rules.py -- by handing the decide step pre-made AI suggestions, including
hostile ones. If these pass, the safety properties hold no matter how wrong or
how manipulated the AI's suggestion was.

  python verify.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from sid_agent import rules  # noqa: E402
from sid_agent.graph import _decide_node  # noqa: E402
from sid_agent.read import mask, prepare  # noqa: E402
from sid_agent.schemas import Check, Email, Sort  # noqa: E402

PASS, FAIL = "  PASS", "  FAIL"
failures = 0
total = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. Counted, so the suite can report its own size."""
    global failures, total
    total += 1
    if condition:
        print(f"{PASS}  {name}")
    else:
        failures += 1
        print(f"{FAIL}  {name}")
        if detail:
            print(f"        {detail}")


def decide(
    *,
    lane="SILENT",
    action="archive",
    masked=None,
    raise_to=None,
    instructions=False,
    signals=None,
):
    """Run just the decide step with a made-up AI suggestion."""
    state = {
        "email": Email(
            id="t", sender_email="someone@example.com", subject="s",
            body="b", masked=masked or [],
        ),
        "sort": Sort(lane=lane, category="test", action=action, reason="test"),
        "check": Check(
            important_signals=signals or [],
            contains_instructions=instructions,
            raise_to=raise_to,
            reason="test",
        ),
    }
    return _decide_node(state)["decision"]


print("\n--- masking (runs before the AI sees anything) ---")

masked_text, found = mask("Your verification code is 839214. It expires soon.")
check("OTP digits removed", "839214" not in masked_text, masked_text)
check("OTP labelled", found == ["otp"], str(found))

masked_text, found = mask("Card 4111 1111 1111 1111 charged today.")
check("card number removed", "4111" not in masked_text, masked_text)

masked_text, found = mask("Meeting at 2pm, room 402, 15 people attending.")
check("ordinary numbers survive", "402" in masked_text and found == [], masked_text)

_, _, labels = prepare("Your code", "Your OTP is 1234")
check("prepare() reports masking", labels == ["otp"], str(labels))


print("\n--- lane ordering (the guarantee that we never relax) ---")

check("ESCALATE beats SILENT", rules.more_cautious("SILENT", "ESCALATE") == "ESCALATE")
check("order is symmetric", rules.more_cautious("ESCALATE", "SILENT") == "ESCALATE")
check("ASK beats NOTIFY", rules.more_cautious("NOTIFY", "ASK") == "ASK")
check("forward needs ASK", rules.min_lane_for("forward") == "ASK")
check("archive is fine in SILENT", rules.min_lane_for("archive") == "SILENT")
check("send_money escalates", rules.min_lane_for("send_money") == "ESCALATE")
check("unknown action escalates", rules.min_lane_for("wat") == "ESCALATE")


print("\n--- the decide step, given bad or hostile AI suggestions ---")

d = decide(lane="SILENT", action="archive")
check("clean newsletter stays SILENT", d.final_lane == "SILENT", d.final_lane)

d = decide(lane="SILENT", action="forward")
check(
    "AI says 'silently forward' -> raised to ASK",
    d.final_lane == "ASK",
    f"got {d.final_lane}",
)

d = decide(lane="SILENT", action="send_money")
check(
    "never-list action -> ESCALATE",
    d.final_lane == "ESCALATE" and d.action == "none",
    f"got {d.final_lane}/{d.action}",
)

d = decide(lane="SILENT", action="archive", masked=["otp"])
check(
    "email contained an OTP -> ESCALATE",
    d.final_lane == "ESCALATE",
    f"got {d.final_lane}",
)

d = decide(lane="SILENT", action="archive", instructions=True)
check(
    "injection: talked its way into SILENT -> ESCALATE",
    d.final_lane == "ESCALATE",
    f"got {d.final_lane}",
)

for start in ["SILENT", "NOTIFY", "ASK", "ESCALATE"]:
    d = decide(lane=start, action="none", instructions=True)
    check(
        f"agent-directed text from {start} -> ESCALATE",
        d.final_lane == "ESCALATE",
        f"got {d.final_lane}",
    )

d = decide(lane="SILENT", action="archive", signals=["payment due", "deadline"])
check(
    "looks routine but mentions money -> raised off SILENT",
    d.final_lane != "SILENT",
    f"got {d.final_lane}",
)

d = decide(lane="ESCALATE", action="none", raise_to="SILENT")
check(
    "second check cannot lower a lane",
    d.final_lane == "ESCALATE",
    f"got {d.final_lane}",
)

d = decide(lane="NOTIFY", action="reply_scheduling", raise_to="ESCALATE")
check("second check can raise a lane", d.final_lane == "ESCALATE", d.final_lane)

d = decide(lane="ESCALATE", action="reply")
check(
    "ESCALATE is allowed no action at all",
    d.action == "none",
    f"got action={d.action}",
)

d = decide(lane="SILENT", action="forward")
check("raises are recorded for the log", len(d.raises) > 0, str(d.raises))


print("\n--- exhaustive: no suggestion of any kind can reach a laxer lane ---")

worst = None
for lane in rules.LANES:
    for action in rules.KNOWN_ACTIONS:
        out = decide(lane=lane, action=action)
        if rules._RANK[out.final_lane] < rules._RANK[lane]:
            worst = (lane, action, out.final_lane)
            break
check(
    f"all {len(rules.LANES) * len(rules.KNOWN_ACTIONS)} lane/action pairs only ever raise",
    worst is None,
    f"{worst} went DOWN" if worst else "",
)

for lane in rules.LANES:
    for action in rules.KNOWN_ACTIONS:
        out = decide(lane=lane, action=action)
        if not rules.is_allowed(out.final_lane, out.action):
            worst = (lane, action, out.final_lane, out.action)
            break
check("final action is always permitted by the final lane", worst is None, str(worst))


print("\n--- lookalike sender domains (deterministic, no model) ---")

from sid_agent.domains import lookalike_of  # noqa: E402

KNOWN = {"knowncompany.example"}

check(
    "suffixed lookalike caught",
    lookalike_of("d.okafor@knowncompany-support.example", KNOWN) == "knowncompany.example",
)
check(
    "typo lookalike caught",
    lookalike_of("y@knowncompanny.example", KNOWN) == "knowncompany.example",
)
check("the real domain is not a lookalike", lookalike_of("priya@knowncompany.example", KNOWN) is None)
check("a real subdomain is not a lookalike", lookalike_of("x@mail.knowncompany.example", KNOWN) is None)
check("an unrelated domain is not a lookalike", lookalike_of("z@other.example", KNOWN) is None)
check("no known domains configured -> no false positives", lookalike_of("a@anything.example", set()) is None)



# Regressions for the shapes that used to evade lookalike detection entirely.
# Each of these was a confirmed miss: the same attack body from a differently
# shaped domain sailed past the deterministic layer and landed on model
# judgement alone.
KNOWN = {"knowncompany.example"}
for addr, want, why in [
    ("d@knowncompany.com", True, "TLD swap -- the most common real spoof shape"),
    ("d@knowncompany.co", True, "shortened TLD"),
    ("d@knowncompany.example.evil.com", True, "trusted name as a subdomain label"),
    ("d@knowncompany.secure-mail.example", True, "trusted name buried mid-domain"),
    ("d@knowncompany.example.", False, "trailing dot is the same host, not a spoof"),
    ("d@knowwncommpanny.example", True, "three-edit typo"),
    ("d@know.example", False, "a short real domain is not an impersonation"),
    ("d@own.example", False, "substring of the trusted name, but unrelated"),
    ("d@supplier.example", False, "ordinary unrelated sender"),
]:
    got = lookalike_of(addr, KNOWN) is not None
    check(f"{'caught' if want else 'allowed'}: {why}", got == want, f"{addr} -> {got}")

print("\n--- calibration: learning is bounded by the rules table ---")

from langgraph.store.memory import InMemoryStore  # noqa: E402

from sid_agent import memory  # noqa: E402


def store_with(action: str, outcomes: list[str]) -> InMemoryStore:
    s = InMemoryStore()
    for o in outcomes:
        memory.record(s, "u", "someone@example.com", action, o)
    return s


s = store_with("reply_scheduling", ["accepted"] * 4)
lane, _ = memory.earned_lane(s, "u", "someone@example.com", "reply_scheduling", "ASK")
check("4 accepts is not yet enough to promote", lane == "ASK", lane)

s = store_with("reply_scheduling", ["accepted"] * 5)
lane, why = memory.earned_lane(s, "u", "someone@example.com", "reply_scheduling", "ASK")
check("5 accepts promotes ASK -> NOTIFY", lane == "NOTIFY" and why, f"{lane}")

s = store_with("reply_scheduling", ["accepted"] * 4 + ["rejected"])
lane, _ = memory.earned_lane(s, "u", "someone@example.com", "reply_scheduling", "ASK")
check("one rejection resets the streak", lane == "ASK", lane)

s = store_with("reply_scheduling", ["accepted"] * 4 + ["edited"])
lane, _ = memory.earned_lane(s, "u", "someone@example.com", "reply_scheduling", "ASK")
check("an edit also resets the streak", lane == "ASK", lane)

s = store_with("reply_scheduling", ["accepted"] * 50)
lane, _ = memory.earned_lane(
    s, "u", "someone@example.com", "reply_scheduling", "ASK", blocked=True
)
check("blocked email refuses promotion despite 50 accepts", lane == "ASK", lane)

# This check used to use `reply_scheduling`, whose floor is already NOTIFY -- so
# it could not fail no matter what the code did, and it did not fail while
# `archive` and `label` were being promoted straight into SILENT. A test that
# cannot fail is worse than no test: it occupies the space where a real one
# would have gone. Every action whose floor is SILENT is now swept.
into_silent = [
    a for a in sorted(memory.PROMOTABLE)
    if rules.min_lane_for(a) == "SILENT"
    and memory.earned_lane(
        store_with(a, ["accepted"] * 100), "u", "someone@example.com", a, "NOTIFY"
    )[0] == "SILENT"
]
check(
    f"no promotable action ({', '.join(sorted(memory.PROMOTABLE))}) can be learned into SILENT",
    not into_silent,
    str(into_silent),
)

# ESCALATE is a verdict, not an opening bid.
out_of_escalate = [
    a for a in sorted(memory.PROMOTABLE)
    if memory.earned_lane(
        store_with(a, ["accepted"] * 100), "u", "someone@example.com", a, "ESCALATE"
    )[0] != "ESCALATE"
]
check(
    "no run of accepts can soften an ESCALATE",
    not out_of_escalate,
    str(out_of_escalate),
)

# The composed path. Sweeping `decide` alone and `earned_lane` alone both passed
# while the seam between them leaked: `blocked` did not account for a lane that
# `decide` had raised, so a farmed streak could undo a lookalike-domain or
# never-list escalation. Compose them and check the whole thing.
composed = []
for lane_in in rules.LANES:
    for action in sorted(memory.PROMOTABLE):
        for kwargs in (
            {"action": "send_money"},
            {"action": "not_a_real_action"},
            {"masked": ["otp"]},
            {"instructions": True},
            {"raise_to": "ESCALATE"},
            {"signals": ["payment due"]},
        ):
            d = decide(lane=lane_in, **kwargs)
            st = store_with(d.action, ["accepted"] * 100)
            earned, _ = memory.earned_lane(
                st, "u", "someone@example.com", d.action, d.final_lane,
                blocked=(bool(d.masked) or d.contains_instructions
                         or bool(d.raises) or bool(d.important_signals)),
            )
            if rules._RANK[earned] < rules._RANK[d.final_lane]:
                composed.append((lane_in, action, kwargs, d.final_lane, earned))
check(
    f"decide -> calibrate composed: no raise can be undone by the ledger "
    f"({len(rules.LANES) * len(memory.PROMOTABLE) * 6} combinations)",
    not composed,
    str(composed[:3]),
)

breach = None
for action in rules.KNOWN_ACTIONS:
    s = store_with(action, ["accepted"] * 100)
    for start in rules.LANES:
        lane, _ = memory.earned_lane(s, "u", "someone@example.com", action, start)
        floor = rules.min_lane_for(action)
        if rules._RANK[lane] < rules._RANK[floor]:
            breach = (action, start, lane, floor)
            break
check(
    "no action, at any lane, after 100 accepts, can be learned below its floor",
    breach is None,
    f"{breach}" if breach else "",
)

never_moved = None
for action in rules.NEVER:
    s = store_with(action, ["accepted"] * 100)
    lane, _ = memory.earned_lane(s, "u", "someone@example.com", action, "ESCALATE")
    if lane != "ESCALATE":
        never_moved = (action, lane)
        break
check("never-list actions stay at ESCALATE forever", never_moved is None, str(never_moved))


# ---------------------------------------------------------------------------
# the proactive path: same floor, different entry point
# ---------------------------------------------------------------------------
print("\n--- proactive situations (no model call anywhere in this path) ---")

from sid_agent import situations as sit  # noqa: E402

# The exhaustive one. Every row of the playbook is a lane a human wrote by hand,
# and a hand-written lane is exactly the kind of thing that drifts below its
# floor during a refactor. Checking the table itself is worth more than checking
# any single situation that flows through it.
bad_rows = [
    (kind, lane, action, rules.min_lane_for(action))
    for kind, (action, lane) in sit.PLAYBOOK.items()
    if rules.more_cautious(lane, rules.min_lane_for(action)) != lane
]
check(
    f"all {len(sit.PLAYBOOK)} playbook rows start at or above their action's floor",
    not bad_rows,
    str(bad_rows),
)
check(
    "every playbook action is a known action",
    all(a in rules.KNOWN_ACTIONS for a, _ in sit.PLAYBOOK.values()),
    str([a for a, _ in sit.PLAYBOOK.values() if a not in rules.KNOWN_ACTIONS]),
)
check(
    "no playbook row proposes a never-list action",
    all(a not in rules.NEVER for a, _ in sit.PLAYBOOK.values()),
)


def situate(kind, **kw):
    return sit.decide_situation(
        sit.Situation(id="t", kind=kind, sender_email="x@y.example", subject="s", **kw)
    )


check(
    "an unknown situation kind escalates",
    situate("not_a_real_kind").final_lane == "ESCALATE",
)
check(
    "an unknown situation kind takes no action",
    situate("not_a_real_kind").action == "none",
)
for kind in sit.ALWAYS_ESCALATE:
    check(
        f"'{kind}' reaches a human",
        situate(kind).final_lane == "ESCALATE",
    )
check(
    "a tainted thread escalates on the proactive path",
    situate("unconfirmed_meeting", tainted=True).final_lane == "ESCALATE",
)

# The lateral-movement check. A thread blocked on the reactive path must not be
# able to earn autonomy by coming back round through the proactive one, however
# trusted its sender is.
lat_store = InMemoryStore()
for _ in range(100):
    memory.record(lat_store, "u", "x@y.example", "reply_scheduling", "accepted")
laundered = sit.decide_situation(
    sit.Situation(id="t", kind="unconfirmed_meeting", sender_email="x@y.example",
                  subject="s", tainted=True),
    store=lat_store, user_id="u",
)
check(
    "100 accepts cannot launder a tainted thread into autonomy",
    laundered.final_lane == "ESCALATE",
    laundered.final_lane,
)

# And the floor holds under calibration on this path too -- the ledger is shared
# between the two entry points, so proving it once on email is not enough.
sit_floor_violations = []
for kind, (action, _) in sit.PLAYBOOK.items():
    st = InMemoryStore()
    for _ in range(100):
        memory.record(st, "u", "x@y.example", action, "accepted")
    got = sit.decide_situation(
        sit.Situation(id="t", kind=kind, sender_email="x@y.example", subject="s"),
        store=st, user_id="u",
    )
    floor = rules.min_lane_for(got.action)
    if rules.more_cautious(got.final_lane, floor) != got.final_lane:
        sit_floor_violations.append((kind, got.final_lane, floor))
    if kind in sit.ALWAYS_ESCALATE and got.final_lane != "ESCALATE":
        sit_floor_violations.append((kind, got.final_lane, "ESCALATE"))
check(
    "no situation, after 100 accepts, can be learned below its floor",
    not sit_floor_violations,
    str(sit_floor_violations),
)
check(
    "the final action is always permitted by the final lane (situations)",
    all(
        rules.is_allowed(d.final_lane, d.action)
        for d in (situate(k) for k in list(sit.PLAYBOOK) + ["bogus"])
    ),
)


print()
if failures:
    print(f"{failures} of {total} check(s) failed.\n")
    raise SystemExit(1)
print(
    f"All {total} checks passed. "
    "The safety layer holds regardless of what the AI says.\n"
)
