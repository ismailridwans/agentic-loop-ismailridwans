# HARNESS.md

Every engineering decision in the reliability layer, and the specific failure each
one defends against.

A working loop is not a reliable one. Nothing in here exists because it's good
practice in general — each item is here because of a particular thing that goes wrong
when you run this against a real API for real.

Implementation: [agent/harness.py](agent/harness.py), [agent/llm.py](agent/llm.py),
[agent/logger.py](agent/logger.py), [agent/config.py](agent/config.py).

---

## The failure map

```
429 rate limit          ->  back off, honour retry-after
timeout / 5xx           ->  back off
unparseable JSON        ->  the repair ladder
bad request / auth      ->  fail fast; retrying can't help
tool raises             ->  ok=False observation, the loop adapts
memory down             ->  continue with no recall, log a warning
runs forever            ->  max_iterations
burns tokens            ->  budget
spins on one step       ->  stuck detection
fabricated quote        ->  quote verification against the source
```

---

## 1. Getting a usable answer out of the model

### Strict structured outputs

**Defends against:** malformed plans, at the source.

On Groq, `response_format: {"type": "json_schema", "strict": true}` uses constrained
decoding — the model *physically cannot* emit a token sequence that violates the
schema. So "reason returned garbage" stops being a failure mode I code around and
becomes one that can't occur.

*Why this and not JSON mode:* JSON Object Mode gives valid JSON with no guarantee it
matches my schema, which is the half I actually need. This is also why the model
choice is what it is — on Groq only `gpt-oss-120b` and `gpt-oss-20b` support strict
mode, so the model follows from the architecture rather than the other way round.

*The catch:* strict mode guarantees the **shape**, not the **sense**, and it only
holds for those two models. Which is why everything below still exists.

### The JSON repair ladder

**Defends against:** a swapped model, a schema 400, `strict_schema: false`, or
anything else that drops me out of constrained decoding.

Four rungs, cheapest first — [agent/llm.py:96](agent/llm.py:96):

| Rung | What | Cost |
|---|---|---|
| 1 | `json.loads` on the raw reply | free |
| 2 | `extract_json` — unwrap code fences, then brace-match a JSON object out of prose | free |
| 3 | `repair_prompt` — show the model its own bad output and the parse error, ask again | 1 call |
| 4 | `simplified_prompt` — strip the prompt to its first paragraph, ask for the minimum | 1 call |

*Why laddered and not "just retry":* the most common failure is the model wrapping a
perfectly good object in commentary. Rung 2 fixes that for zero tokens and zero
latency. Re-calling the API for something a regex solves is waste. Rungs 3 and 4
escalate only when the free options are exhausted, and they escalate *differently* —
3 gives the model more information, 4 gives it less to get wrong.

Brace matching rather than a greedy `{.*}`: a nested object would otherwise truncate
the scan at the first inner `}` — [harness.py:157](agent/harness.py:157).

### Hardcoded fallback plan

**Defends against:** the loop dying because one LLM call went bad.

If every rung fails, `_fallback_plan` ([loop.py:125](agent/loop.py:125)) returns a
deterministic plan chosen from the observation — read the next unread chunks, or
compare the top-ranked pair. No LLM involved.

*Why this and not raising:* a round that produces a mediocre action still makes
progress and still produces a report. A round that raises produces nothing. The
fallback is tagged in its `reasoning` field so it's obvious in the log that the model
didn't choose it.

### Schema validation of params before dispatch

**Defends against:** a plan that's schema-valid but references something that doesn't
exist.

`act` validates `params` against the tool's own JSON Schema *before* the handler runs
([loop.py:199](agent/loop.py:199)). A bad plan fails cheaply and legibly instead of
crashing three frames deep inside a handler.

---

## 2. Surviving the network

### Exponential backoff

**Defends against:** rate limits and temporary outages.

Doubling delay, capped at `max_delay_s` ([harness.py:83](agent/harness.py:83)).
*Why doubling:* hammering a busy service at a fixed interval makes it busier.

### Full jitter

**Defends against:** retries colliding with each other.

Delay is `uniform(0, ceiling)`, not the ceiling itself. If several calls fail at the
same moment and all retry after exactly 2s, they collide again and fail again.
Randomising spreads them out. *Why full jitter and not equal jitter:* for this call
volume the difference is academic, and full jitter is one line.

### Honouring `retry-after`

**Defends against:** guessing at a backoff when the server already told me the answer.

