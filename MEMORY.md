# MEMORY.md

The memory tool I chose and why, how memory is structured, and a worked example of
it changing the outcome.

Implementation: [agent/memory_manager.py](agent/memory_manager.py). Wiring:
[agent/loop.py](agent/loop.py). Tests: [test/test_agent.py](test/test_agent.py).

---

## 0. Why memory isn't optional here

Most agent projects add memory to get better. This one doesn't work without it.

A contradiction is, by definition, **two statements in different places**. An agent
with no memory reads a chunk, extracts some claims, and then has nothing to compare
them against — every earlier round's work is gone. Round 3 finding "system logs are
kept 90 days" is worthless unless it can reach back to round 2's "audit logs are
retained 30 days", five pages earlier.

That also makes this the easiest thing in the project to demonstrate honestly: turn
memory off and the findings disappear. See §5.

---

## 1. What I chose: ChromaDB, local

**The reason is specific to this problem, not a general preference.**

The operation the agent needs most often is *"find claims similar to this one"*. That
is literally a vector similarity search. So Chroma isn't a storage layer bolted on to
satisfy a requirement — it's the **search engine that makes candidate-finding
possible**. Without it, `find_related_claims` is keyword matching, and keyword
matching misses exactly the case that matters most: the same subject named two
different ways ("audit log" / "system log" / "logs"), which is where real
contradictions hide.

Second reason: it runs locally, so the demo can't break on someone else's rate limit
or expired key.

### Why not mem0 or Zep

Both are the alternatives the brief lists, and both are session/conversation memory
tools built around remembering things about a **user** — preferences, history,
identity, across sessions. My problem is remembering things about a **document**,
within one run. Different unit of memory, different retrieval pattern. Chroma is the
closer fit, and that's the whole argument.

### Embeddings

`all-MiniLM-L6-v2`, local, via Chroma's default embedding function. This was not
really a choice: **Groq has no embeddings endpoint** — its model list is chat,
speech, and moderation only. So the embedding side was always going to be local
regardless of vector store, which makes a local-by-default store like Chroma the
natural pick over something hosted like Pinecone.

### Three backends, one interface

| Class | Used by | Why it exists |
|---|---|---|
| `ChromaMemory` | real runs | The vector store. Persistent, one collection per namespace |
| `DictMemory` | the test suite | Same interface, word-overlap ranking instead of embeddings — so tests are fast and offline |
| `NullMemory` | `--no-memory` | Remembers nothing. Exists purely to prove the agent is worse without memory |

All three satisfy the `Memory` protocol at
[agent/memory_manager.py:38](agent/memory_manager.py:38), so the loop never knows
which one it has.

---

## 2. The API

The three functions the challenge asks for, module-level:

```python
save(records: list[dict], namespace: str) -> dict
recall(query: str, namespace: str, k: int = 6) -> list[dict]
clear(namespace: str | None = None) -> dict
```

`recall()` returns a **list**, which matches `reason(observation, memory: list)`
exactly. That wasn't an accident — the required signature was telling me the shape
memory had to have.

---

## 3. Structure: three namespaces

| Namespace | Holds | Used for |
|---|---|---|
| `claims` | every statement found so far | finding candidate pairs to compare |
| `findings` | confirmed contradictions | the final report, and not re-reporting one |
| `rejected` | pairs already checked and cleared | never re-checking the same pair |

A record is `{"id": ..., "text": ...}` plus flat metadata (`section`, `type`,
`subject`). Chroma metadata must be flat scalars, so anything nested is JSON-encoded
on the way in — [memory_manager.py:81](agent/memory_manager.py:81).

### Why `rejected` is its own namespace

This is the one worth defending, because a plain claim store doesn't give it to you.
When `reflect` decides a pair is **not** a contradiction — "that's tiered pricing",
"§2.3 is a stated exception" — it writes that decision down, keyed by an
order-independent `pair_id` so A/B and B/A are the same entry
([memory_manager.py:208](agent/memory_manager.py:208)).

Three payoffs:

1. **Cost** — the same pair is never sent to the LLM twice.
2. **Consistency** — the same false positive can't come back on a later run. Given
   that false positives are my main predicted failure mode, this matters.
