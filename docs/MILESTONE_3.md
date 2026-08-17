# Milestone 3 — The Harness

**Goal:** the loop keeps running when things go wrong, and I can see what it did.

Read [MILESTONE_1.md](MILESTONE_1.md) and [MILESTONE_2.md](MILESTONE_2.md) first.

---

## What the challenge asks for

| Deliverable | Status |
|---|---|
| `agent/harness.py` — retry, fallback, guardrails | §2, §3, §4 |
| `agent/logger.py` — structured step logger | §5 |
| `config.yaml` — all runtime settings | §6 |
| `main.py` updated to wrap the loop with the harness | §7 |
| `HARNESS.md` — each decision and the failure it defends against | §10 |

The framing from the brief, worth keeping in mind: *a working agent loop is not the
same as a reliable one.*

---

## 1. What actually goes wrong

Not theory — these are the things that will happen when I run this ten times.

1. The API rate limits me (429).
2. A call hangs and times out.
3. The LLM returns something that isn't valid JSON.
4. A tool throws.
5. Chroma isn't there, or the read fails.
6. The agent loops forever without finishing.
7. It burns through tokens.

Each of the sections below handles one or more of these. That one-to-one mapping is
also exactly what `HARNESS.md` has to say, so this section *is* the outline for it.

---

## 2. Retry

Wait and try again, with the delay doubling each time, plus a random amount.

```python
delay = min(base_delay * (2 ** attempt), max_delay)
time.sleep(random.uniform(0, delay))
```

**Why doubling:** if the service is busy, hammering it immediately makes it worse.

**Why random:** if several calls fail at the same moment and all retry after exactly
2 seconds, they collide again. Randomizing spreads them out. This is called jitter,
and it's one line.

### Which errors get retried

| Error | Retry? | Why |
|---|---|---|
| Rate limit (429) | Yes, long wait | It's temporary. Groq sends `retry-after` — use it instead of my own guess |
| Timeout | Yes, and send less | Retrying the same oversized request will just time out again |
| Server error (5xx) | Yes | Their problem, probably temporary |
| Bad JSON from the LLM | Yes, immediately | See §3 — no point waiting, the fix is a better prompt |
| Bad request (400) | **No** | The request is malformed. Retrying is waiting to fail again |
| Auth error (401) | **No** | Stop and print something useful about `GROQ_API_KEY` |

The last two rows matter more than they look. "Retry everything" is the wrong answer,
and being able to say why is the point of the table.

### Which limit I'll actually hit

Groq's developer plan allows 1,000 requests/min and 250,000 tokens/min. My loop makes
maybe 10 requests a run, so **requests-per-minute is not my problem — tokens-per-minute
is**, on a long document with big extraction calls.

That changes the response. A 429 from a request limit means *wait a moment*. A 429
from a token limit means *wait, and then send a smaller chunk*. So the retry reads
which limit was hit from the `x-ratelimit-*` headers and shrinks the batch when it's
tokens. Backoff alone doesn't fix a request that's simply too big.

It also means I won't get rate-limited by accident during the demo, which is why §8
exists.

---

## 3. When the LLM returns broken JSON

### First, the honest bit

I'm on Groq with `openai/gpt-oss-120b` in **strict structured output mode**, which
uses constrained decoding — the model is restricted at the token level and physically
cannot emit JSON that violates my schema. So the failure this whole section defends
against is one my primary model shouldn't produce.

I'm building it anyway, and I should be able to say why in one breath:

1. **The brief requires it.** Milestone 3 names malformed JSON as a failure mode to
   handle.
2. **Strict mode guarantees the shape, not the sense.** A perfectly schema-valid plan
   can still say `{"action": "compare_claims", "params": {"a": "clm_99"}}` where
   `clm_99` doesn't exist. That's a different check, and rung 3 is where it lands.
3. **It only holds for two models.** `config.yaml` lets me swap the model. Anything
   else on Groq drops to JSON Object Mode — valid JSON, no schema guarantee — and
   then this ladder is the only thing standing between me and a crash.
4. **Strict mode can still 400.** If my schema uses a feature constrained decoding
   doesn't support, I get a hard error, not a graceful degrade.

> This is a better story than pretending the risk is bigger than it is. "I chose a
> model that makes this failure nearly impossible, *and* I kept the defence because
> the config lets someone change that model" is an engineering decision. Padding the
> threat would be the opposite.

### The ladder

Each rung is cheaper and more constrained than the last.

1. **Parse it.** Usually fine.
2. **Dig the JSON out.** The model wrapped it in "Here's my plan: {...} Hope that
   helps!" Find the braces and parse what's inside. Costs nothing.
3. **Ask again, showing the error.** Send back what it produced plus what the
   validator complained about. "This wasn't valid. Here's why. JSON only."
4. **Ask again, simpler.** Drop the examples and the optional fields. Ask for the
   smallest possible object.
5. **Give up on the LLM for this step.** Use a hardcoded plan — `extract_claims` on
   the next unread chunk. Not smart, but the loop keeps going.

Rung 5 is the important one. **The loop never dies from a bad parse.**

---

## 4. Fallbacks and guardrails

