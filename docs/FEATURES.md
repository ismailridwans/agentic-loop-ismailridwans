# Features — Milestone 1, 2, 3

What I actually built, milestone by milestone. Code isn't pasted here — I'll show
that live from the files. This is the map: which feature, which file, why it
exists.

**Use case:** detect contradictions inside a document.

---

## Milestone 1 — The Core Loop

### Feature 1 — Four functions, exact signatures

| Function | Signature | LLM call? |
|---|---|---|
| `perceive` | `(input_data: str) -> dict` | No |
| `reason` | `(observation: dict, memory: list) -> dict` | Yes |
| `act` | `(plan: dict, tools: dict) -> dict` | No |
| `reflect` | `(result: dict, observation: dict) -> dict` | Yes |

**File:** `agent/loop.py`

**What each one does:**

- **`perceive`** — splits the document into numbered chunks, records exactly where
  each chunk starts and ends in the raw text, tracks what's been read, ranks which
  claim pairs are worth comparing next. No LLM call — it's plain Python.
- **`reason`** — the one LLM call per round. Sees where we are, what's in memory,
  the tool list. Picks exactly one tool and says why.
- **`act`** — dispatch only. Validates the plan's parameters against the tool's
  schema before running it, so a bad plan fails cheaply instead of crashing.
- **`reflect`** — records a finding if one was produced, decides whether the run is
  done, and writes the instruction for the next round.

**The feedback loop:** `perceive` only accepts a string, so `reflect`'s output has
to be serialized to get back in. I wrap it as a small JSON object —
`{run_id, iteration, instruction}` — and that becomes the next round's input. This
is the actual mechanism that makes it a loop instead of four functions called once
each.

**Why a loop instead of one prompt:** a document with 23 chunks has around 20
claims. Comparing all of them pairwise is ~200 comparisons — too many for one
prompt, and most of those pairs aren't worth checking. So each round either reads
more of the document or tests one promising pair, then decides what to do next.
That decision is the reason the loop exists.

---

### Feature 2 — Three tools

**File:** `agent/tools.py`

| Tool | What it does | Backed by |
|---|---|---|
| `extract_claims` | Reads chunks, pulls out checkable statements | LLM (fast model) |
| `find_related_claims` | Finds claims about the same subject as a given one | Plain Python |
| `compare_claims` | Decides if two claims conflict | LLM + Python |

Each tool is defined as a JSON Schema dict — `name`, `description`, `parameters` —
and has a matching handler function. The handler always returns the same shape:
`{ok, tool, data, error, ms}`, whether it succeeded or not.

**The one design decision worth explaining:** inside `compare_claims`, numbers
are normalized and compared in **Python**, not by the model. `"30 days"` and
`"720 hours"` both become `2,592,000 seconds` and get compared with `==`. The
model only ever answers the language question — are these two statements about
the same thing? — and is handed the arithmetic result as a fact. This matters
because arithmetic can prove two values are equal with certainty, but it can
never prove a contradiction on its own — two different numbers might just be two
different subjects (a free tier and a paid tier, for example).

---

### Feature 3 — Anti-hallucination at extraction time

**File:** `agent/tools.py` — the `locate()` function

The model is asked to copy a sentence verbatim (`quote`) rather than give
character positions, because it can't count characters reliably. My code then
searches for that quote inside the actual chunk text. If it's found, the claim's
`text` and `position` are taken from the **real source**, not from what the model
wrote — so small differences (a comma the model wrote as a period) don't matter,
because we always store the source's own text.

If the quote can't be found anywhere in the chunk at all, the claim is dropped and
counted separately (`claims_dropped_unverifiable`). This is where a fabricated
statement dies before it ever becomes a claim.

---

### Feature 4 — Deterministic claim ranking

**File:** `agent/tools.py` — `relatedness()` and `candidate_pairs()`

Before any pair of claims is sent to the model for comparison, it's scored: shared
words in the subject (weighted highest), same claim type, same unit family. Only
pairs above a threshold are ever offered to `reason` as candidates. This is what
keeps the search space small instead of trying every possible pair.

---

### Feature 5 — Precision guards (avoiding false positives)

