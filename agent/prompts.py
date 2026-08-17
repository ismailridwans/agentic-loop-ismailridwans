"""One template per LLM call.

COMPARE is the one that decides whether this works. Most of it is the list of things
that are NOT contradictions, because that is the failure mode: a model asked to find
contradictions will report tiered pricing and unit conversions as conflicts.
"""

from __future__ import annotations

TYPES = """\
NUMBERS  same thing, different values.       "max 10 MB" vs "up to 25 MB"
RULES    same action, different obligation.  "must encrypt" vs "encryption is optional"
DATES    conflicting times.                  "effective Jan 1" vs "effective from April"
MEANING  one term defined two ways.          "active user", defined twice, differently
FACTS    same thing, different answer.       "owned by Team A" vs "owner: Team B"\
"""

GUARDS = """\
different_things  Different subjects — tiers, plans, regions, environments.
                  "Free: 5 GB" vs "Pro: 50 GB" is NOT a contradiction.
superseded        One explicitly replaces the other in time.
                  "Until 2024, X. From 2024, Y." is NOT a contradiction.
stated_exception  One declares itself an exception to the other. Look for
                  "does not apply to", "except", "other than".
                  A rule plus its own stated carve-out is NOT a contradiction.
same_value        Equal once units are converted, or rounded.
                  "10 MB" vs "10,485,760 bytes" is NOT a contradiction.\
"""


# --------------------------------------------------------------------------- #
# reason
# --------------------------------------------------------------------------- #

REASON_SYSTEM = f"""\
You are the planning step of an agent that finds places where a document
contradicts itself. Each round you pick exactly ONE tool. The result comes back to
you next round.

TOOLS
  extract_claims       Read chunks, pull out the checkable statements. Set chunk_ids.
  find_related_claims  Given a claim, find others about the same subject. Set claim_id.
  compare_claims       Decide whether two claims conflict. The only tool that
                       produces findings. Set claim_a and claim_b.

CLAIM TYPES
{TYPES}

HOW TO CHOOSE
- While chunks are unread: extract, and take EVERY chunk in the focus list in one
  call. Contradictions sit pages apart, so reading widely comes first. Comparing
  after three chunks compares neighbours and finds nothing.
- Once everything is read: work the CANDIDATE PAIRS list from the top. It is ranked
  by a filter that already scored shared subject, type and unit. Do not compare
  claims in ID order — clm_001 vs clm_002 is a guess, not a plan.
- Never re-extract a scanned chunk or re-compare a compared pair.

Set the fields your action needs. Set the rest to null.\
"""


def reason_user(observation: dict, memory: list) -> str:
    cov = observation["covered"]
    by_id = {c["id"]: c for c in observation["claims"]}

    def describe(cid: str) -> str:
        c = by_id.get(cid, {})
        value = f" [{c.get('value')}]" if c.get("value") else ""
        return f"§{c.get('section', '?')} ({c.get('type', '?')}) {c.get('subject', '')}{value}"

    lines = [
        f"ROUND {observation['iteration']}",
        f"INSTRUCTION FROM LAST ROUND: {observation['instruction']}",
        "",
        f"COVERAGE: {cov['scanned']}/{cov['total']} chunks read"
        + (f" — {cov['total'] - cov['scanned']} still unread" if cov["scanned"] < cov["total"]
           else " — everything is read, now compare"),
        "",
        "CHUNKS IN FOCUS (take all of them):",
    ]
    lines += [f"  {c['id']}  §{c['section'] or '-'}  {c['preview']}"
              for c in observation["focus_chunks"]] or ["  (none left)"]

    # The ranked shortlist. If this doesn't reach the prompt the model picks pairs
    # at random and the run finds nothing — which is exactly what happened the
    # first time it was left out.
    if observation["candidate_pairs"]:
        lines += ["", "CANDIDATE PAIRS — ranked, best first. Start here:"]
        for p in observation["candidate_pairs"]:
            lines += [
                f"  {p['a']} / {p['b']}  (score {p['score']})",
                f"      {p['a']}: {describe(p['a'])}",
                f"      {p['b']}: {describe(p['b'])}",
            ]

    if observation["claims"]:
        lines += ["", "ALL CLAIMS SO FAR:"]
        lines += [f"  {c['id']}  {describe(c['id'])}" for c in observation["claims"]]

    if observation["compared_pairs"]:
        lines += ["", "ALREADY COMPARED: "
                  + ", ".join(f"{a}/{b}" for a, b in observation["compared_pairs"])]

    # Empty in Milestone 1. The parameter exists because the signature requires it,
    # and so wiring memory_manager in later is a one-line change.
    if memory:
        lines += ["", "FROM MEMORY:"] + [f"  {m}" for m in memory]

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# extract_claims
# --------------------------------------------------------------------------- #

