# Milestone 1 — The Core Loop

**Goal:** four functions that call each other in a circle and find a real
contradiction in a real document.

Read [PLAN.md](../PLAN.md) first — the contradiction types in §3 are used throughout.

---

## What the challenge asks for

| Deliverable | Status |
|---|---|
| `agent/loop.py` — perceive, reason, act, reflect | §2 |
| `agent/tools.py` — tool definitions + handlers | §3 |
| `agent/prompts.py` — prompt templates | §4 |
| `PATTERNS.md` — patterns research + my choice | §8 |
| Working demo, 2–3 iterations on real input | §7 |

Rules: at least 2 tools, defined as JSON Schema dicts. No LangChain / CrewAI /
AutoGen. Reflect's output feeds the next perceive. Stop on done or max iterations.

---

## 1. Step 0 — write the test document first

Before any code: write `test/data/policy.md` and `test/data/expected.yaml` by hand.

Plant **3 real contradictions** (one `NUMBERS`, one `RULES`, one `MEANING`) and
**2 fakes** (a tiered-pricing pair and a stated exception — from the second table in
[PLAN.md §3](../PLAN.md)). Record both lists in `expected.yaml`.

Why first: it forces me to decide what a contradiction actually *is* before any code
depends on the answer. It's also the only way I'll know whether the agent works.

---

## 2. The four functions

The challenge fixes these signatures. I use them exactly as written.

### `perceive(input_data: str) -> dict`

Reads and organizes. **No LLM call here** — plain Python.

**Round 1** — `input_data` is the document path. Load it, split it into numbered
chunks by heading and paragraph, and record where each chunk starts and ends in the
raw text.

Those character positions matter. Every quote in the final report gets checked
against them, so the agent can't invent a quote that isn't in the document.

**Round 2 onward** — `input_data` is the instruction `reflect` produced last round,
as a JSON string ("check §5 to §8 next"). Perceive reads it, reloads the chunks, and
picks the focus.

> The signature says `str`, so state has to be serialized to get back in. That's the
> feedback edge, and it's worth being able to explain.

Returns:

```python
{
  "iteration": 3,
  "doc_id": "sha256:...",
  "intent": "find_contradictions",
  "instruction": "Check §5–§8 next. §2 is done.",
  "focus_chunks": ["c12", "c13", "c14"],
  "covered": {"scanned": 14, "total": 40},
}
```

### `reason(observation: dict, memory: list) -> dict`

The one LLM call per round. It gets:

- where we are, what's left to cover
- what we already found (`memory`, a list — filled in during Milestone 2; `[]` for now)
- the tool list with their schemas

It picks **one** tool and says why.

Returns:

```python
{
  "action": "compare_claims",
  "params": {"a": "clm_17", "b": "clm_23"},
  "reasoning": "Both are about log retention and both give a number of days.",
}
```

**Structured output, strict mode.** On Groq this is `response_format:
{"type": "json_schema", "json_schema": {"strict": true, "schema": PLAN_SCHEMA}}` with
`openai/gpt-oss-120b`. Constrained decoding means the model physically cannot emit
something that violates the schema — `reason` returning garbage stops being a failure
mode I have to code around. `temperature=0` so runs repeat.

Two rules strict mode imposes on `PLAN_SCHEMA`: every field must be listed in
`required`, and the object needs `additionalProperties: false`. An optional field is
modelled as required-and-nullable. See [PLAN.md §4](../PLAN.md).

I still build the JSON repair ladder in Milestone 3. Reasons in
[MILESTONE_3.md §3](MILESTONE_3.md) — the short version is that strict mode
guarantees the *shape*, not the *sense*, and it only holds for these two models.

### `act(plan: dict, tools: dict) -> dict`

Runs the tool the plan picked. Nothing clever.

`tools` is the registry — `{name: {"schema": ..., "handler": ...}}`, which is exactly
the `dict` the signature asks for. Validate `params` against the schema *before*
calling, so a bad plan fails cheaply instead of crashing inside a handler.

> Note I'm **not** using Groq's native tool-calling API. `reason` picks its tool by
> returning an `"action"` string, and `act` dispatches on it. Two reasons: the
> challenge says wire the loop myself, and Groq doesn't allow tool use and structured
> outputs in the same call anyway. The rule and the constraint point the same way.

Every tool returns the same shape:

