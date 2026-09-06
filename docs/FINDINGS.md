# Findings

Every bug the harnesses caught, in the order they were caught, with the numbers
that exposed them. Split out of DESIGN.md so that file can stay short.

This is the most useful document in the repo for judging whether the safety
argument is real. A design doc tells you what someone intended. This tells you
what actually happened when it was tested, including the two occasions a fix
made things worse.

The through-line, stated once so it does not have to be repeated below:
**every one of these was found by measurement, and none by reading the code.**
Three were invisible to a test suite that swept each component in isolation,
because they lived in the seam between two components that were each correct.

---

## 1. A throttled run scored better than a working one

A throttled model call does not raise an error the caller notices — it fails
safe to `ESCALATE`. On a corpus where half the labels *are* `ESCALATE`, failures
masquerade as correct answers and inflate the score.

This bit us. Measured on the same corpus, same code:

| run | exact | unsafe | attacks contained |
|---|---|---|---|
| unthrottled, 14/35 calls rate-limited | 80% | 0 | 8/8 |
| throttled properly | 86% | **3** | **5/8** |

The first run's headline numbers were better *and* completely wrong. The three
"contained" attacks were escalating by accident. `sort.py` now carries an
`InMemoryRateLimiter` with backoff retry, and `eval.py` refuses to present a
score without warning when any call failed.

---

## 2. Escalation that only fired from SILENT

Those three unsafe misses were all injections landing in `ASK` instead of
`ESCALATE`. The check had correctly flagged every one with
`contains_instructions: true`. The bug was in our own rule:

```python
if check.contains_instructions:
    if lane == "SILENT": raise_to("ESCALATE", ...)
    else:                raise_to("ASK", ...)      # too lenient
```

The escalation only fired if the sorter had already said `SILENT`. Anywhere
above that, agent-directed text merely nudged the email to `ASK`. Since an email
addressing the agent is never grounds for autonomous action *whatever lane it
starts in*, the condition is gone — `contains_instructions` now escalates
unconditionally, and `verify.py` tests all four starting lanes.

Result: unsafe misses 3 → 1, attacks contained 5/8 → 8/8, exact 86% → 89%.

---

## 3. Fixing that killed the NOTIFY lane, and let a spoof through

At 89%, the `NOTIFY` lane was dead — 0 of 35 emails reached it. One of the four
lanes in the brief never fired, because `check` was flagging "a named real
person" and "a direct request" as stakes and pushing every scheduling email to
`ASK`.

Loosening the check to judge *stakes* rather than *tone* revived the lane
(0 → 3 correct) and lifted exact to 91%. It also let a spoof through:

```
d.okafor@knowncompany-support.example   ->  ASK   (expected ESCALATE)
```

The model reasoned about the content perfectly — *"sharing sensitive company
data to a personal address requires explicit permission"* — and still missed
that the domain was impersonating `knowncompany.example`. A good impersonation
has innocent tone and plausible content, which is exactly what a tone-based
check cannot catch.

So the fix went in the deterministic layer instead (`domains.py`): compare the
sender's domain against a configured list of domains you actually deal with, and
escalate anything close-but-not-equal. Suffix wrapping
(`knowncompany-support`), typos (`knowncompanny`), bounded edit distance. Real
subdomains and genuinely unrelated domains are left alone.

**Final: 97% exact, 0 unsafe misses, 8/8 attacks contained, all four lanes live.**

The lesson worth stating: when the model cannot be relied on to be suspicious,
encode the suspicion in code that cannot be talked out of it. Two prompt fixes
each traded one failure for another. The structural fix did not.

---

## 4. Calibration forgot everything on exit

The ledger was on `InMemoryStore`, which meant the agent learned during a run
and started from zero on the next one. That is not learning.

`memory.FileStore` persists to `logs/trust.json` on every write. Verified:
a streak of 5 written in one process loads in a fresh one and promotes
`ASK → NOTIFY`. Demos (`--demo`, `--earn`) deliberately use a throwaway store so
they stay reproducible; the interactive mode persists.

