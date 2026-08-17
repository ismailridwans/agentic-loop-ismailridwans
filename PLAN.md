# PLAN.md — Overview

Contradiction Detection Agent. This file is the shared context. The build plan for
each milestone lives in its own file.

- **Use case:** find places where a document contradicts itself
- **Repo:** `agentic-loop-<my-name>` (private)
- **Python 3.11**, no agent frameworks

| Milestone         | Plan                                       | Deliverable doc |
| ----------------- | ------------------------------------------ | --------------- |
| 1 — The core loop | [docs/MILESTONE_1.md](docs/MILESTONE_1.md) | `PATTERNS.md`   |
| 2 — Memory        | [docs/MILESTONE_2.md](docs/MILESTONE_2.md) | `MEMORY.md`     |
| 3 — Harness       | [docs/MILESTONE_3.md](docs/MILESTONE_3.md) | `HARNESS.md`    |

---

## 1. What it does

You give it a document. It gives back the places where the document disagrees with
itself, with the exact quotes.

```
$ python main.py --doc test/data/policy.md

  4 iterations · 23 claims found · 2 contradictions · status=COMPLETE

  [1] NUMBERS   §2.1 vs §7.4
      "Logs are retained for 30 days."
      "All audit logs are kept for a minimum of 90 days."
      Same thing, two different numbers, nothing explains the difference.

  [2] RULES     §3.2 vs §5.1
      "Data at rest MUST be encrypted."
      "Encryption is optional for internal buckets."
      §5.1 allows an exception that §3.2 does not.
```

It only checks the document against itself. It does not check facts against the real
world. That keeps every answer verifiable — it's all in the text.

---

## 2. Why this needs a loop and not one prompt

The main thing I have to defend, so it stays simple and true.

A 30-page document has maybe 200 separate statements. To find contradictions you
compare statements against each other. 200 statements is about 20,000 pairs. You
can't put that in one prompt.

So the agent works in rounds. Each round it looks at one part of the document, pulls
out the statements, checks them against what it already found, and decides where to
look next. That last part is the whole reason a loop exists here.

**One sentence:** the loop is how the agent searches a space too big to look at all
at once.

---

## 3. What counts as a contradiction

This list comes before any code, because it becomes the prompt. All three milestones
depend on it.

**Contradictions:**

| Type      | What it looks like                                                             |
| --------- | ------------------------------------------------------------------------------ |
| `NUMBERS` | Same thing, different values — "max 10 MB" vs "up to 25 MB"                    |
| `RULES`   | Same action, different obligation — "must encrypt" vs "encryption is optional" |
| `DATES`   | Conflicting times — "effective Jan 1" vs "effective from April"                |
| `MEANING` | A word defined two different ways — "active user", twice, differently          |
| `FACTS`   | Same thing, different answer — "owned by Team A" vs "owner: Team B"            |

**Not contradictions** — this table matters more, because this is where the agent
will embarrass me if I skip it:

| Looks like a conflict                 | But actually                       |
| ------------------------------------- | ---------------------------------- |
| "Free: 5 GB" / "Pro: 50 GB"           | Different things — different plans |
| "Until 2024 X. From 2024 Y."          | One replaced the other on purpose  |
| "30 days, except audit logs: 90 days" | The exception is stated            |
| "10 MB" / "10,485,760 bytes"          | Same number, different units       |

An LLM asked "find contradictions" flags all four of these. Handling them is most of
the actual work.

---

## 4. The LLM — Groq

Yes — an LLM does the thinking. Here is exactly which, and exactly where.

### Which model, and the reason

**Provider: Groq.** Two models, picked for one specific reason.

| Job                           | Model                 | Why this one                                             |
| ----------------------------- | --------------------- | -------------------------------------------------------- |
| `reason` and `compare_claims` | `openai/gpt-oss-120b` | The judgement calls. Strongest of the strict-mode models |
| `extract_claims`              | `openai/gpt-oss-20b`  | Bulk work. ~1000 tok/s, a quarter the price              |

The reason is not "these are popular." **On Groq, only `openai/gpt-oss-120b` and
`openai/gpt-oss-20b` support strict structured outputs** — `response_format:
{"type": "json_schema", "json_schema": {"strict": true, ...}}`, which constrains the
model at the token level so the output _cannot_ violate the schema.