```python
{"ok": True, "tool": "compare_claims", "data": {...}, "error": None, "ms": 1840}
```

If a tool fails, that's `ok: False`, not a crash. It flows into `reflect` like any
other result and the agent gets to react to it.

### `reflect(result: dict, observation: dict) -> dict`

Two jobs.

**One — check the finding.** If `act` found a possible contradiction, reflect tries to
talk itself *out* of it using the "not contradictions" table in
[PLAN.md §3](../PLAN.md). Only if it can't explain it away does it get recorded.

This is the precision step. Without it the agent flags tiered pricing as a conflict.

**Two — decide what's next.** Done? How's it going? Where to look next?

Returns:

```python
{
  "is_done": False,
  "quality_score": 0.64,
  "next_instruction": "§5–§8 numbers are clean. Check the encryption rules in §3 and §5.",
  "finding": {...} or None,
  "rejected": [{"a": "clm_17", "b": "clm_44", "why": "different plans"}],
  "lesson": "§6 uses tier headings — always read the heading with the sentence.",
  "fingerprint": "sha1:...",
}
```

`lesson` and `rejected` are written to memory in Milestone 2. `fingerprint` is used
for stuck-detection in Milestone 3. Both fields exist from day one so I'm not
retrofitting later.

### The loop itself

```python
def run_loop(doc_path: str, max_iterations: int = 10) -> dict:
    payload = doc_path
    for i in range(max_iterations):
        observation = perceive(payload)
        plan        = reason(observation, memory=[])      # M2 fills this in
        result      = act(plan, TOOLS)
        reflection  = reflect(result, observation)

        if reflection["is_done"]:
            return finish("COMPLETE")

        payload = json.dumps({                            # the feedback edge
            "doc_id": observation["doc_id"],
            "iteration": i + 2,
            "instruction": reflection["next_instruction"],
        })

    return finish("PARTIAL")
```

That's the whole loop. Everything else is a detail hanging off one of the four steps.

---

## 3. Tools

The challenge asks for at least two. I'm using three, because three is the natural
number here and I can explain each one.

**`extract_claims(chunk_ids)`** — pull the checkable statements out of some chunks.
Each gets a type from [PLAN.md §3](../PLAN.md), the subject it's about, and where it
came from. *LLM-backed.*

**`find_related_claims(claim)`** — find claims about the same subject. This is how the
agent gets candidates to compare without checking all 20,000 pairs.
*Keyword match now; becomes vector search in Milestone 2.*

**`compare_claims(a, b)`** — do these two conflict? Returns yes/no, which type, and
why. The "not contradictions" table goes into this prompt. *LLM-backed.*

One detail worth having: for `NUMBERS` contradictions, convert both values to the
same unit in plain Python and compare with `!=`. The LLM only answers "are these two
talking about the same thing?" It doesn't do arithmetic. Small change, removes a
whole class of wrong answers.

### Schema shape

Every tool is a JSON Schema dict. Descriptions are written for the model, and they
say when **not** to use the tool as well as when to.

```python
EXTRACT_CLAIMS = {
    "name": "extract_claims",
    "description": (
        "Pull out checkable statements from specific chunks of the document. "
        "Use when entering a part of the document that hasn't been read yet. "
        "Do not use to compare statements — use compare_claims for that."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chunk_ids": {
                "type": "array", "items": {"type": "string"},
                "description": "Chunk IDs from the observation, e.g. ['c12','c13'].",
            },
        },
        "required": ["chunk_ids"],
        "additionalProperties": False,
    },
}
```

---

## 4. Prompts

Three templates in `prompts.py`, one per LLM call.

| Prompt | Job |
|---|---|
| `REASON` | The tool list, where we are, what's in memory. "Pick one tool. Say why." |
| `EXTRACT` | "Pull out checkable statements. Give each one a type, a subject, and its position." |
| `COMPARE` | "Do these conflict?" — with the **not-contradictions table** pasted in as the rubric |

`COMPARE` is the one that decides whether this project works. The other two are
plumbing. Budget time accordingly.

---

## 5. What a claim looks like

```python
{
  "claim_id": "clm_17",
  "chunk_id": "c04",
  "section": "§2.1",
  "text": "Audit logs are retained for 30 days.",
  "type": "NUMBERS",
  "subject": "audit log retention",
  "value": {"raw": "30 days", "normalized": 30, "unit": "day"},
  "position": [4821, 4977],
}
```

