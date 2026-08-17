# Contradiction Detection Agent

An agentic loop, written from scratch, that finds places where a document
contradicts itself. No agent framework — perceive / reason / act / reflect are four
plain Python functions wired together by hand.

**Use case:** detect contradictions inside a document.

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # then paste your Groq key into it
python main.py --doc test/data/policy.md
```

First run downloads a ~80 MB embedding model for the local vector store. After that
it is offline.

```bash
pytest                        # fast tests: no API key, no network, no cost
pytest -m llm                 # adds one real end-to-end run against Groq
```

---

## The LLM

**Groq**, through its OpenAI-compatible endpoint. Every LLM call in this project
goes through Groq — no other provider is used anywhere.

| Job | Model |
|---|---|
| `reason`, `compare_claims` | `openai/gpt-oss-20b` |
| `extract_claims` | `openai/gpt-oss-20b` |

The choice follows from the architecture rather than from taste. On Groq **only the
`gpt-oss` models support strict structured outputs**, where constrained decoding
stops the model emitting anything that violates the schema. The loop depends on
`reason` returning a parseable plan every single round, so those were the two models
available. `llama-3.3-70b-versatile` only offers JSON-object mode: valid JSON, no
schema guarantee.

Both jobs currently run on `gpt-oss-20b`. The original split put `gpt-oss-120b` on
the reasoning/comparing calls, but its free-tier daily quota ran out mid-testing and
its per-request reservation cap (8000 TPM) is tighter than 20b's — so both fields
point at 20b until quota resets. Same config field either way, so switching back is
a one-line change, not a code change.

Everything is set in `config.yaml`, so swapping provider or model is a config change.

**The LLM is used in exactly three places** — deciding the next action, pulling
claims out of text, and judging whether two claims conflict. Segmenting, comparing
numbers, ranking candidate pairs, retrying, logging and dedup are all plain Python.
If the model is ever doing arithmetic, that is a bug.

---

## How it works

```
input ──▶ perceive ──▶ reason ──▶ act ──▶ reflect ──┐
            ▲            │          │         │      │
            │         memory      tools    memory    │
            │        recall()             save()     │
            └──────── next_instruction (serialised) ─┘