EXTRACT_SYSTEM = f"""\
Pull the checkable statements out of a piece of a document.

Checkable means it could later be found to disagree with a statement elsewhere in
the same document: retention periods, obligations, definitions, limits, ownership,
dates. Not checkable: headings, transitions, vague statements with nothing
measurable in them.

For each one:
  quote      The sentence copied VERBATIM from the text below. Do not paraphrase or
             join two sentences. If you cannot copy it exactly, leave it out.
  type       One of:
{TYPES}
  subject    What it is about, normalised. Two claims about the same thing must get
             the same subject string. Prefer "system log retention" to "logs".
  value_raw  The measurable value if any, e.g. "30 days", "5 GB". Null otherwise.

Return an empty list if there is nothing checkable.\
"""


def extract_user(chunks: list[dict]) -> str:
    return "TEXT:\n\n" + "\n\n".join(
        f"[{c['id']}] Section {c['section'] or '-'} — {c['heading']}\n{c['text']}"
        for c in chunks
    )


# --------------------------------------------------------------------------- #
# compare_claims — the one that matters
# --------------------------------------------------------------------------- #

COMPARE_SYSTEM = f"""\
Decide whether two statements from the SAME document contradict each other. Work in
this order.

STEP 1 — SAME SUBJECT?
If they are about different tiers, plans, regions, environments or entities they
cannot contradict each other. Stop: same_subject=false, no_contradiction.

STEP 2 — DOES A GUARD APPLY?
These look like conflicts and are not. Check every one:

{GUARDS}

If any applies, name it and return no_contradiction.

STEP 3 — ONLY THEN
Same subject, no guard, and they cannot both be true as written. Classify it:

{TYPES}

BIAS: be reluctant. A false alarm is worse than a miss — a report that flags things
that aren't wrong gets ignored entirely. When unsure, return no_contradiction with
confidence below 0.5.\
"""


def compare_user(a: dict, b: dict, arithmetic: str | None = None) -> str:
    lines = [
        "STATEMENT A",
        f"  Section: {a['section']}",
        f"  Subject: {a['subject']}",
        f"  Text:    {a['text']}",
        "",
        "STATEMENT B",
        f"  Section: {b['section']}",
        f"  Subject: {b['subject']}",
        f"  Text:    {b['text']}",
    ]
    if arithmetic:
        # Already computed in Python, so the model never does arithmetic itself.
        lines += ["", f"ARITHMETIC (already computed, treat as fact): {arithmetic}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# reflect
# --------------------------------------------------------------------------- #

REFLECT_SYSTEM = """\
You are the reflection step. You have just seen the result of one round.

is_done           True only if the whole document has been read AND the promising
                  pairs have been compared. False if anything is unread.
quality_score     0.0 to 1.0 — how well this run is going, based on coverage and
                  whether recent rounds produced anything.
next_instruction  A specific directive for the next round. Name sections or claim
                  IDs: "Compare clm_004 against clm_011" beats "keep looking". If
                  pairs are listed below, name the top unexamined one. If a round
                  produced nothing, say what to try instead — do not repeat the
                  last instruction.\
"""


def reflect_user(result: dict, observation: dict, outcome: str) -> str:
    cov = observation["covered"]
    lines = [
        f"ROUND {observation['iteration']}",
        f"ACTION: {result.get('tool')}   SUCCEEDED: {result.get('ok')}",
        f"OUTCOME: {outcome}",
        "",
        f"COVERAGE: {cov['scanned']}/{cov['total']} chunks",
        f"CLAIMS: {len(observation['claims'])}   "
        f"PAIRS COMPARED: {len(observation['compared_pairs'])}   "
        f"FINDINGS: {observation['findings_count']}",
        "",
        f"LAST INSTRUCTION: {observation['instruction']}",
    ]

    if observation["candidate_pairs"]:
        lines += ["", "TOP UNEXAMINED PAIRS:"]
        lines += [f"  {p['a']} / {p['b']} ({p['score']})"
                  for p in observation["candidate_pairs"][:5]]
    if cov["scanned"] < cov["total"]:
        lines += ["", "Chunks are still unread — send the next round back to reading."]

    return "\n".join(lines)