Every other Groq model, `llama-3.3-70b-versatile` included, only gets JSON Object
Mode: valid JSON, but no guarantee it matches my schema.

My loop lives or dies on `reason` returning a parseable plan every round. So the
model choice follows from the architecture, not the other way round. **That's the
sentence for the README and the report.**

_Verified against Groq's docs on 17 Aug 2026. Model IDs churn — re-check
`console.groq.com/docs/models`, or `GET https://api.groq.com/openai/v1/models`,
before submitting._

### Two constraints strict mode puts on my schemas

1. Every field must be in `required`. Optional fields get modelled as
   required-but-nullable instead.
2. Every object needs `additionalProperties: false`.

Worth knowing before I write `schemas.py`, not after.

### Where the LLM is used

| Where            | What it decides                                                  |
| ---------------- | ---------------------------------------------------------------- |
| `reason`         | which tool to run next, and why                                  |
| `extract_claims` | which sentences are checkable statements, and what they're about |
| `compare_claims` | do these two statements conflict                                 |

**Everything else is plain Python** — splitting the document, comparing numbers,
searching memory, retrying, logging, dedup, scoring.

That ratio is on purpose. Three LLM calls means three places that can go wrong, three
prompts to get right, three things to test. If the LLM is ever doing arithmetic or
bookkeeping, that's a bug and not a feature.

### The client

Groq is OpenAI-compatible, so `agent/llm.py` is thin:

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["GROQ_API_KEY"],
    base_url="https://api.groq.com/openai/v1",
)

resp = client.chat.completions.create(
    model=cfg.llm.model,
    messages=[...],
    temperature=0,
    max_completion_tokens=cfg.llm.max_completion_tokens,
    response_format={
        "type": "json_schema",
        "json_schema": {"name": "plan", "strict": True, "schema": PLAN_SCHEMA},
    },
)
```

Note: **tool use and streaming don't work alongside structured outputs on Groq.** Not
a problem — the challenge says wire the loop myself, so `reason` picks its tool by
returning a `"action"` field in the schema, not by native function calling. The
constraint and the rule happen to agree.

**All calls go through `agent/llm.py`.** One module — so there's one place to put
retries, and one thing to replace with a fake in tests (§6).

### What this costs

At `gpt-oss-120b` prices ($0.15 in / $0.60 out per 1M), the 150K-token budget in
`config.yaml` is roughly **5 cents a run**. The budget guardrail stays because the
brief requires it, not because I'm going to hit it.

Rate limits on the developer plan are 1K requests/min and 250K tokens/min — far more
than a single-document run needs. Two consequences:

- I won't hit a 429 by accident, so `--inject-failure rate_limit` isn't a nice-to-have
  for the demo video. It's the only way I get that shot. (Free tier is tighter —
  check `console.groq.com/settings/limits` for my own account.)
- Tokens-per-minute is the limit I'd actually brush on a long document, not
  requests-per-minute. Worth knowing which one to expect.

---

## 5. Files

```
agentic-loop-<my-name>/
├── agent/
│   ├── loop.py             # perceive, reason, act, reflect        [M1]
│   ├── tools.py            # 3 tool schemas + handlers             [M1]
│   ├── prompts.py          # prompt templates                      [M1]
│   ├── schemas.py          # the dict shapes                       [M1]
│   ├── segmenter.py        # document → numbered chunks            [M1]
│   ├── llm.py              # the only place that calls the API     [M1]
│   ├── memory_manager.py   # save, recall, clear                   [M2]
│   ├── harness.py          # retry, fallbacks, guardrails          [M3]
│   ├── logger.py           # JSON line logger                      [M3]
│   └── config.py           # loads config.yaml + .env              [M3]
├── test/
│   ├── data/
│   │   ├── policy.md       # test doc: 3 real contradictions + 2 fakes
│   │   └── expected.yaml   # what should be found, what should NOT
│   └── test_agent.py       # 16 tests, one file — see §6
├── docs/
│   ├── MILESTONE_1.md
│   ├── MILESTONE_2.md
│   └── MILESTONE_3.md
├── main.py
├── config.yaml · .env.example · requirements.txt · pytest.ini
├── README.md
├── PATTERNS.md · MEMORY.md · HARNESS.md
└── PLAN.md
```

---

## 6. Tests

One file, `test/test_agent.py` — 16 tests, the minimum that would actually catch a
broken submission, not a test per function. A `FakeLLM` class at the top returns
scripted replies, so the whole file runs offline in under a second:

```bash
pytest              # 15 fast tests, no API key needed
pytest -m llm       # adds one real end-to-end run against Groq
```

`pytest.ini` registers the marker:

```ini
[pytest]
markers = llm: hits the real Groq API — needs GROQ_API_KEY, costs a few cents
addopts = -m "not llm" -q
```

### What it covers

| Group | Example | Proves |
| --- | --- | --- |
| Loop (M1) | `test_reflect_feeds_the_next_round` | reflect's output actually reaches the next perceive — this is a loop, not 4 calls in a row |
| Memory (M2) | `test_recalled_memory_reaches_the_prompt` | recall isn't fetched and silently dropped |
| Harness (M3) | `test_retries_a_429_but_never_a_400` | retry policy is per error class, not blanket |
| Harness (M3) | `test_the_loop_survives_an_unusable_model` | the loop never dies from a bad parse |
| Config (M3) | `test_config_precedence_and_isolation` | CLI > env > yaml > default, and one test's override can't leak into the next |
| Real run | `test_real_run_against_groq` | finds the 3 planted contradictions, ignores the 2 traps |

### The test document

`test/data/policy.md` is written **by hand, before any code**. It's the most
important file in the repo after `loop.py`.

| What goes in it                                                   | Why                                  |
| ----------------------------------------------------------------- | ------------------------------------ |
| 3 real contradictions — one `NUMBERS`, one `RULES`, one `MEANING` | Does it find them?                   |
| 2 fakes — a tiered-pricing pair, and a stated exception           | Does it correctly **not** flag them? |

`test/data/expected.yaml` records both lists:

```yaml
should_find:
  - type: NUMBERS
    sections: ["§2.1", "§7.4"]
    about: log retention
  - type: RULES
    sections: ["§3.2", "§5.1"]
    about: encryption requirement
  - type: MEANING
    sections: ["§1.3", "§6.2"]
    about: definition of "active user"