### Fallbacks — the challenge's four, plus what I added

| Failure | What happens |
|---|---|
| LLM output unparseable | The ladder in §3 |
| Tool call fails | Returns `ok: False`. Reflect sees it, notes it, next round works around it |
| Max iterations reached | Stop, return what we have, `status=PARTIAL` |
| Memory read fails | Carry on with `memory=[]`, log a warning, set `degraded=True` in the observation so the model knows its recall is blind |
| Same tool fails 3 times | Mark it unavailable in the observation. The model stops choosing it |

That last one — a tool that's reliably broken shouldn't be offered again this run.
Retry alone doesn't fix a tool that's actually down.

### Guardrails

| Guardrail | Setting | What it stops |
|---|---|---|
| Max iterations | `10`, configurable | Running forever |
| Token budget | Warn at 100k, stop at 150k | Cost blowout |
| Stuck detection | Reflect returns the same thing twice → `STUCK` | Spinning on the same step |
| Quote verification | Every quote must match the source at its recorded position | Hallucinated findings |

**Stuck detection** — hash the reflection with the timestamps stripped out. Two
identical hashes in a row means nothing changed, so stop.

**Quote verification** is my own addition and I think it's the best thing in the
harness. Every claim carries the character positions it came from
([MILESTONE_1.md §5](MILESTONE_1.md)). Before a finding goes in the report, I slice
the original text at those positions and check the quote matches. If it doesn't, the
model made it up and the finding is dropped.

It's about six lines, and it makes fabricated quotes structurally impossible to
report rather than merely unlikely. That's a nice thing to be able to say.

### End states

| Status | Means |
|---|---|
| `COMPLETE` | Finished properly |
| `PARTIAL` | Hit max iterations, here's what I have |
| `STUCK` | Stopped repeating itself |
| `BUDGET_EXCEEDED` | Ran out of tokens |
| `FAILED` | Something unrecoverable |

**Every one of them still returns a report.** The agent never returns nothing.

---

## 5. Logging

One JSON object per line, to `logs/run-<id>.jsonl`. The challenge lists the required
fields; these are them plus tokens, because I want to see cost.

```json
{"ts":"2026-08-17T09:14:02.881Z","run_id":"run_042","iteration":3,"step":"act",
 "tool":"compare_claims","in":"clm_17 vs clm_23",
 "out":"contradiction=true type=NUMBERS","ms":1840,
 "tokens":{"in":1203,"out":288},"attempts":1,"error":null}
```

One line per step, so four lines per round minimum. Long fields get truncated so the
file stays readable.

Alongside it, readable console output during the run — same events, formatted for a
human. The JSONL is for me afterwards; the console is what the video shows.

---

## 6. Config

Everything in `config.yaml`. **Nothing hardcoded in the loop** — that's an explicit
rule in the brief.

```yaml
llm:
  provider: groq
  base_url: https://api.groq.com/openai/v1
  model: openai/gpt-oss-120b      # reason + compare_claims (strict mode)
  fast_model: openai/gpt-oss-20b  # extract_claims (strict mode, 4x cheaper)
  temperature: 0
  max_completion_tokens: 2048
  strict_schema: true             # false → falls back to the §3 ladder
  timeout_s: 60

loop:
  max_iterations: 10
  min_confidence: 0.6

budget:
  max_tokens: 150000              # ≈ $0.05 a run at gpt-oss-120b prices
  warn_at: 100000

retry:
  max_attempts: 4
  base_delay_s: 1.0
  max_delay_s: 30.0
  honour_retry_after: true        # Groq sends it; trust it over my own maths

memory:
  backend: chroma
  persist_dir: .memory
  embedding_model: all-MiniLM-L6-v2   # local — Groq has no embeddings endpoint
  recall_k: 8

logging:
  level: INFO
  path: logs/run-{run_id}.jsonl
```

`.env.example` holds one line: `GROQ_API_KEY=`.

Order of precedence: **command line flag > environment variable > config.yaml >
default.** API keys live in `.env` and never go in git.

`strict_schema: false` is worth having as a switch. Flipping it forces the §3 ladder
into use on the same model, which is how I test the fallback path without waiting for
a real failure — and how I'd run on `llama-3.3-70b-versatile` if I ever wanted to.

---

## 7. `main.py`

The harness wraps the loop from the outside:

```python
def main():
    cfg = load_config()
    log = StepLogger(cfg)

    try:
        report = harness.run(loop.run_loop, doc=args.doc, cfg=cfg, log=log)
    except Exception as e:
        log.error(e)
        report = partial_report(reason=str(e), status="FAILED")

    write_report(report)
    print_summary(report)
```

The loop itself stays clean. It doesn't know about retries or budgets — the harness
handles that around it. Keeping them separate is why the loop is still readable
after all three milestones.

---

## 8. Fault injection

Build this — it's a few lines and it earns them back twice: once on video day, once
as the thing the tests below drive.

```bash
python main.py --doc test/data/policy.md --inject-failure rate_limit
python main.py --doc test/data/policy.md --inject-failure bad_json
python main.py --doc test/data/policy.md --inject-failure tool_error
python main.py --doc test/data/policy.md --inject-failure memory_down
```

