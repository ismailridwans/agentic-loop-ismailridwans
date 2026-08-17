"""Minimum tests — one per thing that would actually break the submission.

    pytest              fast: no API key, no network, no cost
    pytest -m llm       adds one real run against Groq

Five for the loop, three for memory, five for the harness, two for config.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import config, harness, loop, memory_manager, segmenter, tools  # noqa: E402
from agent.loop import act, perceive, reason  # noqa: E402
from agent.logger import StepLogger  # noqa: E402
from agent.memory_manager import CLAIMS, DictMemory  # noqa: E402
from agent.tools import State, build_tools  # noqa: E402

POLICY = str(ROOT / "test" / "data" / "policy.md")
QUOTE = "All system logs are retained for 30 days."


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class FakeLLM:
    """Scripted replies keyed by call name, and a record of what it was asked.

    agent/llm.py is a separate module precisely so this can replace it.
    """

    def __init__(self, replies: dict[str, list[dict]]):
        self.replies, self.asked = replies, []

    def complete_json(self, *, system, user, schema, name, fast=False):
        self.asked.append((name, user))
        queue = self.replies.get(name)
        if not queue:
            raise AssertionError(f"no {name!r} replies left")
        return queue.pop(0)

    def usage(self):
        return {"calls": len(self.asked), "tokens_in": 0, "tokens_out": 0, "tokens_total": 0}

    def prompts(self, name):
        return [u for n, u in self.asked if n == name]


def plan(action, **fields):
    base = {"action": action, "chunk_ids": None, "claim_id": None,
            "claim_a": None, "claim_b": None, "reasoning": "because"}
    return {**base, **fields}


def extracted(*claims):
    return {"claims": [{"quote": q, "type": t, "subject": s, "value_raw": v}
                       for q, t, s, v in claims]}


def reflected(instruction="keep going", done=False):
    return {"is_done": done, "quality_score": 0.5, "next_instruction": instruction}


def cfg(**over):
    """Defaults only, so tests don't drift when config.yaml changes."""
    return config.load("no-such-file.yaml", **over)


@pytest.fixture(autouse=True)
def _reset():
    loop.reset()
    yield
    loop.reset()


@pytest.fixture
def raw():
    return Path(POLICY).read_text(encoding="utf-8")


@pytest.fixture
def state(raw):
    return State(path=POLICY, raw=raw, chunks=segmenter.segment(raw))


def reading_script(rounds):
    """Reads one chunk per round, never satisfied."""
    return FakeLLM({
        "plan": [plan("extract_claims", chunk_ids=[f"c{4 + i:03d}"]) for i in range(rounds)],
        "extraction": [extracted() for _ in range(rounds)],
        "reflection": [reflected(f"round {i + 2}") for i in range(rounds)],
    })


# =========================================================================== #
# Milestone 1 — the loop
# =========================================================================== #


def test_chunk_positions_are_exact(raw, state):
    """Claims are pinned to source offsets and the harness slices the document at
    them to prove a quote is real. If these drift, that check passes on garbage."""
    for c in state.chunks:
        assert raw[c["start"]:c["end"]] == c["text"]


def test_bad_params_are_rejected_before_the_tool_runs(state):
    """A string where an array belongs must fail at validation, not inside a tool."""
    state.llm = FakeLLM({})           # would raise if it were ever called
    result = act({"action": "extract_claims", "params": {"chunk_ids": "c001"}},
                 build_tools(state))
    assert result["ok"] is False and result["error"]["class"] == "InvalidParams"


def test_invented_quotes_never_become_claims(state):
    """Where fabrication dies. A sentence not in the chunk is dropped."""
    chunk = next(c for c in state.chunks if c["section"] == "2.1" and c["kind"] == "text")
    state.llm = FakeLLM({"extraction": [extracted(
        (QUOTE, "NUMBERS", "log retention", "30 days"),
        ("Logs use quantum cryptography.", "RULES", "log encryption", None))]})

    data = act({"action": "extract_claims", "params": {"chunk_ids": [chunk["id"]]}},
               build_tools(state))["data"]
    assert len(data["claims_added"]) == 1
    assert data["claims_dropped_unverifiable"] == 1


def test_a_pair_cannot_be_compared_twice(state):
    """Regression. reflect once gave the same instruction two rounds running,
    and nothing stopped the tool running it again — the same contradiction got
    recorded twice, so a report of 2 real findings showed 3."""
    c1 = next(c for c in state.chunks if c["section"] == "2.1" and c["kind"] == "text")
    c2 = next(c for c in state.chunks if c["section"] == "7.4" and c["kind"] == "text")
    state.llm = FakeLLM({"extraction": [
        extracted((QUOTE, "NUMBERS", "system log retention", "30 days")),
        extracted(("System logs must be kept for a minimum of 90 days",
                   "NUMBERS", "system log retention", "90 days")),
    ]})
    tools_ = build_tools(state)
    act({"action": "extract_claims", "params": {"chunk_ids": [c1["id"]]}}, tools_)
    act({"action": "extract_claims", "params": {"chunk_ids": [c2["id"]]}}, tools_)

    state.llm = FakeLLM({"comparison": [{
        "same_subject": True, "verdict": "contradiction", "type": "NUMBERS",
        "guard": "none", "confidence": 0.9, "reasoning": "30 vs 90",
    }]})
    first = act({"action": "compare_claims",
                "params": {"claim_a": "clm_001", "claim_b": "clm_002"}}, tools_)
    assert first["ok"] is True and first["data"]["is_contradiction"] is True

    second = act({"action": "compare_claims",
                 "params": {"claim_a": "clm_001", "claim_b": "clm_002"}}, tools_)
    assert second["ok"] is False and "already compared" in second["error"]["message"]


