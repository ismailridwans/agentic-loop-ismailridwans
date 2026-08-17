# Narration script — how my loop works

Notes to speak from, not to read out. Plain words, in the order that makes sense
when explaining it to someone.

---

## 1. What it does (20 seconds)

> "My use case is detecting contradictions inside a document. You give it a policy
> document, it gives back the places where the document disagrees with itself, with
> the exact quotes and the section numbers."
>
> "It only checks the document against itself. It doesn't fact-check against the
> outside world. That keeps every answer verifiable — the evidence is right there in
> the text."

---

## 2. Why this needs a loop (30 seconds)

This is the question they will ask, so lead with it.

> "The obvious question is why not just one big prompt. Here's why."
>
> "A contradiction is two statements in different places. So you have to compare
> statements against each other, in pairs. My test document has 23 chunks and about
> 20 claims. A real 30-page policy has around 200 claims — that's about 20,000
> possible pairs. You can't put 20,000 comparisons in one prompt."
>
> "So the loop is a search strategy. Each round it either reads a new part of the
> document, or tests one promising pair. Then it decides where to look next. That
> decision is the whole reason a loop exists here."

---

## 3. The four steps (90 seconds)

Go one at a time. The thing to stress is that each does genuinely different work.

**Perceive**

> "Perceive reads and organises. There's no LLM call in it at all — it's plain
> Python."
>
> "It splits the document into numbered chunks and records exactly where each one
> starts and ends in the raw text. Those character positions matter later — every
> claim is pinned to them, so I can prove a quote is real by slicing the original
> document at that offset."
>
> "It also tracks how much has been read, and ranks which pairs of claims are worth
> comparing."

**Reason**

> "Reason is the one LLM call per round. It sees where we are, what's left, what's
> in memory, and the list of tools. It picks exactly one tool and says why."
>
> "I use Groq's strict structured output here, so the model physically cannot return
> something that breaks my schema."

**Act**

> "Act just runs the tool. No decisions. It validates the parameters against the
> tool's JSON schema first, so a bad plan fails cheaply instead of crashing inside a
> handler."
>
> "Every tool returns the same shape — ok, data, error, timing. So if a tool fails,
> that's not a crash, it's just `ok: false` flowing into reflect. The agent gets to
> react to it and route around it."

**Reflect**

> "Reflect does two things. If a contradiction was found, it records it. Then it
> decides — are we done, and what should the next round do."
>
> "One detail I'd point out: the model advises but coverage decides. If the model
> says 'I'm done' but there are still unread chunks, I override it. It's not done."

**The feedback edge**

> "Then reflect's instruction goes back into the next perceive. The challenge
> specifies that perceive takes a string, so I serialise the state into a small JSON
> envelope to get it back in."
>
> "That's the loop. And it's the one thing I have a dedicated test for — if that
> line breaks, you've got four function calls in a row, not a loop. It actually
> caught a real regression when I refactored."

---

## 4. The three tools (45 seconds)

> "Three tools. Extract claims from chunks. Find claims about the same subject.
> Compare two claims."
>
> "The interesting one is compare. I split the job: Python does the arithmetic, the
> model does the language."
>
> "So if one section says 30 days and another says 720 hours, Python normalises both
> to seconds and knows they're identical. The model never does arithmetic. It only
> answers the question it's actually good at — are these two statements about the
> same thing?"
>
> "And that split is one-directional on purpose. Equal numbers prove there's no
> contradiction, with certainty. But different numbers prove nothing on their own —
> 5 GB and 50 GB look like a conflict until you notice they're different pricing
> tiers."

---

## 5. Precision — the hard part (45 seconds)

Worth spending time on. It's what separates this from a toy.

> "The hard part isn't finding contradictions. It's not inventing them."
>
> "If you just ask a model to find contradictions, it flags everything. Tiered
> pricing. A clause that was superseded in 2024. A rule with a stated exception. Ten
> megabytes versus ten million bytes."
>
> "So most of my compare prompt is actually a list of things that are NOT
> contradictions. Four guards — different subjects, superseded, stated exception,
> same value in different units."
>
> "My test document has three real contradictions planted in it, and two traps. The
> traps are the real test. Anyone can score well on finding contradictions by
> flagging everything."