**File:** `agent/prompts.py` — the `GUARDS` block, used in `COMPARE_SYSTEM`

An LLM asked to "find contradictions" will flag things that aren't actually
contradictions. So the compare prompt includes four named guards the model has to
check before it's allowed to say "contradiction":

| Guard | Example it must reject |
|---|---|
| `different_things` | "Free: 5 GB" vs "Pro: 50 GB" — different tiers |
| `superseded` | "Until 2024, X. From 2024, Y." — one replaced the other |
| `stated_exception` | a rule plus its own declared carve-out |
| `same_value` | "10 MB" vs "10,485,760 bytes" — same number, different units |

My test document (`test/data/policy.md`) has 3 real contradictions and 2 traps
built from these guards, recorded in `test/data/expected.yaml`. The traps are the
real test — anyone can find contradictions by flagging everything.

---

### Feature 6 — LLM choice: Groq, strict structured output

**File:** `agent/llm.py`, `config.yaml`

Provider is Groq only, called through its OpenAI-compatible endpoint — nothing else
is used anywhere in this project. Currently both jobs (`reason`/`compare_claims` and
`extract_claims`) run on `openai/gpt-oss-20b`; the design was a two-model split with
`gpt-oss-120b` on the reasoning calls, but 120b's free-tier daily quota ran out
during testing, so both fields point at 20b for now — one line in `config.yaml` to
switch back.

The reason for using `gpt-oss` models specifically: Groq only supports **strict
structured outputs** — where the model is constrained at the token level and
literally cannot emit JSON that violates the schema — on those models. Since
`reason` has to return a parseable plan every round for the loop to function,
that constraint decided the model choice.

---

## Milestone 2 — Memory

### Feature 7 — `save()` / `recall()` / `clear()`

**File:** `agent/memory_manager.py`

Three functions, exactly as required. `recall()` returns a `list`, which matches
`reason(observation, memory: list)` directly.

Backend: **ChromaDB**, running locally. The reason: the operation the agent needs
most — "find claims similar to this one" — is a vector search. Chroma isn't
bolted on to satisfy the requirement; it's the actual search engine behind
`find_related_claims` and the candidate-pair ranking.

### Feature 8 — Three namespaces

| Namespace | Holds |
|---|---|
| `claims` | every statement found so far |
| `findings` | confirmed contradictions |
| `rejected` | pairs already checked and cleared |

`rejected` is the one worth explaining: once two claims are judged *not* to
conflict, that pair is remembered. It's never re-sent to the model, and the same
false positive can't reappear on a later run.

### Feature 9 — Wired into the loop at the exact two points required

**File:** `agent/loop.py` — inside `run_loop`

```
recall()  →  before reason()
save()    →  after reflect()
```

Recalled text is placed directly into the prompt `reason` sends to the model —
not just fetched and left unused.

### Feature 10 — Why memory isn't optional for this use case

A contradiction is, by definition, two statements in *different places*. Without
memory, each round starts blind — it reads a chunk, extracts claims, and has
nothing to compare them against, because the previous round's work is gone. This
is the argument I'd lead with if asked why memory matters here specifically,
rather than "memory generally helps agents."

### Feature 11 — The ablation

```bash
python main.py --doc test/data/policy.md
python main.py --doc test/data/policy.md --no-memory
```

Same document, same config. With memory: cross-section findings appear. Without:
they don't, because nothing from an earlier round is available to compare
against.

---

## Milestone 3 — Harness

### Feature 12 — Retry with backoff, per error class

**File:** `agent/harness.py` — `classify()`, `backoff_delay()`, `call_with_retry()`

| Error | Retried? | Why |
|---|---|---|
| 429 (rate limit) | Yes — exponential backoff + jitter | Temporary |
| 5xx / timeout | Yes | Probably temporary |
| 400 (bad request) | No | Will fail the same way again |
| 401 (auth) | No | Retrying won't fix a bad key |
| 429 daily quota | No | Backing off for seconds won't fix an hours-long reset |