def test_python_does_the_arithmetic_not_the_model():
    """"30 days" and "720 hours" are the same quantity, so they cannot conflict —
    and the model gets no vote on that."""
    hint, equal = tools.compare_values(tools.normalize("30 days"),
                                       tools.normalize("720 hours"))
    assert equal and "SAME" in hint
    assert tools.compare_values(tools.normalize("30 days"),
                                tools.normalize("90 days"))[1] is False
    assert tools.normalize("AES-256") is None      # digits, but not a measurement


def test_reflect_feeds_the_next_round():
    """THE test for "is this a loop". Round N's instruction must appear in round
    N+1's prompt, which can only happen if reflect's output went back through
    perceive. Every other test here would pass on a straight-line pipeline."""
    llm = reading_script(3)
    loop.run_loop(POLICY, llm, cfg=cfg(loop__max_iterations=3))
    assert "round 2" in llm.prompts("plan")[1]
    assert "round 3" in llm.prompts("plan")[2]


# =========================================================================== #
# Milestone 2 — memory
# =========================================================================== #


def test_save_recall_clear():
    mem = DictMemory()
    assert mem.recall("anything", CLAIMS) == []       # round 1 hits this every run
    mem.save([{"id": "clm_001", "text": QUOTE, "section": "2.1",
               "subject": "system log retention"}], CLAIMS)
    assert mem.recall("system log retention", CLAIMS)[0]["id"] == "clm_001"
    mem.clear()
    assert mem.recall("system log retention", CLAIMS) == []


def test_recalled_memory_reaches_the_prompt():
    """Fetching memory and never showing it to the model would be a no-op that
    still passes the storage test above."""
    llm = FakeLLM({"plan": [plan("extract_claims", chunk_ids=["c004"])]})
    reason(perceive(POLICY), [f"2.1: {QUOTE}"], llm=llm)
    assert QUOTE in llm.prompts("plan")[0]


def test_loop_recalls_before_reason_and_saves_after_reflect(raw):
    """The order the challenge specifies."""
    order = []

    class Spy(DictMemory):
        def recall(self, q, ns, k=6):
            order.append("recall")
            return super().recall(q, ns, k)

        def save(self, records, ns):
            order.append("save")
            return super().save(records, ns)

    chunk = next(c["id"] for c in segmenter.segment(raw)
                 if c["section"] == "2.1" and c["kind"] == "text")
    llm = FakeLLM({"plan": [plan("extract_claims", chunk_ids=[chunk])],
                   "extraction": [extracted((QUOTE, "NUMBERS", "log retention", "30 days"))],
                   "reflection": [reflected()]})

    loop.run_loop(POLICY, llm, cfg=cfg(loop__max_iterations=1), memory=Spy())
    assert order[0] == "recall" and "save" in order


def test_broken_memory_degrades_instead_of_stopping():
    class Broken(memory_manager.NullMemory):
        def recall(self, q, ns, k=6):
            raise RuntimeError("store unreachable")

    llm = FakeLLM({"plan": [plan("extract_claims", chunk_ids=["c004"])],
                   "extraction": [extracted()], "reflection": [reflected()]})
    report = loop.run_loop(POLICY, llm, cfg=cfg(loop__max_iterations=1), memory=Broken())
    assert report["iterations"] == 1 and report["degraded"] is True


# =========================================================================== #
# Milestone 3 — the harness
# =========================================================================== #


class Boom(Exception):
    def __init__(self, status):
        super().__init__(str(status))
        self.status_code = status


