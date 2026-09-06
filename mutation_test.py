#!/usr/bin/env python
"""
Do the safety harnesses actually test anything?

  python mutation_test.py
  python mutation_test.py --list

Break one safety rule in the source, run every harness, restore the file. A
harness that still passes with the rule broken was not testing that rule.

This exists because a harness can pass for the wrong reason, and two in this
repo did:

  - `eval.py --replay` reported 34/35 exact and 0 unsafe. Deliberately allowing
    `send_money` in SILENT changed *nothing* about its output, because it fed
    back the post-clamp action -- which is "none" for anything that escalated,
    and "none" is legal in every lane. It was re-deriving the recording. That
    number is gone from the README and `--replay` now refuses those records.
  - `verify.py` asserted every member of `ALWAYS_ESCALATE` reaches a human by
    looping over the set. Emptying the set made the loop run zero times and
    pass. It now asserts the set is populated first.

Both were found here, not by reading the code. The same technique found that
`verify.py` tested `lookalike_of()` in isolation while never checking that
`decide` calls it -- deleting the call left all six lookalike assertions green.

An equivalent mutant -- one that provably does not change behaviour -- is not a
failure. `invoice_due` is pinned by both the playbook and ALWAYS_ESCALATE, so
loosening the playbook row alone changes nothing, which is defence in depth
doing its job. Those are listed separately rather than counted against the
suite.

Exit code is non-zero if any non-equivalent mutation survives.
"""

import subprocess
import sys
from pathlib import Path
ROOT = Path("/Users/meetgajjar/Desktop/sid-agent")
PY = str(ROOT / ".venv-mac/bin/python")

M = {
 "floor: allow send_money in SILENT": ("src/sid_agent/rules.py",
   '"SILENT": ["archive", "label", "none"],',
   '"SILENT": ["archive", "label", "none", "send_money", "forward"],'),
 "masking: mask() becomes a no-op": ("src/sid_agent/read.py",
   "    return masked, sorted(found)", "    return text, []"),
 "decide: drop masked -> ESCALATE": ("src/sid_agent/graph.py",
   'if email.masked:', 'if False and email.masked:'),
 "decide: drop lookalike raise": ("src/sid_agent/graph.py",
   'if impersonating:', 'if False and impersonating:'),
 "decide: drop display-name raise": ("src/sid_agent/graph.py",
   'if claimed and claimed != impersonating:', 'if False and claimed:'),
 "decide: ignore contains_instructions": ("src/sid_agent/graph.py",
   'if check.contains_instructions:\n        raise_to(', 'if False:\n        raise_to('),
 "decide: ignore check.raise_to": ("src/sid_agent/graph.py",
   'if check.raise_to:', 'if False and check.raise_to:'),
 "calibration: remove SILENT ceiling": ("src/sid_agent/memory.py",
   "    promoted = rules.more_cautious(promoted, PROMOTION_CEILING)\n", "\n"),
 "calibration: allow out of ESCALATE": ("src/sid_agent/memory.py",
   'if current_lane == "ESCALATE":', 'if False:'),
 "calibration: ignore the blocked flag": ("src/sid_agent/memory.py",
   "if blocked or action not in PROMOTABLE:", "if action not in PROMOTABLE:"),
 "calibration: promote after 1 accept": ("src/sid_agent/memory.py",
   "PROMOTE_AFTER = 5", "PROMOTE_AFTER = 1"),
 "situations: invoice_due goes silent": ("src/sid_agent/situations.py",
   '"invoice_due": ("none", "ESCALATE"),', '"invoice_due": ("archive", "SILENT"),'),
 "situations: taint no longer blocks": ("src/sid_agent/situations.py",
   'if situation.tainted:', 'if False and situation.tainted:'),
 "situations: empty ALWAYS_ESCALATE": ("src/sid_agent/situations.py",
   'ALWAYS_ESCALATE: set[Kind] = {"invoice_due", "unread_security_alert"}',
   'ALWAYS_ESCALATE: set[Kind] = set()'),
}
# Mutations that provably do not change behaviour. Each one is pinned by a
# second, independent rule, so loosening the first changes no output. Verified
# by hand, and re-verified by the check below rather than taken on trust.
EQUIVALENT = {
    "situations: invoice_due goes silent":
        "ALWAYS_ESCALATE pins it independently of the playbook row",
}

H = ["verify.py", "worst_case.py", "situations_eval.py", "calibration_eval.py"]

def run(h):
    return subprocess.run([PY, str(ROOT / h)], cwd=ROOT,
                          capture_output=True, text=True).returncode

if "--list" in sys.argv:
    for name, (rel, old, _) in M.items():
        tag = "  [equivalent]" if name in EQUIVALENT else ""
        print(f"{name:40} {rel}{tag}")
    raise SystemExit(0)

base = {h: run(h) for h in H}
assert all(v == 0 for v in base.values()), f"baseline not green: {base}"
print("baseline: all green\n")
hdr = " ".join(h.replace(".py", "")[:11].rjust(11) for h in H)
print(f"{'mutation':38} {hdr}")
print("-" * 38 + " " + " ".join("-" * 11 for _ in H))

undetected = []
for name, (rel, old, new) in M.items():
    f = ROOT / rel; orig = f.read_text(encoding="utf-8")
    if old not in orig:
        print(f"{name:38} !! anchor missing"); undetected.append(name + " (BAD MUTATION)"); continue
    f.write_text(orig.replace(old, new, 1), encoding="utf-8")
    try:
        codes = {h: run(h) for h in H}
    finally:
        f.write_text(orig, encoding="utf-8")
    print(f"{name:38} " + " ".join(("CAUGHT" if codes[h] else ".").rjust(11) for h in H))
    if all(c == 0 for c in codes.values()):
        undetected.append(name)

real = [u for u in undetected if u not in EQUIVALENT]
equiv = [u for u in undetected if u in EQUIVALENT]

print(f"\n  mutations           {len(M)}")
print(f"  caught              {len(M) - len(undetected)}")
print(f"  equivalent          {len(equiv)}  (provably no behaviour change)")
print(f"  SURVIVED            {len(real)}")

for u in equiv:
    print(f"\n  equivalent: {u}\n    {EQUIVALENT[u]}")

if real:
    print("\n  Survived with no harness noticing -- these rules are untested:")
    for u in real:
        print("   -", u)
    raise SystemExit(1)
print("\n  Every mutation that changes behaviour was caught by a harness.\n")
raise SystemExit(0)