```

**Why a loop and not one prompt.** A 30-page document holds ~200 checkable
statements. Contradictions live in *pairs*, so checking them all is ~20,000
comparisons — more than fits in any context window. The loop is the search
strategy: each round reads one part of the document or tests one ranked pair, then
decides where to look next.

| Step | What it does | LLM? |
|---|---|---|
| `perceive` | Splits the document into chunks with exact source offsets, tracks coverage, ranks candidate pairs, picks the focus | no |
| `reason` | Chooses one tool and says why | yes |
| `act` | Validates params against the tool's JSON Schema, dispatches | no |
| `reflect` | Records a finding, decides done/next | yes |

`perceive` takes a `str`, so `reflect`'s output is serialised into a JSON envelope
to get back in. That is the feedback edge, made explicit rather than hidden in a
global — and it means every round is replayable from the log.

### Tools

| Tool | Backing |
|---|---|
| `extract_claims` | LLM, then every quote is located in the source or dropped |
| `find_related_claims` | plain Python — subject/type/unit overlap |
| `compare_claims` | LLM for the language question, Python for the arithmetic |

Numbers are normalised to a base unit and compared with `!=` in Python; the model is
handed the result as a stated fact and only ever asked *are these two about the same
thing?* Arithmetic can rule a contradiction **out** with certainty. It can never rule
one **in**, because equal-looking numbers about different subjects are not a
conflict.

### Precision

The hard part is not finding contradictions — it is not inventing them. A model
asked to "find contradictions" will flag tiered pricing, superseded clauses and unit
conversions. Most of the compare prompt is therefore a list of things that are *not*
contradictions:

| Guard | Example that must be rejected |
|---|---|
| `different_things` | "Free: 5 GB" vs "Pro: 50 GB" |
| `superseded` | "Until 2024, X. From 2024, Y." |
| `stated_exception` | a rule plus its own declared carve-out |
| `same_value` | "10 MB" vs "10,485,760 bytes" |

`test/data/expected.yaml` lists both the contradictions the agent should find and
the traps it must ignore. The second list is the real test.

---

## Milestones

| | Deliverable | Notes |
|---|---|---|
| **1** | `agent/loop.py`, `tools.py`, `prompts.py`, `PATTERNS.md` | three tools, JSON Schema definitions |
| **2** | `agent/memory_manager.py`, `MEMORY.md` | ChromaDB, `save`/`recall`/`clear` |
| **3** | `agent/harness.py`, `logger.py`, `config.yaml`, `HARNESS.md` | retry, fallbacks, guardrails, JSONL trace |

### Memory (M2)

Local **ChromaDB**. The operation the agent needs most often is "find claims similar
to this one", which is a vector search — the store is the search engine, not
decoration. Three namespaces:

- `claims` — every statement found, the pool candidates come from
- `findings` — confirmed contradictions
- `rejected` — pairs already cleared, so the same pair is never re-judged

Read before every `reason`, written after every `reflect`.

```bash
python main.py --doc test/data/policy.md --no-memory   # the ablation
```

### Harness (M3)

```bash
python main.py --inject-failure rate_limit    # backoff, visible in the log
python main.py --inject-failure bad_json      # the repair ladder climbing
python main.py --inject-failure tool_error    # ok=False, the loop routes around it
python main.py --inject-failure memory_down   # continues, degraded
```

- Retry with exponential backoff and full jitter, **per error class** — 429 and 5xx
  retry, 400 and 401 fail fast because retrying a malformed request only fails
  slower. `retry-after` is honoured when Groq sends it.
- A five-rung ladder for unparseable JSON, ending in a hardcoded plan so the loop
  never dies from a bad parse.
- Guardrails: max iterations, token budget, stuck detection, and quote verification
  that drops any finding quoting text the document does not contain.
- Every run ends in `COMPLETE` / `PARTIAL` / `STUCK` / `BUDGET_EXCEEDED` / `FAILED`,
  and every one of them still returns a report.
- One JSON object per step in `logs/run-<id>.jsonl`.

---

## Options

```
--doc PATH                document to check
--config PATH             default config.yaml
--max-iterations N        overrides config.yaml
--model NAME              overrides config.yaml
--no-memory               disable recall
--clear-memory            wipe the store first
--inject-failure MODE     rate_limit | bad_json | tool_error | memory_down
--json PATH               also write the report as JSON
--quiet
```

Precedence: CLI flag > environment variable > `config.yaml` > built-in default.

---

## Layout

```
agent/
  loop.py            perceive, reason, act, reflect, run_loop     [M1]
  tools.py           three tools: schemas + handlers              [M1]
  prompts.py         one template per LLM call                    [M1]
  schemas.py         JSON Schemas for the model's replies         [M1]
  segmenter.py       document -> chunks with source offsets       [M1]
  llm.py             the only module that calls the API           [M1]
  memory_manager.py  save / recall / clear                        [M2]
  harness.py         retry, JSON ladder, guardrails               [M3]
  logger.py          structured JSONL step logger                 [M3]
  config.py          config.yaml + env + CLI                      [M3]
test/
  test_agent.py      one file, 16 tests — the minimum that would actually
                      catch a broken submission
  data/policy.md     test document: 3 planted contradictions + 2 traps
  data/expected.yaml ground truth for both
docs/                milestone build plans
main.py · config.yaml · PATTERNS.md · MEMORY.md · HARNESS.md · PLAN.md
```

---

## Known limitation

The agent reliably reads the whole document and extracts claims, and its precision
guards work — it correctly refuses the planted traps. Recall on distant pairs is
still weak: with a fixed iteration budget it can spend its comparison rounds on
lower-value pairs and never reach the one that matters. The candidate ranking in
`tools.relatedness` is a bag-of-words overlap on the subject strings the model
writes, so two claims about the same thing described in different words
("system log retention" vs "log retention period") score lower than they should.
Replacing that with vector similarity over the claim store is the obvious next step.