should_not_find:
  - sections: ["§4.1", "§4.2"]
    why: different pricing tiers, not a conflict
  - sections: ["§2.1", "§2.3"]
    why: §2.3 is a stated exception to §2.1
```

The `should_not_find` list is the one that matters. Anyone can score well on finding
contradictions by flagging everything.

This doubles as the demo input, so there's one copy of it and it can't drift.

---

## 7. Order of work

| #   | Step                                                                  | Where                     |
| --- | --------------------------------------------------------------------- | ------------------------- |
| 0   | **Claim the use case on the sign-up sheet. Create the private repo.** | —                         |
| 1   | Write `test/data/policy.md` and `expected.yaml` by hand               | [M1](docs/MILESTONE_1.md) |
| 2   | Skeleton + `llm.py` + `FakeLLM` + fake tools                          | [M1](docs/MILESTONE_1.md) |
| 3   | Segmenter + real `perceive` + `test_agent.py`                         | [M1](docs/MILESTONE_1.md) |
| 4   | Real tools, real `reason`/`act`/`reflect` → **M1 done**               | [M1](docs/MILESTONE_1.md) |
| 5   | Write `PATTERNS.md`                                                   | [M1](docs/MILESTONE_1.md) |
| 6   | Chroma + `memory_manager.py` + `--no-memory` → **M2 done**            | [M2](docs/MILESTONE_2.md) |
| 7   | Write `MEMORY.md`                                                     | [M2](docs/MILESTONE_2.md) |
| 8   | Retries, fallbacks, guardrails, logging, config → **M3 done**         | [M3](docs/MILESTONE_3.md) |
| 9   | Write `HARNESS.md`                                                    | [M3](docs/MILESTONE_3.md) |
| 10  | README, type hints, pin requirements, check no keys committed         | —                         |
| 11  | Record the video                                                      | §8 below                  |
| 12  | **Write the solution PDF myself**                                     | §10 below                 |

Step 1 comes before step 2 on purpose. Writing the test document by hand forces me
to decide what a contradiction actually is before any code depends on the answer —
and it means I have something to test against from the first line of code.

---