If the response carries a `retry-after` header, that value wins over my own maths
(capped at `max_delay_s` so a hostile header can't stall the run) —
[harness.py:116](agent/harness.py:116). Configurable via
`retry.honour_retry_after`.

### No retry on 400 / 401 / 403

**Defends against:** turning a fast failure into a slow one.

`classify` ([harness.py:35](agent/harness.py:35)) splits errors into `RETRYABLE` and
`FATAL`. A malformed request will be malformed the second time too, and a bad key
will still be bad. *Why not retry everything:* "retry everything" costs four attempts
and ~30 seconds to arrive at the same failure, and it hides the real cause behind
backoff noise.

---

## 3. Surviving the rest of the system

### Tool errors as observations, not exceptions

**Defends against:** a broken tool crashing the run instead of being routed around.

Every tool returns the same envelope — `{ok, tool, data, error, ms}`. A handler that
raises produces `ok: False`, which flows into `reflect` like any other result. The
agent gets to *react* to the failure: reflect sees it, and `next_instruction` sends
the next round somewhere else.

*Why this and not try/except at the top of the loop:* catching it centrally keeps the
run alive but tells the agent nothing. Making the failure an observation is what lets
it adapt.

### Memory failure degrades instead of stopping

**Defends against:** the vector store being unavailable killing an otherwise fine run.

`recall` is wrapped; on failure the round proceeds with no recalled context and logs
a warning ([loop.py:346](agent/loop.py:346)). The run gets worse — probably fewer
cross-section findings — but it finishes and says so.

---

## 4. Guardrails — checked once per round

### Max iterations

**Defends against:** running forever.

`before_round` returns `PARTIAL` past the limit
([harness.py:235](agent/harness.py:235)). *Why `PARTIAL` and not an error:* hitting
the ceiling isn't a failure, it's an incomplete search. The report is still real; it
just can't claim coverage. Nothing in this project ever says "there are only 2
contradictions", only "found 2".

### Token budget

**Defends against:** cost blowout.

Stops at `budget.max_tokens` with status `BUDGET_EXCEEDED`, and warns once at
`warn_at` so a long run gives notice before it dies. *Why a warn threshold at all:*
a budget that only speaks when it kills you is useless for tuning.

### Stuck detection

**Defends against:** spinning on the same step.

`fingerprint` hashes the parts of a reflection that indicate progress — `is_done`,
`next_instruction`, the finding id — and two identical fingerprints in a row ends the
run as `STUCK` ([harness.py:211](agent/harness.py:211)).

*Why those fields and not the whole object:* timestamps and token counts differ every
round, so hashing the whole reflection would never match and the guard would never
fire. Two rounds that differ only in *when* they happened have not made progress.

### Quote verification

**Defends against:** reporting a contradiction that isn't in the document.

Before a finding is reported, every quote is checked against the raw source text with
whitespace normalised ([harness.py:266](agent/harness.py:266)). Anything that doesn't
match is dropped.

*Why this is the one I'd keep if I could keep only one:* it's nearly free, and it
moves "the agent fabricated a quote" from *unlikely* to *structurally impossible*.
Everything else in this file makes the agent more robust; this one makes its output
checkable.

---

## 5. Configuration

### Everything in `config.yaml`

**Defends against:** not being able to change behaviour without editing the loop.

Precedence is **CLI flag > environment variable > `config.yaml` > default**
([agent/config.py](agent/config.py)). Model, temperature, iteration cap, focus size,
budget, retry policy, memory backend, recall `k`, log paths — all of it.

*Why that precedence order:* the yaml is the project's stance, env vars are the
machine's, and a CLI flag is what I'm doing right now. Most-specific wins.

### Structured JSONL logging

**Defends against:** not being able to explain what the agent did.

One line per step in `logs/run-{run_id}.jsonl`, fields truncated at
`max_field_chars` so a giant chunk of document doesn't drown the log.

*Why JSONL and not prose:* the run log is the evidence for every claim I make about
this project, and it needs to be greppable and diffable across runs, not readable
once.

### Fault injection

**Defends against:** only being able to *describe* failure handling.

`--inject-failure {rate_limit,bad_json,tool_error,memory_down}` forces a specific
failure for the first N opportunities ([harness.py:293](agent/harness.py:293)).

*Why this is in the shipped code and not a test fixture:* Groq's developer-plan
limits are 1K req/min and 250K tok/min, so a single-document run will never
organically hit a 429. Injection is the only way I can show the retry path working —
in the demo and in the suite — rather than asserting that it would.

---

## 6. Summary table

| Decision | Defends against |
|---|---|
| Strict structured outputs | Malformed plans, at the source |
| JSON repair ladder (4 rungs) | Losing strict mode for any reason |
| Hardcoded fallback plan | One bad LLM call killing the run |
| Param validation before dispatch | A valid-looking plan crashing inside a handler |
| Exponential backoff | Rate limits, temporary outages |
| Full jitter | Retries colliding with each other |
| Honouring `retry-after` | Guessing when the server already answered |
| No retry on 400/401/403 | A fast failure becoming a slow one |
| Tool errors as observations | A broken tool crashing instead of being routed around |
| Memory degrades, doesn't stop | The vector store taking down a fine run |
| Max iterations | Running forever |
| Token budget + warn threshold | Cost blowout, with notice |
| Stuck detection | Spinning on the same step |
| Quote verification | Reporting something not in the document |
| Everything in config | Changing behaviour requiring a code edit |
| Structured JSONL logs | Not being able to explain a run afterwards |
| Fault injection | Only being able to describe failure handling |

---

## 7. What has a test behind it, and what doesn't

A defence with a test behind it is a different claim from a defence that's only
described. All of it now lives in one file, [test/test_agent.py](test/test_agent.py)
— 16 tests, fast and offline, no API key needed:

| Behaviour | Test |
|---|---|
| 429 retries, 400 doesn't | `test_retries_a_429_but_never_a_400` |
| JSON dug out of prose / a code fence | `test_json_is_dug_out_of_prose` |
| the loop survives an unusable model | `test_the_loop_survives_an_unusable_model` |
| max-iterations, budget, stuck detection | `test_guardrails_stop_the_run_and_still_return_a_report` |
| a fabricated quote is dropped | `test_fabricated_quotes_are_dropped_from_the_report` |
| every step is logged with the required fields | `test_every_step_is_logged_with_the_required_fields` |
| config precedence, and defaults aren't mutated | `test_config_precedence_and_isolation` |

Not covered by a fast test: `retry_after_seconds` honouring Groq's header, and the
repair ladder's middle rungs (3 and 4) firing in order rather than jumping straight
to the rung-5 fallback. Both are exercised implicitly by `test_json_is_dug_out_of_prose`
and `test_the_loop_survives_an_unusable_model`, but not asserted rung-by-rung. Small
gap, listed here rather than left silent.
