# Demo narration script

Roughly 4 minutes. Written to be spoken, not read — short sentences, no long
clauses. Record each section separately and stitch them; that way one fluffed line
doesn't cost you the whole take.

The four things the challenge requires the video to show are marked **[REQUIRED]**.

---

## Before you hit record

```bash
python main.py --doc test/data/policy.md --clear-memory --model openai/gpt-oss-20b
```

Do one practice run first. You need to know what it's going to print, because you're
narrating over it. Have a second terminal ready for the memory and failure sections.

---

## 0:00 — What it does  (20 sec)

> "My use case is detecting contradictions inside a document.
>
> You give it a document. It reads through, pulls out the checkable statements, and
> tells you where the document disagrees with itself.
>
> The test document here is a made-up company policy. I planted three real
> contradictions in it, and two things that *look* like contradictions but aren't.
> Both of those matter, and I'll come back to why."

*[ON SCREEN: `test/data/policy.md` open, scroll it briefly]*

---

## 0:20 — Why this needs a loop  (30 sec)

> "First — why an agent loop, and not just one big prompt?
>
> A contradiction is always two statements, in two different places. So you're not
> checking statements, you're checking *pairs* of statements.
>
> This document has about twenty claims in it. That's already two hundred possible
> pairs. A thirty-page document has two hundred claims, which is twenty thousand
> pairs. You cannot put that in one prompt.
>
> So the loop is the search strategy. Each round it reads one part of the document,
> or it tests one specific pair. Then it decides where to look next. That decision is
> the whole reason the loop exists."

---

## 0:50 — The four steps  (40 sec)

*[ON SCREEN: `agent/loop.py`, scroll slowly past each function]*

> "Four functions, exactly as the challenge specifies.
>
> **Perceive** splits the document into numbered chunks and records exactly where
> each one starts and ends in the source text. There's no LLM call here — it's all
> plain Python. Those character positions matter later.
>
> **Reason** is the one LLM call per round. It sees where we are and what we've
> found, and it picks one tool.
>
> **Act** runs that tool. It validates the parameters against the tool's JSON schema
> first, so a bad plan fails cheaply instead of crashing inside a handler.
>
> **Reflect** decides whether we're done, and writes the instruction for the next
> round.
>
> And this is the part worth pointing at — perceive takes a *string*. So reflect's
> output has to be serialised back into it. That's the feedback edge, and it's the
> difference between a loop and four function calls in a row."

*[ON SCREEN: the `payload = json.dumps({...})` line at the bottom of `run_loop`]*

> "I actually broke this line during a rewrite once. Every round silently
> re-perceived round one. There's a test called `test_reflect_feeds_the_next_round`
> that exists purely to catch that."

---

## 1:30 — The live run  **[REQUIRED: 3+ iterations]**  (90 sec)

*[ON SCREEN: terminal, run the command]*

Narrate over the first three rounds. Something like:

> "Round one. Perceive gives it the first chunks. Reason picks `extract_claims`, and
> it says why — you can see its reasoning printed there. Act runs it, and reflect
> reports back: four chunks read, six claims found.
>
> And there's the instruction for the next round.
>
> Round two — and notice, that instruction it just wrote is now the instruction
> perceive is working from. That's the feedback edge doing its job.
>
> Round three, same shape. It's still reading, because it can't compare two
> statements until it's read both of them."

Then, once it switches to comparing:

> "Now it's read enough, so it switches to comparing. And this is where the
> deterministic part earns its place.
>
> Notice `compare_claims` — the numbers are normalised and compared in **Python**,
> not by the model. Thirty days and seven hundred and twenty hours are the same
> quantity, and Python knows that for certain. The model is only ever asked the
> language question: *are these two statements about the same thing?*
>
> Arithmetic can rule a contradiction out with certainty. It can never rule one in —
> because two different numbers about two different things aren't a conflict."

---

## 3:00 — Memory  **[REQUIRED]**  (40 sec)

*[ON SCREEN: point at a round where recalled claims appear in the prompt]*

> "Memory here isn't an add-on. Without it this task is impossible.
>
> The two halves of a contradiction can be five pages apart. Round eight has to be
> able to reach back to what round two saw. If each round starts blind, there's
> nothing to compare against and the agent finds nothing, ever.
>
> So it's a local Chroma vector store. Claims get saved after every reflect, and
> recalled before every reason. Three namespaces — claims, findings, and rejected
> pairs, so it never re-judges a pair it's already cleared."

*[Second terminal]*

```bash
python main.py --doc test/data/policy.md --no-memory --model openai/gpt-oss-20b
```

> "Same document, memory switched off. It re-reads, it can't connect anything across
> sections, and the cross-section findings just aren't there."

---

## 3:40 — A failure, handled  **[REQUIRED]**  (30 sec)

```bash
python main.py --doc test/data/policy.md --inject-failure rate_limit
```

> "For failure handling — I built failure injection in, so I can show this instead of
> just describing it.
>
> That's a forced rate limit. You can see it backing off, and the run continues.
>
> The retry policy is per error class, which is the bit I'd defend. A 429 gets
> retried, because it's temporary. A 400 gets *no* retry at all — a malformed request
> will be malformed the second time too, so retrying it only fails slower."

Optionally, if you have time:

```bash
python main.py --doc test/data/policy.md --inject-failure tool_error
```

> "And a failing tool doesn't crash the run. It comes back as a normal result with
> `ok` set to false, reflect sees it, and the next round works around it."

---

## 4:10 — Honest close  (20 sec)

> "One thing that doesn't work as well as I'd like.
>
> The precision side works — it correctly refuses the two traps I planted. It knows
> tiered pricing isn't a contradiction, and it knows a rule plus its own stated
> exception isn't one either.
>
> Recall is weaker. The candidate ranking is word overlap on the subject strings the
> model writes. So if it calls one claim 'system log retention' and another 'log
> retention period', they score lower than they should, and the pair might not
> surface before the iteration budget runs out.
>
> The fix is to rank candidates with the vector store instead of word overlap —
> which is what the vector store is already there for. That's the first thing I'd do
> next."

---

## Notes

**Be honest in the close.** The brief says outright that they want "clear thinking,
honest engineering decisions" and not perfect code. Naming a real limitation and the
specific fix reads as confidence. Claiming it all works when the run in front of them
shows otherwise does the opposite.

**If the run doesn't find contradictions on camera**, don't hide it. Say: *"this run
didn't surface the pair — here's why, and here's the fix."* Then show a run that did,
or the report JSON from one. A reviewer who has built agents will recognise this
problem, and will trust you more for naming it.

**Set the video sharing to "anyone with the link".** The brief explicitly warns that
reviewers must not have to request access.