---

## 5. An adversarial review, and the four holes it found

Everything above was found by our own harness. That harness was written by the
same person who wrote the code, which is a known blind spot, so the safety layer
was handed to an independent reviewer with one instruction: break the invariant.

The reviewer confirmed the core claim — no path through `decide` produces a
laxer lane, across malformed actions, malformed lanes, unicode whitespace,
empty strings and every ordering of the rules. `decide` held. Four things
around it did not.

**1. `domains.py` skipped the most common spoof there is.** The loop read:

```python
if not good_core or core == good_core:
    continue          # "same name, must be fine"
```

So `knowncompany.com` against a trusted `knowncompany.example` was classified
*not a lookalike* — the registrable name reused under a suffix we never
trusted. Same body, same sorter output, only the domain shape changed:

| sender | result |
|---|---|
| `d.okafor@knowncompany-support.example` | ESCALATE ✓ |
| `d.okafor@knowncompany.com` | **ASK — unsafe miss** |

Also missed: the trusted name pushed into a subdomain label
(`knowncompany.example.evil.com`), a trailing DNS root dot defeating the whole
check (`knowncompany.example.`), and a typo budget that did not scale with name
length. All four now caught, with regressions for each.

The uncomfortable part is that `verify.py` had six lookalike assertions and all
six passed, because they tested the two shapes that worked.

**2. Calibration could learn into `SILENT`.** §3 above claimed it could not.
That was false. `archive` and `label` have floor `SILENT`, so a `NOTIFY`
decision could be promoted to "handle it and say nothing" — the exact failure
§5 is built around. Promotion is now capped at `NOTIFY` regardless of floor.

The test that should have caught it, `"promotion never reaches SILENT"`, used
`reply_scheduling` — whose floor is *already* `NOTIFY`. It could not fail no
matter what the code did. **A test that cannot fail is worse than no test: it
occupies the space where a real one would have gone.** It now sweeps every
promotable action.

**3. Calibration could soften an `ESCALATE`.** When a lane rises, the action is
clamped to `none` — and `none` has floor `SILENT`, so an escalation looked
freely promotable. A farmed streak dropped escalations one step to `ASK`. The
reviewer measured 143 such drops across the state space.

`ESCALATE` is now terminal for the ledger. It is a verdict, not an opening bid:
something reached it because a rule fired, and unrelated good behaviour from
the same sender is not evidence against that specific rule.

**4. `blocked` was under-computed** — the bug that made 3 reachable. It read
`bool(d.masked) or d.contains_instructions`, so a lookalike domain, a
never-list action, an unknown action and a raise from `check` all left it
`False`. Trust could undo the very rule that had just fired.

The through-line: **sweeping `decide` alone and `earned_lane` alone both
passed while the seam between them leaked.** `verify.py` now sweeps the
composed `decide → calibrate` path, which is where three of the four lived.

---

## 6. That fix over-corrected, and the measurement caught it

The first fix for hole 4 was `blocked = ... or bool(d.raises)` — if the rules
intervened at all, learning does not get to undo it.

It was too blunt, and `calibration_eval.py` said so immediately: the measured
fall in ask rate went to **zero**. Nothing could ever be promoted again.

The reason is that not all raises mean the same thing. Two kinds:

| raise | what it tells you |
|---|---|
| `'schedule' is not permitted in NOTIFY` | **mechanical.** The model picked a lane its own action isn't allowed in. The floor corrected it. Says nothing about the email. |
| `sender domain imitates 'knowncompany.example'` | **a hazard.** A judgement about *this* email. |

The first is the single most common raise in the corpus. Blocking on it froze
calibration outright while protecting nothing.

`Decision` now carries `hazards` alongside `raises`, and only hazards block the
ledger. `important_signals` is recorded as a hazard *even when it did not raise
the lane* — the signal rule only fires out of `SILENT`, so an `ASK` email marked
"payment due" produced no raise at all and was freely promotable to `NOTIFY`.
Whether a signal moved the lane and whether it should block learning are two
different questions, and conflating them is what let that through.