---

## 6. Memory (30 seconds)

> "For memory I used ChromaDB, running locally."
>
> "The reason is specific to this problem, not a general preference. The thing the
> agent needs to do most often is 'find claims similar to this one'. That's exactly
> a vector search. So the store isn't decoration bolted on to tick a box — it's the
> search engine."
>
> "Three namespaces. Claims, findings, and rejected pairs. That third one matters —
> once the agent decides two claims don't conflict, it remembers that, so it never
> pays to check the same pair twice."
>
> "Memory is read before every reason call and written after every reflect. And
> without it this task is genuinely impossible — a contradiction is two statements in
> different places, so if round three can't see what round two found, there's nothing
> to compare."

---

## 7. Harness (45 seconds)

> "The harness is everything that makes it survive running unsupervised."
>
> "Retry with exponential backoff and jitter — but per error class, not blanket. A
> 429 rate limit gets retried. A 400 bad request does not, because a malformed
> request will be malformed the second time too. Retrying that just fails slower."
>
> "For unparseable JSON there's a five-step ladder. Try to parse. Dig the JSON out of
> the prose. Ask again showing the error. Ask again simpler. And if all of that
> fails, fall back to a hardcoded plan — so the loop never dies from a bad parse."
>
> "Guardrails: max iterations, a token budget, and stuck detection. And a quote check
> — if a finding quotes text that isn't in the document, it gets dropped. That makes
> a hallucinated quote structurally impossible to report, not just unlikely."
>
> "Whatever happens, it returns a report. Complete, partial, stuck, or budget
> exceeded — it never returns nothing."

---

## 8. What I'd do differently (30 seconds)

Say this. It reads as confidence, not weakness.

> "The precision side works — it correctly refuses both my planted traps."
>
> "Recall is where it's still weak. The candidate ranking is word overlap on the
> subject strings the model writes. So if it labels one claim 'system log retention'
> and another 'log retention period', they score lower than they should and might
> never get compared."
>
> "The fix is sitting right there — I already have a vector store for memory. I
> should be ranking candidate pairs with vector similarity instead of word overlap.
> That's the first thing I'd change."

---

## If they ask

**"Why did you pick that model?"**
> "Groq, gpt-oss-20b. Not for the usual reasons — on Groq only the gpt-oss models
> support strict structured outputs. My loop depends on reason returning a
> parseable plan every single round, so the model choice followed from the
> architecture."
>
> "I actually planned a split — gpt-oss-120b for the reasoning calls, 20b for bulk
> extraction — but 120b's daily quota ran out on the free tier while I was testing,
> so everything runs on 20b right now. Same field in config.yaml either way."

**"Which agentic pattern is this?"**
> "ReAct as the backbone — think, act, observe, repeat. Reflexion in the reflect
> step. Chain of thought is the reasoning field, which I keep in the log so I can
> read why it did what it did. I looked at Tree of Thoughts and skipped it — it
> multiplies cost exploring several ways to judge each pair, and for the numeric half
> a Python comparison already gives me a certain answer."

**"How do you know memory actually does anything?"**
> "There's a `--no-memory` flag. Same document, run it both ways, and the
> cross-section findings disappear. And there's a test asserting that recalled text
> actually reaches the prompt — because fetching memory and never showing it to the
> model would be a no-op that still passes every storage test."

**"What broke while building it?"**
> "Three things worth mentioning. A refactor dropped one line and severed the
> feedback edge — every round was silently re-reading round one. My config loader was
> shallow-copying its defaults, so every override permanently mutated global state.
> And my stuck detection was hashing the reflection text, which killed working runs —
> because while you're reading a document, 'keep reading' is the correct instruction
> several rounds in a row. I fixed it to include progress counters, so stuck means
> nothing moved, not the model repeated itself."

**"Why only three tools?"**
> "Three is what the problem actually needs — read statements out, find related ones,
> judge a pair. The challenge asks for at least two. I'd rather have three I can
> defend than seven for show."
