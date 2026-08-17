"""The core loop: perceive -> reason -> act -> reflect, then round again.

The four signatures are fixed by the challenge and used exactly as written. Two
things follow from that:

1. perceive() takes a str, so reflect's output has to be serialised to get back in.
   That is the JSON envelope at the bottom of run_loop — the feedback edge, made
   explicit rather than smuggled through a global.

2. Because only a string comes in, perceive has to look the run's state back up.
   Hence _RUNS. A cost of honouring the signature, not an accident.

Each function also takes optional keyword-only arguments with defaults, so it stays
callable exactly as specified while tests inject fakes.

Milestone 2 adds the two memory calls: recall() before reason, save() after reflect.
Milestone 3 adds the harness around the whole thing — retries live in llm.py, the
guardrails and statuses live in run_loop.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agent import harness, memory_manager, prompts, segmenter
from agent.llm import LLMParseError
from agent.logger import Timer
from agent.schemas import PLAN_SCHEMA, REFLECTION_SCHEMA
from agent.tools import State, build_tools, candidate_pairs

_RUNS: dict[str, State] = {}

FIRST_INSTRUCTION = "Start reading the document from the beginning and pull out the claims."
DEFAULT_FOCUS_SIZE = 6


def get_state(run_id: str) -> State:
    return _RUNS[run_id]


def reset() -> None:
    _RUNS.clear()


# --------------------------------------------------------------------------- #
# 1. PERCEIVE
# --------------------------------------------------------------------------- #

# "§7.4", "section 7.4", and what a model actually writes: "sections 3 and 5".
_SECTION_REF = re.compile(
    r"(?:§|sections?\s+)(\d+(?:\.\d+)*(?:\s*(?:,|and|&)\s*\d+(?:\.\d+)*)*)", re.I
)


def sections_mentioned(text: str) -> list[str]:
    found: list[str] = []
    for group in _SECTION_REF.findall(text):
        for part in re.split(r"\s*(?:,|and|&)\s*", group, flags=re.I):
            if part.strip() and part.strip() not in found:
                found.append(part.strip())
    return found


def perceive(input_data: str, *, focus_size: int = DEFAULT_FOCUS_SIZE) -> dict[str, Any]:
    """Parse and structure the input. No LLM call — plain Python.

    Round 1: input_data is a document path. Round 2+: it is the JSON envelope the
    previous round produced.
    """
    try:
        envelope = json.loads(input_data)
        assert isinstance(envelope, dict) and "run_id" in envelope
    except (json.JSONDecodeError, AssertionError):
        envelope = None

    if envelope is None:
        raw, chunks = segmenter.load(input_data)
        run_id = "run_" + segmenter.doc_id(raw)
        _RUNS[run_id] = State(path=input_data, raw=raw, chunks=chunks)
        iteration, instruction = 1, FIRST_INSTRUCTION
    else:
        run_id = envelope["run_id"]
        iteration = envelope["iteration"]
        instruction = envelope["instruction"]

    state = _RUNS[run_id]
    unscanned = state.unscanned()

    # Where to look next. Deterministic: honour a section the instruction names,
    # otherwise take the next few unread chunks in document order.
    focus: list[dict] = []
    for section in sections_mentioned(instruction):
        focus += [c for c in state.in_section(section) if c["id"] not in state.scanned]
    focus = focus or unscanned[:focus_size]

    pairs = candidate_pairs(state)

    return {
        "run_id": run_id,
        "iteration": iteration,
        "intent": "find_contradictions",
        "instruction": instruction,
        "focus_chunks": [
            {"id": c["id"], "section": c["section"],
             "preview": c["text"][:100].replace("\n", " ")}
            for c in focus[:focus_size]
        ],
        "claims": list(state.claims.values()),
        "candidate_pairs": pairs[:8],
        "compared_pairs": sorted(state.compared),
        "covered": state.coverage(),
        "findings_count": len(state.findings),
        # Deterministic termination: nothing to read and nothing worth comparing.
        "work_left": bool(unscanned) or bool(pairs),
    }


# --------------------------------------------------------------------------- #
# 2. REASON
# --------------------------------------------------------------------------- #


def _fallback_plan(observation: dict) -> dict[str, Any]:
    """Rung 5 of the JSON ladder: the model is unusable, so pick something sensible
    without it. Not smart, but the loop keeps going, which is the point."""
    if observation["focus_chunks"]:
        return {"action": "extract_claims",
                "params": {"chunk_ids": [c["id"] for c in observation["focus_chunks"]]},
                "reasoning": "fallback: model output unusable, reading the next chunks"}
    if observation["candidate_pairs"]:
        best = observation["candidate_pairs"][0]
        return {"action": "compare_claims",
                "params": {"claim_a": best["a"], "claim_b": best["b"]},
                "reasoning": "fallback: model output unusable, taking the top ranked pair"}
    return {"action": "find_related_claims",
            "params": {"claim_id": observation["claims"][0]["id"]},
            "reasoning": "fallback: model output unusable"}


def reason(observation: dict, memory: list, *, llm: Any = None,
           logger: Any = None) -> dict[str, Any]:
    """Ask the model what to do next. The one planning call per round.

    `memory` is the list recall() returned. It is passed straight into the prompt —
    fetching it without showing it to the model would be pointless.
    """
    try:
        reply = llm.complete_json(
            system=prompts.REASON_SYSTEM,
            user=prompts.reason_user(observation, memory),
            schema=PLAN_SCHEMA,
            name="plan",
        )
    except LLMParseError as exc:
        if logger:
            logger.warn(f"reason: falling back to a fixed plan ({exc})")
        return _fallback_plan(observation)

    # Unflatten. The wire format keeps params at the top level because strict mode
    # needs one fixed shape; internally a plan is {action, params, reasoning}.
    action = reply["action"]
    params: dict[str, Any] = {}
    if action == "extract_claims":
        params = {"chunk_ids": reply.get("chunk_ids") or []}
    elif action == "find_related_claims":
        params = {"claim_id": reply.get("claim_id") or ""}
    elif action == "compare_claims":
        params = {"claim_a": reply.get("claim_a") or "", "claim_b": reply.get("claim_b") or ""}

    return {"action": action, "params": params, "reasoning": reply["reasoning"]}


# --------------------------------------------------------------------------- #
# 3. ACT
# --------------------------------------------------------------------------- #


def act(plan: dict, tools: dict) -> dict[str, Any]:
    """Run the planned tool. Dispatch only.

    Params are validated against the tool's schema before the handler runs, so a
    bad plan fails cheaply instead of crashing somewhere inside.
    """
    from jsonschema import ValidationError, validate

    name = plan.get("action")
    entry = tools.get(name)

    def fail(cls: str, msg: str) -> dict:
        return {"ok": False, "tool": name, "data": {},
                "error": {"class": cls, "message": msg}, "ms": 0}

    if entry is None:
        return fail("UnknownTool", f"no tool named {name!r}")

    try:
        validate(instance=plan.get("params", {}), schema=entry["schema"]["parameters"])
    except ValidationError as exc:
        return fail("InvalidParams", exc.message)

    return entry["handler"](plan["params"])


# --------------------------------------------------------------------------- #
# 4. REFLECT
# --------------------------------------------------------------------------- #


def _describe(result: dict) -> str:
    if not result["ok"]:
        return f"FAILED: {result['error']['class']} — {result['error']['message']}"

    data, tool = result["data"], result["tool"]
    if tool == "extract_claims":
        dropped = data["claims_dropped_unverifiable"]
        extra = f", {dropped} dropped as unverifiable" if dropped else ""
        return (f"read {len(data['chunks_scanned'])} chunks, "
                f"found {len(data['claims_added'])} claims{extra}")
    if tool == "find_related_claims":
        return f"found {len(data['related'])} claims related to {data['claim_id']}"
    if data["is_contradiction"]:
        return (f"CONTRADICTION ({data['type']}) between "
                f"§{data['sections'][0]} and §{data['sections'][1]}")
    why = data["guard"] if data["guard"] != "none" else (
        "different subjects" if not data["same_subject"] else "no conflict")
    return f"no contradiction between {data['claim_a']} and {data['claim_b']} ({why})"


def reflect(result: dict, observation: dict, *, llm: Any = None) -> dict[str, Any]:
    """Evaluate the round: was a finding produced, are we done, what next."""
    state = _RUNS[observation["run_id"]]
    finding = None

    if result["ok"] and result["tool"] == "compare_claims" and result["data"]["is_contradiction"]:
        d = result["data"]
        finding = {
            "id": f"fnd_{len(state.findings) + 1:03d}",
            "type": d["type"],
            "sections": d["sections"],
            "claims": [d["claim_a"], d["claim_b"]],
            "quotes": d["quotes"],
            "confidence": d["confidence"],
            "explanation": d["reasoning"],
        }
        state.findings.append(finding)

    outcome = _describe(result)

    try:
        verdict = llm.complete_json(
            system=prompts.REFLECT_SYSTEM,
            user=prompts.reflect_user(result, observation, outcome),
            schema=REFLECTION_SCHEMA,
            name="reflection",
        )
    except LLMParseError:
        verdict = {"is_done": False, "quality_score": 0.5,
                   "next_instruction": "Continue reading the document."}

    # The model advises; coverage decides. Unread chunks mean not done whatever it
    # says, and nothing left to do means done whatever it says.
    is_done = bool(verdict["is_done"])
    if not observation["work_left"]:
        is_done = True
    elif observation["covered"]["scanned"] < observation["covered"]["total"]:
        is_done = False

    reflection = {
        "is_done": is_done,
        "quality_score": float(verdict["quality_score"]),
        "next_instruction": verdict["next_instruction"],
        "finding": finding,
        "outcome": outcome,
        # What actually moved this round. Part of the reflection because it is what
        # the reflection was judging — and it is what makes stuck detection mean
        # "nothing changed" rather than "the model repeated itself".
        "progress": {
            "scanned": state.coverage()["scanned"],
            "claims": len(state.claims),
            "compared": len(state.compared),
            "findings": len(state.findings),
        },
    }
    reflection["fingerprint"] = harness.fingerprint(reflection)
    return reflection


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def run_loop(doc_path: str, llm: Any, *, cfg: Any = None, memory: Any = None,
             logger: Any = None, faults: harness.Faults | None = None) -> dict[str, Any]:
    """Run rounds until reflection says done, a guardrail fires, or the cap is hit.

    Whatever happens, a report comes back. The agent never returns nothing.
    """
    if cfg is None:
        from agent import config
        cfg = config.load()

    memory = memory if memory is not None else memory_manager.NullMemory()
    rails = harness.Guardrails(max_iterations=cfg.loop.max_iterations,
                               max_tokens=cfg.budget.max_tokens,
                               warn_at=cfg.budget.warn_at, logger=logger)

    payload = doc_path
    status = harness.PARTIAL
    iterations = 0
    trace: list[dict] = []
    state: State | None = None
    degraded = False

    for i in range(cfg.loop.max_iterations):
        iterations = i + 1
        tokens = llm.usage().get("tokens_total", 0) if hasattr(llm, "usage") else 0

        stop = rails.before_round(iterations, tokens)
        if stop:
            status = stop
            break

        observation = perceive(payload, focus_size=cfg.loop.focus_size)
        state = _RUNS[observation["run_id"]]
        state.llm = llm
        state.faults = faults

        if not observation["work_left"]:
            status = harness.COMPLETE
            break

        if logger:
            logger.round_header(iterations, observation["covered"],
                                len(observation["claims"]), observation["findings_count"])
            logger.step(iteration=iterations, step="perceive",
                        summary_in=observation["instruction"],
                        summary_out=f"{len(observation['focus_chunks'])} chunks in focus, "
                                    f"{len(observation['candidate_pairs'])} candidate pairs")
            logger.say("perceive", observation["instruction"])

        # ---- MEMORY READ, before reason (Milestone 2) --------------------
        recalled: list[dict] = []
        try:
            if faults:
                faults.memory_read()
            hits = memory.recall(observation["instruction"], memory_manager.CLAIMS,
                                 k=cfg.memory.recall_k)
            recalled = [f"{h.get('section', '?')}: {h.get('text', '')}" for h in hits]
        except Exception as exc:  # noqa: BLE001 — memory is optional, the run is not
            degraded = True
            if logger:
                logger.warn(f"memory read failed, continuing without recall: {exc}")

        # An LLM call that survives harness.call_with_retry's own attempts (a
        # rate limit that outlasts the retry budget, say) raises here. Round
        # 1..N of work already happened and is sitting in `state` — losing it
        # by letting the exception escape to main.py would break the one rule
        # that matters most: the agent never returns nothing. So it's caught
        # here, where `state` is still in scope, and folded into the same
        # report-building code every other ending goes through.
        try:
            with Timer() as t:
                plan = reason(observation, recalled, llm=llm, logger=logger)
            if logger:
                logger.step(iteration=iterations, step="reason",
                            summary_in=f"{len(recalled)} memories recalled",
                            summary_out=f"{plan['action']} {plan['params']}", ms=t.ms)
                logger.say("reason", f"{plan['action']}({json.dumps(plan['params'])})")
                logger.say("", plan["reasoning"])

            result = act(plan, build_tools(state))
            if logger:
                logger.step(iteration=iterations, step="act", tool=result["tool"],
                            summary_in=json.dumps(plan["params"]),
                            summary_out=json.dumps(result["data"])[:200],
                            ms=result["ms"], error=result["error"])
                logger.say("act", f"{'ok' if result['ok'] else 'FAILED'} in {result['ms']}ms")
                if not result["ok"]:
                    logger.say("", f"{result['error']['class']}: {result['error']['message']}")

            reflection = reflect(result, observation, llm=llm)
        except Exception as exc:  # noqa: BLE001
            if harness.is_quota_exhausted(exc):
                status = harness.BUDGET_EXCEEDED
                if logger:
                    # Each gpt-oss model on Groq has its own separate daily
                    # bucket. If a different one isn't already exhausted too,
                    # --model names it as a way to keep working today; if it
                    # is, this is just naming what's already true.
                    other = ("openai/gpt-oss-120b" if cfg.llm.model == "openai/gpt-oss-20b"
                            else "openai/gpt-oss-20b")
                    logger.warn(
                        f"Groq daily token quota is spent for {cfg.llm.model}. "
                        f"Wait for the daily reset (UTC midnight), check remaining "
                        f"quota at console.groq.com/settings/billing, or try "
                        f"--model {other} if that one hasn't also been used up today."
                    )
            else:
                status = harness.FAILED
                if logger:
                    logger.warn(f"unrecoverable in round {iterations}: "
                                f"{type(exc).__name__}: {exc}")
            iterations -= 1        # this round never completed; don't count it
            break

        if logger:
            logger.step(iteration=iterations, step="reflect",
                        summary_in=reflection["outcome"],
                        summary_out=f"done={reflection['is_done']} "
                                    f"q={reflection['quality_score']:.2f}",
                        fingerprint=reflection["fingerprint"])
            logger.say("reflect", reflection["outcome"])
            logger.say("", f"next: {reflection['next_instruction']}")

        # ---- MEMORY WRITE, after reflect (Milestone 2) -------------------
        try:
            new_claims = (result["data"].get("claims_added", [])
                          if result["ok"] and result["tool"] == "extract_claims" else [])
            memory_manager.remember_round(
                memory,
                claims=[state.claims[c["id"]] for c in new_claims],
                finding=reflection["finding"],
                rejected=state.rejected[-1] if state.rejected else None,
            )
        except Exception as exc:  # noqa: BLE001
            degraded = True
            if logger:
                logger.warn(f"memory write failed: {exc}")

        trace.append({"iteration": iterations, "action": plan["action"],
                      "params": plan["params"], "reasoning": plan["reasoning"],
                      "ok": result["ok"], "outcome": reflection["outcome"],
                      "next_instruction": reflection["next_instruction"]})

        stuck = rails.after_round(reflection)
        if stuck:
            status = stuck
            break

        if reflection["is_done"]:
            status = harness.COMPLETE
            break

        # THE FEEDBACK EDGE. Serialised, because perceive takes a string. Without
        # this line every round re-perceives round 1 and the loop is four function
        # calls in a row — which is exactly what test_feedback_reaches_next_round
        # exists to catch.
        payload = json.dumps({"run_id": observation["run_id"],
                              "iteration": iterations + 1,
                              "instruction": reflection["next_instruction"]})

    assert state is not None

    # Last line of defence: a finding may not quote text the document doesn't have.
    kept, fabricated = harness.verify_quotes(state.findings, state.raw)
    if fabricated and logger:
        logger.warn(f"dropped {len(fabricated)} finding(s) whose quotes are not in the source")

    return {
        "status": status,
        "iterations": iterations,
        "doc": state.path,
        "coverage": state.coverage(),
        "claims": list(state.claims.values()),
        "findings": kept,
        "dropped_findings": fabricated,
        "pairs_compared": len(state.compared),
        "degraded": degraded,
        "trace": trace,
        "usage": llm.usage() if hasattr(llm, "usage") else {},
    }