def test_retries_a_429_but_never_a_400():
    """The whole point of per-error-class policy: a malformed request will be
    malformed the second time too, so retrying it only fails slower."""
    delays, attempts = [], {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Boom(429)
        return "ok"

    assert harness.call_with_retry(flaky, harness.RetryConfig(),
                                   sleep=delays.append) == "ok"
    assert attempts["n"] == 3 and len(delays) == 2      # backed off twice

    bad_request_tries = {"n": 0}

    def bad_request():
        bad_request_tries["n"] += 1
        raise Boom(400)

    with pytest.raises(Boom):
        harness.call_with_retry(bad_request, harness.RetryConfig(), sleep=delays.append)
    assert bad_request_tries["n"] == 1                    # tried once, gave up


def test_json_is_dug_out_of_prose():
    """The most common real failure: the model wraps its answer in commentary."""
    assert harness.extract_json('Sure! {"action": "x"} Hope that helps') == {"action": "x"}
    assert harness.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert harness.extract_json("no json here {oops") is None


def test_the_loop_survives_an_unusable_model():
    """The last rung of the ladder: the loop never dies from a bad parse.

    Strict mode means the live model won't exercise this — but the suite does, on
    every commit. A defence you can't trigger is one you can't trust.
    """

    class Unusable(FakeLLM):
        def complete_json(self, **kw):
            from agent.llm import LLMParseError
            raise LLMParseError("nothing usable")

    report = loop.run_loop(POLICY, Unusable({}), cfg=cfg(loop__max_iterations=2))
    assert "fallback" in report["trace"][0]["reasoning"]


def test_guardrails_stop_the_run_and_still_return_a_report():
    report = loop.run_loop(POLICY, reading_script(5), cfg=cfg(loop__max_iterations=2))
    assert report["status"] == "PARTIAL" and report["iterations"] == 2
    assert "findings" in report        # a stopped run is never an empty return

    rails = harness.Guardrails(max_tokens=1000)
    assert rails.before_round(1, 1200) == harness.BUDGET_EXCEEDED

    # Stuck means nothing moved — not merely that the wording repeated. While
    # reading, "keep reading" is the correct instruction several rounds running.
    same = {"is_done": False, "next_instruction": "same", "finding": None,
            "progress": {"scanned": 1}}
    moving = {**same, "progress": {"scanned": 9}}
    assert rails.after_round(same) is None
    assert rails.after_round(moving) is None             # progress, so not stuck
    assert rails.after_round(moving) == harness.STUCK    # nothing moved


def test_fabricated_quotes_are_dropped_from_the_report(raw):
    """Makes an invented quote structurally impossible to report."""
    real = "All system logs are retained for 30 days"
    kept, dropped = harness.verify_quotes(
        [{"id": "a", "quotes": [real, "System logs must be kept for a minimum of 90 days"]},
         {"id": "b", "quotes": [real, "Logs are deleted after 5 minutes"]}], raw)
    assert [f["id"] for f in kept] == ["a"]
    assert [f["id"] for f in dropped] == ["b"]


def test_every_step_is_logged_with_the_required_fields(tmp_path):
    path = tmp_path / "run.jsonl"
    log = StepLogger("run_x", jsonl_path=str(path), console=False)
    loop.run_loop(POLICY, reading_script(2), cfg=cfg(loop__max_iterations=2), logger=log)
    log.close()

    lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    assert {"perceive", "reason", "act", "reflect"} <= {ln["step"] for ln in lines}
    for field in ("ts", "iteration", "step", "in", "out", "ms", "error"):
        assert field in next(ln for ln in lines if ln["step"] == "act")


def test_config_precedence_and_isolation(tmp_path, monkeypatch):
    path = tmp_path / "c.yaml"
    path.write_text("loop:\n  max_iterations: 3\n", encoding="utf-8")

    assert config.load(path).loop.max_iterations == 3             # yaml beats default
    assert config.load(path).budget.max_tokens == 150_000         # other keys survive

    monkeypatch.setenv("AGENT_MAX_ITERATIONS", "7")
    assert config.load(path).loop.max_iterations == 7             # env beats yaml
    assert config.load(path, loop__max_iterations=2).loop.max_iterations == 2   # cli wins
    monkeypatch.delenv("AGENT_MAX_ITERATIONS")

    # A shallow copy of DEFAULTS once made every override permanent, process-wide.
    assert config.load("nope.yaml").loop.max_iterations == 10


# =========================================================================== #
# The real thing — off by default
# =========================================================================== #


@pytest.mark.llm
def test_real_run_against_groq():
    """pytest -m llm. Costs a few cents.

    Two assertions. The second is the one that matters: anyone can score on the
    first by flagging everything.
    """
    import os

    import yaml
    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("GROQ_API_KEY"):
        pytest.skip("GROQ_API_KEY not set")

    from agent.llm import GroqLLM

    conf = config.load("config.yaml", loop__max_iterations=16)
    store = memory_manager.configure(conf.memory.backend, conf.memory.persist_dir)
    store.clear()

    report = loop.run_loop(POLICY, GroqLLM(conf), cfg=conf, memory=store)
    expected = yaml.safe_load(
        (ROOT / "test" / "data" / "expected.yaml").read_text(encoding="utf-8"))
    found = {frozenset(f["sections"]) for f in report["findings"]}

    traps = [t["guard"] for t in expected["should_not_find"]
             if frozenset(t["sections"]) in found]
    assert not traps, f"false positives: {traps}"

    missed = [w["about"] for w in expected["should_find"]
              if frozenset(w["sections"]) not in found]
    assert not missed, f"missed: {missed}"