3. **Termination** — repeating an action becomes provably useless, so the agent moves
   on instead of circling. It's a cheap contributor to actually finishing.

---

## 4. Where it plugs into the loop

Exactly two places, and they're the two the challenge specifies —
[agent/loop.py:336](agent/loop.py:336):

```python
recalled = memory.recall(observation["instruction"], CLAIMS, k=cfg.memory.recall_k)
#   ↑ READ — before reason, every round

plan       = reason(observation, recalled)      # the recalled text goes INTO the prompt
result     = act(plan, TOOLS)
reflection = reflect(result, observation)

memory_manager.remember_round(                   # ↓ WRITE — after reflect
    memory, claims=..., finding=..., rejected=...
)
```

Two details that matter more than they look:

- **Recall is inside a try/except.** If the memory backend is down, the round
  continues with no recall and a logged warning rather than crashing
  ([loop.py:346](agent/loop.py:346)). Degrade, don't stop.
- **The recalled text is formatted into the prompt**, not merely fetched
  ([loop.py:342](agent/loop.py:342)). This is the difference between memory existing
  and memory mattering, and `test_memory_reaches_the_prompt` is the test that holds
  me to it — it asserts the recalled claim's text appears in what the fake LLM was
  actually asked.

`find_related_claims` — a keyword stub in Milestone 1 — becomes a real `recall()`
against the `claims` namespace. That's the upgrade that makes the tool work.

---

## 5. Memory in action: the cross-section finding

The one concrete example, because it's the shape of every finding this agent gets:

**Round 2** reads §2 and extracts:

> `clm_17` — *"Audit logs are retained for 30 days."*  → saved to `claims`

`reflect`'s lesson, also saved: *"§2 says 'audit log'; other sections may say 'system
log' — treat as possibly the same subject."*

**Round 3** reads §7 and extracts:

> `clm_23` — *"System logs are kept for a minimum of 90 days."*

It calls `find_related_claims(clm_23)`. The vector search returns `clm_17` — five
pages back, from a round that finished two iterations ago, and matched despite the
subject being worded differently. `compare_claims(clm_17, clm_23)` runs. Both
normalise to days, `30 != 90`, nothing in the text explains the gap.

**→ `NUMBERS` contradiction, §2.1 vs §7.4.**

Note which part memory did: not the judging, the *pairing*. The LLM could never have
compared these two claims because it never saw them in the same context window. That
is the whole job.

> **TODO before submission:** replace the summary above with the actual
> `logs/run-<id>.jsonl` lines from a real run. The milestone plan calls for real log
> lines here, not a reconstruction, and I'd rather paste output than paraphrase it.

---

## 6. The ablation

```bash
python main.py --doc test/data/policy.md
python main.py --doc test/data/policy.md --no-memory
```

Same document, same config, `NullMemory` in the second. The cross-section findings
disappear, because there's nothing for round 3 to compare round 2 against.

`--clear-memory` resets the store so the demo is repeatable.

What the suite actually checks, in [test/test_agent.py](test/test_agent.py), is the
mechanism this ablation depends on: `test_recalled_memory_reaches_the_prompt` proves
recalled text is shown to the model rather than fetched and dropped, and
`test_loop_recalls_before_reason_and_saves_after_reflect` proves the read/write order
the challenge specifies. The end-to-end outcome above — more findings with memory
than without — is checked by running both commands, not by an automated test. Worth
being honest about: the mechanism is asserted, the outcome is demonstrated.

---

## 7. What I'd flag honestly

- **Subject identity is still the hard part.** Embeddings make "audit log" / "system
  log" match, which keyword search wouldn't. They also make things match that
  shouldn't. Recall `k` is a tuning knob over a genuine precision/recall trade-off,
  and I don't think it has a correct setting.
- **`rejected` is per-store, so it persists across runs.** That's the point, but it
  means a bad rejection is sticky until `--clear-memory`.
- **Chroma is doing retrieval, not reasoning.** It hands `compare_claims` a shortlist.
  Everything about whether a pair actually conflicts happens after that.