`position` is what lets me verify the quote against the source. `normalized` + `unit`
is what lets Python do the comparison instead of the LLM.

---

## 6. Tests

Landed simpler than planned: one file, [test/test_agent.py](../test/test_agent.py),
not one file per module. A `FakeLLM` class at the top scripts replies by call name,
so `agent/llm.py` is never touched and the whole file runs offline.

The M1-relevant ones:

| Test | Checks |
|---|---|
| `test_chunk_positions_are_exact` | `raw[start:end] == text`, character for character — everything downstream, including quote verification in M3, depends on this |
| `test_bad_params_are_rejected_before_the_tool_runs` | `act` refuses a bad plan before the handler runs |
| `test_invented_quotes_never_become_claims` | a sentence not in the chunk is dropped, not stored |
| `test_python_does_the_arithmetic_not_the_model` | `"30 days"` == `"720 hours"`; the model never computes this |
| `test_reflect_feeds_the_next_round` | `reflect`'s instruction reaches round N+1's prompt — the test for *"is this a loop"* |

The last one is the one that matters. Everything else here would pass on a
straight-line pipeline; that one only passes if the feedback edge actually works,
and it caught a real regression when a rewrite once dropped it.

`test_real_run_against_groq` (`pytest -m llm`) runs the whole thing against
`test/data/policy.md` and checks both directions against `expected.yaml`: the 3
planted contradictions are found, and the 2 planted traps are not. The second
assertion is the one that fails first — anyone can score on the first by flagging
everything.

```bash
pytest              # fast tests, no API key
pytest -m llm       # adds the real run
```

---

## 7. Done when

- [ ] `python main.py --doc test/data/policy.md` runs 3+ rounds
- [ ] Each round visibly does something different from the last
- [ ] It finds at least one of the contradictions I planted
- [ ] It does **not** flag either of the fakes
- [ ] `reflect`'s `next_instruction` visibly changes what round N+1 does
- [ ] Loop stops on `is_done` or at `max_iterations`, and returns a report either way
- [ ] `pytest` passes with no API key set

The fourth box is the one to care about. Finding contradictions is easy; not finding
fake ones is the actual job.

---

## 8. `PATTERNS.md`

Write it right after the loop works, while the reasoning is fresh.

Required: a paragraph on each of five patterns, which one(s) I used, and why that
fits my use case.

| Pattern | The one-line version |
|---|---|
| **ReAct** | Think, act, look at the result, think again — interleaved |
| **Reflexion** | After acting, critique yourself in words and carry that critique forward |
| **Chain-of-Thought** | Make the model write its reasoning steps before its answer |
| **Tree of Thoughts** | Branch into several lines of reasoning, evaluate them, keep the best |
| **LATS** | Tree of Thoughts plus tree search — score branches, expand the promising ones |

**What I used and why:**

*ReAct* is the loop's backbone — perceive/reason/act/reflect is a ReAct cycle with
the steps named explicitly.

*Reflexion* is in the reflect step, twice. Once at finding level (argue against your
own finding). Once at loop level (write down what you learned and use it next round).

*Chain-of-Thought* is the `reasoning` field in the plan — the model writes its
thinking before it commits to a tool, and I keep it in the log so I can read why it
did what it did.

*Tree of Thoughts and LATS* I **skipped**. ToT would explore several different ways to
judge each pair of claims. That multiplies cost, and for the numeric half of the
judgement a `!=` comparison already gives the right answer with certainty. For the
language half, the second opinion I actually want is the adversarial check in
reflect — which is cheaper and more targeted than a search tree.

> Declining ToT with a reason is a stronger answer than adopting it because it sounds
> impressive. But I have to be able to say *why* out loud — see §9.

---

## 9. Questions I should be able to answer for this milestone

1. Why does this need a loop instead of one prompt?
2. Why is there no LLM call in `perceive`?
3. `perceive` takes a string. So how does `reflect`'s output get back into it?
4. What are all the ways the loop can stop?
5. Explain all five patterns, one paragraph each, from memory.
6. Which did I use, and why did I skip ToT and LATS?
7. Why three tools and not two, or seven?
8. What stops the agent from quoting something that isn't in the document?
9. Give three things that look like contradictions but aren't, and how I reject each.
10. Why do my tests use a fake LLM instead of the real one?

---

**Next:** [MILESTONE_2.md](MILESTONE_2.md) — memory.