Each one forces a specific failure at a specific round. Turns "my harness handles
failures" into something I can show the camera instead of describe.

The video requires one failure handled gracefully. This makes that shot repeatable
instead of dependent on getting rate-limited at the right moment.

---

## 9. Tests

Landed as five tests inside the single [test/test_agent.py](../test/test_agent.py),
not a file per concern. `time.sleep` is replaced with a list-appending recorder, so
a test of exponential backoff runs instantly — assertions are on the *computed*
delays, never on elapsed time.

| Test | Checks |
|---|---|
| `test_retries_a_429_but_never_a_400` | retry is per error class: a 429 gets 3 attempts and backs off twice; a 400 gets exactly 1 — retrying a malformed request only fails slower |
| `test_json_is_dug_out_of_prose` | the ladder's early rungs: JSON wrapped in commentary or a code fence still parses; real garbage still returns `None` |
| `test_the_loop_survives_an_unusable_model` | rung 5 — a model that always fails to parse still gets a hardcoded fallback plan, never an exception |
| `test_guardrails_stop_the_run_and_still_return_a_report` | max-iterations → `PARTIAL` with a real report; the token budget guardrail fires; stuck detection distinguishes "nothing moved" from "same instruction, but still reading" |
| `test_fabricated_quotes_are_dropped_from_the_report` | a finding quoting text the document doesn't have never reaches the report |
| `test_every_step_is_logged_with_the_required_fields` | one JSONL line per step, all four step names present, all required fields present |
| `test_config_precedence_and_isolation` | yaml > default, env > yaml, CLI > env, and — a real regression this caught — one test's override can't leak into the next call's defaults |

`test_the_loop_survives_an_unusable_model` is the one to defend if asked: strict mode
means the live model shouldn't ever exercise the repair ladder, but the test does, on
every run of the suite. A defence you can't trigger is one you can't trust.

Not covered by a fast test: `retry-after` header handling, and rungs 3–4 of the
ladder firing in the right order rather than jumping straight to the fallback. Both
exist in `harness.py` and are exercised implicitly by the tests above, just not
asserted rung-by-rung. Listed here rather than left silent.

---

## 10. `HARNESS.md`

The challenge asks for "each engineering decision and what failure it defends
against." That's exactly the shape of the tables above, so it writes as one table:

| Decision | Defends against |
|---|---|
| Strict structured outputs on Groq | Malformed plans at the source — the model can't emit them |
| Exponential backoff | Rate limits and temporary outages |
| Honouring `retry-after` | Guessing a backoff when the server already told me the answer |
| Shrinking the batch on a token-limit 429 | A request that's simply too big to ever succeed |
| Random jitter | Retries colliding with each other |
| No retry on 400/401 | Wasting time on a request that can't succeed |
| JSON repair ladder | A swapped model, a schema 400, or anything not in strict mode |
| Semantic validation of the plan | A schema-valid plan that references something that doesn't exist |
| Hardcoded fallback plan | The loop dying because one LLM call went bad |
| Tool errors as observations | A broken tool crashing the run instead of being routed around |
| Marking failed tools unavailable | The model repeatedly choosing something that's down |
| Max iterations | Running forever |
| Token budget | Cost blowout |
| Stuck detection | Spinning on the same step |
| Quote verification | Reporting a contradiction that isn't in the document |
| Everything in config | Not being able to change behaviour without editing the loop |

For each row I need one honest sentence about *why that choice and not another*. That
sentence is the whole point of the file.

Each row also has a test in §9. Worth saying so in `HARNESS.md` — a defence with a
test behind it is a different claim from a defence that's just described.

---

## 11. Done when

- [ ] A forced rate limit retries with visible backoff and the run completes
- [ ] Forced bad JSON climbs the ladder and the run completes
- [ ] A forced tool error is handled, and the next round does something different
- [ ] Memory down → the run continues with a warning
- [ ] Max iterations → `PARTIAL` with a real report attached
- [ ] Two identical reflections → `STUCK`
- [ ] `logs/run-<id>.jsonl` has one line per step with all the required fields
- [ ] Changing `config.yaml` changes behaviour with no code edits
- [ ] Nothing hardcoded left in `loop.py`
- [ ] `pytest` passes — the whole suite, no API key needed

---

## 12. Questions I should be able to answer for this milestone

1. Why do I retry a 429 but not a 400?
2. What is jitter and why does it matter?
3. Walk through what happens when the LLM returns broken JSON.
4. Why is a failed tool an observation instead of an exception?
5. What's the difference between retrying a tool and marking it unavailable?
6. How does stuck detection work, and why isn't max iterations enough on its own?
7. What stops the agent from reporting a quote that isn't in the document?
8. What does the agent return when it fails? Why never nothing?
9. How do I test 30-second backoff without waiting 30 seconds?
10. My model uses strict structured outputs and can't emit bad JSON. So why did I
    build the repair ladder anyway?
11. Groq's limits are 1K requests/min and 250K tokens/min. Which one do I actually
    hit, and why does that change what the retry does?

---

**Back to:** [PLAN.md](../PLAN.md)