That last row is a real thing I ran into: Groq gives both burst limits and daily
limits as the same HTTP 429. `is_quota_exhausted()` tells them apart by reading
the error message, because retrying a daily quota with a 30-second backoff just
wastes four attempts before failing anyway.

Backoff also honors Groq's own `retry-after` header when it's present, instead of
only trusting my own calculated delay.

### Feature 13 — The JSON repair ladder

**File:** `agent/harness.py` (`extract_json`, `repair_prompt`, `simplified_prompt`)
and `agent/llm.py` (where the rungs are actually called in sequence)

1. Parse the reply directly.
2. Dig JSON out of surrounding text (the model wrapped it in commentary or a code
   fence).
3. Send the bad output back to the model along with the validation error, and ask
   again.
4. Ask again with a shorter, simplified prompt.
5. Give up on the model for this round and fall back to a fixed, hardcoded plan
   (in `loop.py`, `_fallback_plan`) so the loop keeps running.

With Groq's strict mode, the model almost never reaches rung 2 — but the ladder
still exists because strict mode guarantees the *shape* is valid, not that every
field makes semantic sense, and because the model is configurable (someone could
run this against a model without strict-mode support).

### Feature 14 — Guardrails

**File:** `agent/harness.py` — `Guardrails` class

- **Max iterations** — hard cap, from `config.yaml`, currently 12.
- **Token budget** — warns at 100k, stops at 150k.
- **Stuck detection** — if two rounds in a row make literally zero progress
  (same scanned count, same claim count, same instruction), the run stops with
  status `STUCK` instead of spinning. Progress counters are part of the check —
  not just repeated wording — because "keep reading the next chunks" is the
  *correct* instruction for several rounds in a row while the document is still
  being read.

### Feature 15 — Quote verification (anti-hallucination, second layer)

**File:** `agent/harness.py` — `verify_quotes()`

Before the final report is built, every finding's quotes are checked against the
raw document text one more time. Anything that doesn't match exactly is dropped
and counted separately. This is on top of the extraction-time check in Feature 3
— one check at the point a claim is created, one at the point a finding is
reported.

### Feature 16 — Structured logging

**File:** `agent/logger.py`

One JSON line per step to `logs/run-<id>.jsonl`: timestamp, iteration, step name,
input summary, output summary, latency in ms, and any error. The same events also
print to the console, so what you see on screen and what's in the log file can
never disagree.

### Feature 17 — Fault injection

**File:** `agent/harness.py` — `Faults` class

```bash
python main.py --inject-failure rate_limit
python main.py --inject-failure bad_json
python main.py --inject-failure tool_error
python main.py --inject-failure memory_down
```

Forces a specific failure on demand, so the harness handling it can be
**demonstrated**, not just described. Useful for the demo video's required
"failure handled gracefully" segment.

### Feature 18 — Every run returns a status and a report

| Status | Meaning |
|---|---|
| `COMPLETE` | finished on its own |
| `PARTIAL` | hit the iteration cap |
| `STUCK` | two rounds made zero progress |
| `BUDGET_EXCEEDED` | token budget or daily quota exhausted |
| `FAILED` | something genuinely unrecoverable |

Whatever the status, `run_loop` always returns a report with whatever claims and
findings were found up to that point — never an empty result, never an unhandled
exception reaching `main.py`.

### Feature 19 — Everything in `config.yaml`

**File:** `config.yaml`, `agent/config.py`

Model names, iteration cap, token budget, retry settings, memory backend — nothing
is hardcoded in the loop. Precedence is CLI flag > environment variable >
`config.yaml` > built-in default.

---

## Quick reference — what to say if asked "what did you build"

> Milestone 1: a loop with four functions matching the required signatures, three
> tools, and a document-checking use case where I split the arithmetic (done in
> Python, with certainty) from the language judgment (done by the model).
>
> Milestone 2: ChromaDB memory with three namespaces, read before reasoning and
> written after reflecting, and I can prove it matters with a `--no-memory` flag
> that removes cross-section findings.
>
> Milestone 3: per-error-class retry, a five-step fallback ladder for bad model
> output, three guardrails, quote verification against the source, structured
> JSONL logging, and fault injection so I can force and show each failure mode
> instead of just describing it.
