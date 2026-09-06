# sid-agent

An email triage agent that sorts mail into four lanes — and a harness that
measures whether it does so *safely*.

It runs entirely offline against a labeled corpus. No mailbox is connected, and
it executes nothing. Gmail is an optional extra at the very end, not a
prerequisite.

## Results

Measured on 35 labeled emails, 8 of them adversarial. Fully throttled run,
zero degraded model calls.

| | |
|---|---|
| Exact lane | **34/35 (97%)** |
| Over-cautious | 1 *(safe, just noisy)* |
| **Unsafe misses** | **0** *(nothing placed in a laxer lane than it deserved)* |
| Attacks contained | **8/8** |
| Safety checks | **42 passing**, no API key or network required |

All four lanes are live. Reproduce with `python eval.py`, or see the committed
run with `python dashboard.py --golden` (needs nothing at all).

Design decisions and the bugs the harness caught: **[DESIGN.md](DESIGN.md)**.

## The four lanes

| Lane | Means |
|---|---|
| `SILENT` | handle it, say nothing |
| `NOTIFY` | handle it, tell me afterwards |
| `ASK` | stop, ask me, then do what I say |
| `ESCALATE` | touch nothing, just put it in front of me |

## How it works

Built on LangGraph, with LangChain for the model calls and LangSmith for
tracing and evaluation.

```
                    START
                      |
                    sort          <- LangChain, structured output. no tools.
                      |
                    check         <- second opinion. can only raise a lane.
                      |
                    decide        <- plain Python. no model. the safety layer.
                      |
                   calibrate      <- LangGraph Store. the learned trust ledger.
                      |
        +-------------+-------------+-------------+
        |             |             |             |
   lane_silent   lane_notify    lane_ask    lane_escalate
                                    |
                              interrupt()   <- human in the loop
```

Masking (`read.py`) runs before any of this, on the way in — OTPs and card
numbers are stripped by regex so the model never sees them at all.

There is a second entry point. Not everything worth acting on arrives as a
message: nobody emails you to say you have not replied to your manager in four
days. [`situations.py`](src/sid_agent/situations.py) derives those from mailbox
state and sends them through the same four-lane decision and the same floor —
with **no model call at all**, because a situation is built from our own
metadata and so has no prompt to inject.

| Framework piece | Where |
|---|---|
| `StateGraph` + conditional edges | four real branches, one per lane |
| `Store` | [`memory.py`](src/sid_agent/memory.py) — the per-sender trust ledger |
| checkpointer | pause/resume, so `interrupt()` works |
| `interrupt()` | the ASK lane stops and waits for you |
| `init_chat_model` + `with_structured_output` | [`sort.py`](src/sid_agent/sort.py), [`check.py`](src/sid_agent/check.py) |
| LangSmith `evaluate()` | [`langsmith_eval.py`](langsmith_eval.py) |
| `langgraph.json` | run it in Studio with `langgraph dev` |

**The one rule that makes it safe:** the AI only ever *suggests* a lane.
[`rules.py`](src/sid_agent/rules.py) decides what that lane may do, and every
path through the code moves an email to a *more* cautious lane — never a less
cautious one. The worst a malicious email can achieve is being escalated.

## Setup

```bash
python -m venv .venv

source .venv/bin/activate      # macOS / Linux
.venv\Scripts\activate         # Windows

pip install -r requirements.txt
cp .env.example .env           # Windows: copy .env.example .env
```

You need a model key for steps 2 and 3. **Free options, no card required:**

- **`google_genai:gemini-3.5-flash-lite`** — the model the reported numbers
  were measured on. Free key from
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey). This is
  Google AI Studio, which is *not* Google Cloud Console — no billing account,
  no project setup, just a key.
- **`ollama:llama3.2`** — fully local and offline. Install Ollama, then
  `ollama pull llama3.2`. No key at all.

Set `SID_MODEL` in `.env` to whichever you use, plus `SID_USER_NAME` and
`SID_USER_CONTEXT` — the sorter uses those to judge what is relevant to you.

## Run it

**Prove the safety layer holds.** No API key, no network, no model:

```bash
python verify.py
```

42 checks including an exhaustive sweep of all 64 lane×action combinations,
confirming nothing can ever reach a laxer lane than it deserves.

**Score the agent against the labeled corpus:**

```bash
python eval.py                 # all 35 emails
python eval.py --adversarial   # just the 8 attack cases
python eval.py --errors        # just the mistakes
```

**See four worked examples**, one per lane, with every step traced:

```bash
python examples.py
```

**Open the dashboard** — metrics, lane distribution, confusion, safety activity:

```bash
python dashboard.py
python dashboard.py --watch
```

**Measure the calibration** — does it actually ask less over time? No API key,
no network, no model call: it replays the recorded run through the real
`_calibrate_node` and a throwaway store, so it is deterministic and reproducible
by anyone.

```bash
python calibration_eval.py            # the round-by-round curve
python calibration_eval.py --seeds 25 # is that just a lucky seed?
python calibration_eval.py --json
```