Worth stating plainly: this over-correction was caught by the calibration
measurement, not by a safety test. Both suites passed the whole time. Building
the thing that measures the *feature* is what surfaced a bug in the *fix*.

---

## 7. Masking was thinner than it looked

The reviewer swept 35 realistic secret shapes past `read.py`. **24 went
through untouched.** A miss here is worse than it appears: the model sees the
secret *and* the automatic `masked → ESCALATE` raise never fires, leaving only
the model choosing to be careful — the exact dependency §4 exists to remove.

The causes were structural, not a missing pattern here and there:

- Every keyword window was `[^\n]{0,40}`, so it could not cross a line break.
  `"Your code:\n839214"` — the shape most providers actually send — matched
  nothing.
- `\b(\d{4,8})\b` cannot anchor inside a longer digit run, so a 9-digit code
  was invisible.
- No normalisation, so a zero-width space inside the keyword
  (`verifica​tion code`) was a free bypass.

`clean()` now runs NFKC and strips invisible characters before any pattern
sees the text; windows cross newlines; digit runs tolerate separators; and the
keyword and token lists cover PIN/MFA, pass phrases, AWS keys, Google keys,
JWTs, SSNs and IBANs.

Bare `"code"` is still deliberately excluded. **Over-masking is not free** —
masked content forces an `ESCALATE`, so a false positive costs the user a
pointless glance. It is admitted only as `"your code"`, `"code:"` and
`"code is"`. The first cut of that matched `"the codebase"`. Both directions
are now tested: 19 shapes that must mask, 7 ordinary emails that must not.

---

## 8. Two harnesses were passing for the wrong reason

Everything above was found by a harness. This one was found by asking whether
the harnesses could fail at all.

`mutation_test.py` breaks one safety rule in the source, runs every harness,
and restores the file. A harness that still passes with the rule broken was not
testing that rule. Fourteen mutations; two survivors that should not have.

**`eval.py --replay` was measuring nothing.** It reported *34/35 exact, 0 unsafe
misses* and was quoted in the README as lane accuracy. Allowing `send_money` in
the `SILENT` lane changed **0 of 35** outcomes.

The cause is worth stating exactly, because it is subtle and it is a data-model
bug rather than a logic one. Replay reconstructed the model's suggestion from
the recorded decision — but the log stored only the **post-clamp** action. Any
email that escalated had its action rewritten to `none` before it was written
out, and `none` is permitted in every lane. So the replay handed `decide` an
action that no rule could object to, and got back the lane it started with.
It re-derived the recording.

`Decision` now records `proposed_action` and `check_raise_to` — what the model
actually said, before the lane touched it. Records without those fields are
refused by `--replay` rather than silently producing a number, and the 97% is
gone from the README. It is quoted once, in a section that says plainly that it
is stale and that the 14 newest adversarial emails have never been scored.

**`verify.py` had a check that ran zero times.** It asserted every member of
`ALWAYS_ESCALATE` reaches a human by iterating the set. Emptying the set made
the loop iterate nothing and pass. It now asserts the set is populated first.

A third finding, less dramatic but the same shape: `verify.py` had six
assertions about `lookalike_of()` and none about whether `decide` *calls* it.
Deleting the call from `_decide_node` left all six green. There are now
end-to-end checks for the lookalike, display-name and masking raises — testing
a function and testing its wiring are different tests.

Final state: 14 mutations, 13 caught, 1 equivalent. The equivalent one loosens
the `invoice_due` playbook row, which changes no behaviour because
`ALWAYS_ESCALATE` pins that situation independently — verified by running it,
not assumed. Equivalent mutants are reported separately rather than counted as
passes.

**The general lesson.** The two bad harnesses were both *green* and both
*specific* — they printed real-looking numbers about real properties. Nothing
about reading them suggested a problem. What exposed them was the only question
that reliably does: *make the thing they check untrue, and see if they notice.*
