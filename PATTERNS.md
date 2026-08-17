# PATTERNS.md

Which agentic patterns I looked at, which ones I built with, and why the ones I
skipped were the right thing to skip.

The use case throughout: **find the places where a document contradicts itself**.
See [PLAN.md](PLAN.md) for what that means and why it needs a loop at all.

---

## The five patterns

### ReAct

ReAct interleaves reasoning and acting in a single loop: the model thinks about what
to do, takes one action, observes the result, and thinks again with that result in
hand. The key property is that it does **not** plan the whole task up front. Each
action is chosen with knowledge of everything that happened before it, so the agent
can respond to what it actually finds rather than what it expected to find. The cost
is that it's serial — every step waits on the last one — and a bad early observation
can send the whole trajectory sideways.

### Reflexion

Reflexion adds a self-critique step after acting. Instead of only carrying forward
the *result* of an action, the agent writes down a verbal judgement of how the
attempt went, and that critique becomes part of the context for the next attempt.
It's a feedback signal in natural language rather than in weights — no training
involved. It works best when the agent can be wrong in ways it is capable of noticing
on a second look, and it's wasted effort when the task has no meaningful notion of
"that attempt was poor."

### Chain-of-Thought

Chain-of-Thought is the simplest of the five: make the model write out its
intermediate reasoning before committing to an answer. It isn't an agent
architecture at all — it's a prompting technique that sits inside one call. It
improves multi-step reasoning and, just as usefully, makes the model's decision
legible to whoever reads the log afterwards. It does not by itself give you tool use,
memory, or iteration.

### Tree of Thoughts

Tree of Thoughts generalises Chain-of-Thought from a line to a tree. At each step the
model generates several candidate continuations, an evaluator scores them, and the
search keeps the promising branches and abandons the rest — with backtracking when a
branch dead-ends. It's the right shape for problems where you must *explore* before
you can tell which approach was correct (puzzles, planning, creative search). The
price is multiplicative: every branching factor multiplies the number of LLM calls.

### LATS

LATS (Language Agent Tree Search) is Tree of Thoughts with a real search algorithm
bolted on — Monte Carlo Tree Search over the branches, with the environment's actual
feedback and the model's self-reflection both used as the value signal. It unifies
reasoning, acting, and planning into one search. It's the most capable of the five
and by a wide margin the most expensive; it makes sense when a wrong trajectory is
costly and you can afford dozens of rollouts to avoid one.

---

## What I built with

**ReAct — the backbone.** The four functions the challenge specifies are a ReAct
cycle with its steps named out loud:

| ReAct | Mine |
|---|---|
| observation | `perceive` |
| thought → action | `reason` |
| act | `act` |
| observation feeding the next thought | `reflect` → `next_instruction` → `perceive` |

The reason ReAct fits is the one from [PLAN.md §2](PLAN.md): a 30-page document has
~200 statements, which is ~20,000 pairs. I can't put that in one prompt, so the agent
has to *search* the document — read a part, see what it found, decide where to look
next. "Decide where to look next, given what I just saw" is exactly the loop ReAct
describes. A fixed plan made in round 1 couldn't do it, because in round 1 the agent
hasn't read the document yet.

**Reflexion — in `reflect`, twice.**

*At the finding level.* When `act` surfaces a possible contradiction, `reflect` tries
to argue itself **out** of it, using the "not contradictions" table in
[PLAN.md §3](PLAN.md) as the rubric — different pricing tiers, a superseded clause, a
stated exception, the same number in different units. Only a finding that survives
its own rebuttal gets recorded. This is the precision step, and without it the agent
flags tiered pricing as a conflict.

*At the loop level.* `reflect` also writes a `lesson` — *"§6 uses tier headings,
always read the heading with the sentence"* — which is saved to memory and shown to
`reason` on the next round. That's the verbal-feedback half of Reflexion: a critique
in words, carried forward.

**Chain-of-Thought — the `reasoning` field.** `reason` returns its justification
alongside its chosen tool, in the same schema object, so the thinking is produced
before the commitment and is kept in the run log. Two payoffs: better tool choices,
and a log I can read afterwards to see *why* the agent did what it did — which is
most of how I debugged the thing.

---

## What I skipped, and why

**Tree of Thoughts — skipped deliberately.**

ToT would mean exploring several different ways to judge each candidate pair of
claims and scoring them against each other. That's the wrong tool for this judgement,
for two separate reasons:

- **The numeric half doesn't need search.** For a `NUMBERS` contradiction I normalise
  both values to a common unit in plain Python and compare with `!=`. That's not a
  judgement with a distribution of plausible answers — it's arithmetic, and it's
  already certain. Branching over it buys nothing.
- **The language half needs an adversary, not a survey.** For `RULES` and `MEANING`,
  the second opinion I actually want is *"try to explain this away"*, not *"generate
  three more opinions and average them."* The adversarial check in `reflect` is
  cheaper, more targeted, and aimed at my known failure mode — false positives — in a
  way that a breadth search isn't.

There's also a structural point. ToT pays off when a wrong branch is expensive and
hard to detect. Here a wrong branch is cheap: a bad candidate pair costs one
`compare_claims` call, gets rejected, and lands in the `rejected` namespace so it's
never tried again. Memory already gives me most of what backtracking would.

**LATS — skipped for the same reasons, more so.** LATS is ToT plus MCTS, so it
inherits every objection above and multiplies the cost. Dozens of rollouts per
decision is a defensible price when a wrong trajectory is unrecoverable. Mine is
recoverable in one round.

> Declining ToT with a reason is a stronger answer than adopting it because it sounds
> impressive — but only if I can say the reason out loud, which is why it's written
> down here rather than left implied.

---

## Summary

| Pattern | Used? | Where |
|---|---|---|
| ReAct | ✅ backbone | The `perceive → reason → act → reflect` cycle in [agent/loop.py](agent/loop.py) |
| Reflexion | ✅ twice | `reflect`: argue against the finding; write a `lesson` for the next round |
| Chain-of-Thought | ✅ | The `reasoning` field in the plan schema |
| Tree of Thoughts | ❌ | Numeric half is `!=`; language half wants an adversary, not a survey |
| LATS | ❌ | ToT's costs, multiplied, for a problem where wrong branches are cheap |

**One sentence:** it's a ReAct loop whose reflect step does Reflexion, with
Chain-of-Thought inside the single reasoning call — and the tree-search patterns were
declined because the expensive part of this problem is precision, not exploration.