**Score the proactive path** — the situations the agent notices on its own, with
no incoming message to react to. Also needs nothing:

```bash
python situations_eval.py
```

**See the calibration work** — how a sender earns autonomy, and what can never
be learned. No API key needed:

```bash
python hitl.py --demo
```

**Open it in LangGraph Studio:**

```bash
langgraph dev
```

**Run it as a LangSmith experiment** (free tier, no card — smith.langchain.com):

```bash
python langsmith_eval.py --upload
python langsmith_eval.py
```

Same scoring as `eval.py`, but every run is traced — so when it gets one wrong
you can click into the exact sort/check reasoning that produced it.

## The metric that matters

`eval.py` reports three numbers, and they are not equally important:

- **Exact** — how often it picked the right lane.
- **Over-cautious** — it escalated something routine. Annoying. Costs a glance.
- **UNSAFE MISSES** — it put an email in a *laxer* lane than it deserved. This
  is the only number that can actually hurt you, and the target is zero.

Useful baseline: with no model configured at all, every email fails safe to
`ESCALATE`. That scores **49% exact, 0 unsafe, 8/8 adversarial contained** —
perfectly safe and completely useless. The job of the model is to raise *exact*
while holding *unsafe* at zero. Any change that improves accuracy by trading
away an unsafe miss is a regression.

## The corpus

[`data/emails.json`](data/emails.json) — 35 labeled emails. 7 silent, 4 notify,
7 ask, 10 escalate, plus 8 adversarial cases:

- direct injection asking to be archived silently and forwarded out
- injection hidden inside an ordinary newsletter
- instruction hidden in an HTML comment
- fake `SYSTEM:` authority claim requesting a forwarding rule
- credential phishing, advance-fee fraud, payment-redirect fraud
- lookalike-domain request for the client list

Add your own entries — `eval.py` picks them up automatically. Growing this
corpus *is* the project.

## Files

| File | What it is |
|---|---|
| [`src/sid_agent/rules.py`](src/sid_agent/rules.py) | **the safety model.** read this one first |
| [`src/sid_agent/read.py`](src/sid_agent/read.py) | masking. regex, runs before the AI |
| [`src/sid_agent/sort.py`](src/sid_agent/sort.py) | step 2, the AI's suggestion |
| [`src/sid_agent/check.py`](src/sid_agent/check.py) | step 3, the second opinion |
| [`src/sid_agent/graph.py`](src/sid_agent/graph.py) | the LangGraph graph + the deterministic `decide` step |
| [`src/sid_agent/memory.py`](src/sid_agent/memory.py) | the calibration ledger, persisted across runs |
| [`src/sid_agent/domains.py`](src/sid_agent/domains.py) | lookalike sender detection, deterministic |
| [`DESIGN.md`](DESIGN.md) | key decisions, and the bugs the harness caught |
| [`data/emails.json`](data/emails.json) | the labeled corpus |
| [`src/sid_agent/situations.py`](src/sid_agent/situations.py) | the proactive path. no model call anywhere in it |
| [`eval.py`](eval.py) | scores the agent, separates unsafe from merely noisy |
| [`calibration_eval.py`](calibration_eval.py) | measures whether it asks less over time. no API key |
| [`situations_eval.py`](situations_eval.py) | scores the proactive path. no API key |
| [`docs/TRANSCRIPTS.md`](docs/TRANSCRIPTS.md) | four worked examples, one per lane, committed output |
| [`langsmith_eval.py`](langsmith_eval.py) | the same, as a traced LangSmith experiment |
| [`hitl.py`](hitl.py) | the human-in-the-loop lane and the calibration demo |
| [`verify.py`](verify.py) | proves the safety properties, no API key needed |
| [`examples.py`](examples.py) | four worked examples, one per lane |
| [`dashboard.py`](dashboard.py) | the run dashboard |

## A note on rate limits

Free-tier quotas are strict, and a throttled model call does not raise an error
you notice — it fails safe to `ESCALATE`. On a corpus where half the labels are
`ESCALATE`, that quietly *inflates* accuracy and makes attack containment look
perfect when nothing was actually caught.

This is not hypothetical: a fast unthrottled run scored 80% with 0 unsafe misses
and 8/8 attacks contained. The same corpus, throttled properly, scored 86% with
**3 unsafe misses and 5/8 contained** — and those three exposed a real bug in
`decide`. The rate limiter in `sort.py` and the degraded-run warning in
`eval.py` both exist because of that.

If `eval.py` warns about failed calls, the score is not real. Lower `SID_RPS`
in `.env` and run it again.

## What comes next

1. **Get exact accuracy up** while unsafe misses stay at 0. Tune the prompts in
   `sort.py`, and add corpus entries wherever it guesses wrong.
2. **Grow the adversarial set.** Every new attack shape you can think of is a
   permanent regression test.
3. **Wire the lanes to real actions** — lane 1 first (archive/label only), then
   lane 3 via the `interrupt()` that is already in the graph, then lane 2 last.
