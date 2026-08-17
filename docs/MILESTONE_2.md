# Milestone 2 — Memory

**Goal:** the agent remembers what it found, so round 3 can use what round 2 learned.

Read [MILESTONE_1.md](MILESTONE_1.md) first — this extends that loop.

---

## What the challenge asks for

| Deliverable | Status |
|---|---|
| `agent/memory_manager.py` with `save()`, `recall()`, `clear()` | §2 |
| `loop.py` updated to show memory read/write | §3 |
| `MEMORY.md` — tool choice, structure, concrete example | §7 |

Rules: memory is **read at the start of each `reason`** and passed to the LLM.
Memory is **written after each `reflect`**. The demo must show round N+1 meaningfully
changed by what round N stored.

---

## 1. Why memory is the whole point here

Without memory, each round starts blind. It reads a chunk, extracts some claims, and
then has nothing to compare them against — every previous round's work is gone.

A contradiction is by definition **two statements in different places**. So an agent
with no memory cannot find one at all. Round 3 has to be able to reach back to what
round 2 saw on page 2.

That makes this the easiest milestone to demonstrate honestly: turn memory off and
the findings disappear.

---

## 2. What I'm using and why

**ChromaDB, running locally.**

The reason is specific to this problem, not a general preference. The operation the
agent needs most often is *"find claims similar to this one"* — which is exactly what
a vector store does. It isn't decoration bolted on to satisfy a requirement; it's the
search engine that makes candidate-finding possible.

Second reason: it runs offline. My demo can't break because of someone else's rate
limit or expired key.

Embeddings: `all-MiniLM-L6-v2`, running locally through Chroma's default embedding
function. Free, no API call, and no choice about it — **Groq has no embeddings
endpoint**. Its model list is chat, speech, and moderation only. So the embedding
side of this project was always going to be local regardless of which vector store I
picked, which makes Chroma (local by default) the natural fit rather than something
like a hosted Pinecone.

*(mem0 and Zep are the alternatives the brief lists. Both are session/conversation
memory tools built around remembering things about a **user**. My problem is
remembering things about a **document**. Chroma is the closer fit — and I should be
able to say that in one sentence.)*

---

## 3. The module

```python
# The three the challenge requires
save(records: list[dict], namespace: str) -> dict
recall(query: str, namespace: str, k: int = 8) -> list[dict]
clear(namespace: str | None = None) -> dict
```

`recall()` returns a **list**, which matches `reason(observation, memory: list)`
exactly. That's not an accident — the signature was telling me the shape.

### Three namespaces

| Namespace | Holds | Used for |
|---|---|---|
| `claims` | every statement found so far | finding candidates to compare |
| `findings` | confirmed contradictions | the final report, and not re-reporting |
| `rejected` | pairs already checked and cleared | never re-checking the same pair |

`rejected` is worth explaining. When `reflect` decides a pair is *not* a contradiction
("that's tiered pricing"), it saves that. Three benefits:

1. **Cost** — the same pair is never sent to the LLM twice.
2. **Consistency** — the same false positive can't come back next run.
3. **It helps the loop finish** — repeating an action becomes provably useless, so
   the agent moves on instead of circling.

---

## 4. Where it plugs into the loop

Two lines change in `run_loop`, and they're the two the challenge specifies.

```python
for i in range(max_iterations):
    observation = perceive(payload)

    memory = memory_manager.recall(                    # ← READ, before reason
        query=observation["instruction"],
        namespace="claims",
        k=config.memory.recall_k,
    )

    plan       = reason(observation, memory)
    result     = act(plan, TOOLS)
    reflection = reflect(result, observation)

    memory_manager.save(...)                           # ← WRITE, after reflect
    #   new claims        → "claims"
    #   confirmed finding → "findings"
    #   rejected pair     → "rejected"
    #   the lesson        → "claims" (as a note)
```

Also: `find_related_claims` — the keyword-matching stub from Milestone 1 — now
becomes a real `recall()` call against the `claims` namespace. That's the upgrade
that makes the tool actually work.

---

## 5. The example I'll show in the video

This is the required "round N+1 informed by round N" proof, and it needs to be one
specific, repeatable thing.

**Round 2** — reads §2. Finds:

> `clm_17` — *"Audit logs are retained for 30 days."*

Saved to `claims`. Reflect's lesson: *"§2 says 'audit log'; other sections may say
'system log' — treat as possibly the same thing."* Also saved.

**Round 3** — reads §7. Finds:

> `clm_23` — *"System logs are kept for a minimum of 90 days."*

Calls `find_related_claims(clm_23)`. Memory returns `clm_17` — five pages back, from
a round that already finished. `compare_claims` runs on the pair.

**→ Contradiction found.**

### The ablation

I'll build a `--no-memory` flag purely for this. Run the same document twice:

```bash
python main.py --doc test/data/policy.md
python main.py --doc test/data/policy.md --no-memory
```

Same document, same seed. With memory: 2 findings. Without: 0. Nothing to argue
with, and it takes twenty seconds of video.

---

## 6. Tests

Landed as part of the single [test/test_agent.py](../test/test_agent.py), not a
separate file — no API key needed, `DictMemory` stands in for Chroma so the suite
stays offline.

| Test | Checks |
|---|---|
| `test_save_recall_clear` | the three required functions work, including round-1's empty store |
| `test_recalled_memory_reaches_the_prompt` | recalled text is actually shown to the model, not fetched and discarded — storage working isn't the same as memory mattering, and this is the test for the difference |
| `test_loop_recalls_before_reason_and_saves_after_reflect` | the read/write order the challenge specifies |
| `test_broken_memory_degrades_instead_of_stopping` | a dead store makes the run worse, not dead |

**Not an automated test:** the ablation itself — with-memory finding more than
without-memory — is checked by running the two CLI commands in §5, not by an
assertion. Worth being honest about that rather than claiming more coverage than
exists.

---

## 7. `MEMORY.md`

The challenge asks for three things. I already have all three above:

| Required | Where it comes from |
|---|---|
| Which memory tool I chose and why | §2 |
| How memory is structured | §3 — the three namespaces |
| A concrete example of memory in action | §5 — the clm_17 / clm_23 story |

Write it right after this milestone works. The §5 example should include the actual
log lines from a real run, not made-up ones.

---

## 8. Done when

- [ ] `save()`, `recall()`, `clear()` exist and work
- [ ] `recall()` is called before every `reason`, `save()` after every `reflect`
- [ ] The recalled text is visibly *in the prompt*, not just fetched
- [ ] The §5 example reproduces reliably on the test document
- [ ] `--no-memory` produces a visibly worse result on the same input
- [ ] A second run on the same document reuses the stored claims instead of re-reading
- [ ] `clear()` resets it so the demo is repeatable
- [ ] `pytest` passes

---

## 9. Questions I should be able to answer for this milestone

1. Why can't this agent work at all without memory?
2. Why Chroma and not mem0 or Zep — for *this* problem specifically?
3. What do the three namespaces each do?
4. What does the `rejected` namespace give me that a plain claim store doesn't?
5. Name one contradiction the agent finds **only** because memory exists.
6. Where exactly is memory read, and where is it written?
7. What happens on the second run over the same document?
8. How do I know memory reaches the LLM and isn't just fetched and dropped?

---

**Next:** [MILESTONE_3.md](MILESTONE_3.md) — the harness.
