"""The three tools: their JSON Schema definitions and their handlers.

The challenge asks for at least two. Three is the natural number here: read
statements out of the text, find statements that might disagree, decide whether two
of them actually do.

Note the split inside compare_claims. Numbers are normalised and compared in Python;
the model is only ever asked the language question — are these two about the same
thing? — and is handed the arithmetic as a fact. Arithmetic can rule a contradiction
OUT with certainty. It can never rule one IN, because equal-looking numbers about
different subjects are not a conflict.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agent import prompts
from agent.schemas import COMPARISON_SCHEMA, EXTRACTION_SCHEMA

# --------------------------------------------------------------------------- #
# Unit normalisation — plain Python
# --------------------------------------------------------------------------- #

# alias -> (family, base unit, multiplier). Month and year are approximations,
# which is fine for deciding "30 days vs 90 days".
_UNITS = {
    "second": ("duration", "second", 1), "seconds": ("duration", "second", 1),
    "minute": ("duration", "second", 60), "minutes": ("duration", "second", 60),
    "hour": ("duration", "second", 3600), "hours": ("duration", "second", 3600),
    "day": ("duration", "second", 86400), "days": ("duration", "second", 86400),
    "week": ("duration", "second", 604800), "weeks": ("duration", "second", 604800),
    "month": ("duration", "second", 2592000), "months": ("duration", "second", 2592000),
    "year": ("duration", "second", 31536000), "years": ("duration", "second", 31536000),
    "b": ("size", "byte", 1), "byte": ("size", "byte", 1), "bytes": ("size", "byte", 1),
    "kb": ("size", "byte", 1024), "mb": ("size", "byte", 1024**2),
    "gb": ("size", "byte", 1024**3), "tb": ("size", "byte", 1024**4),
}

_QUANTITY = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([a-zA-Z]+|%)")


def normalize(raw: str | None) -> dict | None:
    """"30 days" -> a comparable number. None if there is no quantity.

    A recognised unit is required — "AES-256" and "TLS 1.3" contain digits but are
    not measurements, and comparing them would produce nonsense.
    """
    if not raw:
        return None

    for m in _QUANTITY.finditer(raw):
        unit = m.group(2).lower()
        amount = float(m.group(1).replace(",", ""))
        if unit == "%":
            return {"raw": raw, "value": amount / 100, "unit": "fraction", "family": "ratio"}
        if unit in _UNITS:
            family, base, mult = _UNITS[unit]
            return {"raw": raw, "value": amount * mult, "unit": base, "family": family}

    return None


def compare_values(a: dict | None, b: dict | None) -> tuple[str | None, bool]:
    """Returns (a line for the prompt, whether the values are equal)."""
    if not a or not b or a["family"] != b["family"]:
        return None, False

    if abs(a["value"] - b["value"]) < 1e-9:
        return (f"'{a['raw']}' and '{b['raw']}' are the SAME quantity "
                f"({a['value']:g} {a['unit']}). These do not conflict."), True

    return (f"'{a['raw']}' = {a['value']:g} {a['unit']}, "
            f"'{b['raw']}' = {b['value']:g} {b['unit']}. Different quantities."), False


# --------------------------------------------------------------------------- #
# Run state
# --------------------------------------------------------------------------- #


@dataclass
class State:
    """Everything one run accumulates.

    Looked up rather than passed around, because perceive() takes only a string.
    """

    path: str
    raw: str
    chunks: list[dict]
    llm: Any = None
    faults: Any = None          # M3 fault injection, None in normal runs
    scanned: set[str] = field(default_factory=set)
    claims: dict[str, dict] = field(default_factory=dict)
    compared: set[tuple[str, str]] = field(default_factory=set)
    findings: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    def chunk(self, cid: str) -> dict | None:
        return next((c for c in self.chunks if c["id"] == cid), None)

    def content_chunks(self) -> list[dict]:
        """Headings carry no claims."""
        return [c for c in self.chunks if c["kind"] == "text"]

    def unscanned(self) -> list[dict]:
        return [c for c in self.content_chunks() if c["id"] not in self.scanned]

    def in_section(self, section: str) -> list[dict]:
        """'2' matches '2.1' as well."""
        return [c for c in self.content_chunks()
                if c["section"] == section or c["section"].startswith(section + ".")]

    def coverage(self) -> dict:
        total = len(self.content_chunks())
        return {"scanned": total - len(self.unscanned()), "total": total}

    def next_id(self, prefix: str, store) -> str:
        return f"{prefix}_{len(store) + 1:03d}"

    @staticmethod
    def pair(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)


# --------------------------------------------------------------------------- #
# Candidate pairing — deterministic. This is what stops the agent sending all
# ~20,000 possible pairs to the model.
# --------------------------------------------------------------------------- #

_STOPWORDS = {"the", "a", "an", "of", "for", "to", "in", "and", "or", "is", "are"}


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _STOPWORDS}


def relatedness(a: dict, b: dict) -> float:
    """0.0 to 1.0 — how likely these two are worth comparing."""
    ta, tb = _tokens(a["subject"]), _tokens(b["subject"])
    overlap = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0

    score = 2.0 * overlap
    if a["type"] == b["type"]:
        score += 1.0
    if a["value"] and b["value"] and a["value"]["family"] == b["value"]["family"]:
        score += 1.0
    return min(score / 4.0, 1.0)


def candidate_pairs(state: State, threshold: float = 0.3) -> list[dict]:
    """Un-compared pairs worth a look, best first.

    0.3 excludes pairs matching on type alone (which scores 0.25) — two NUMBERS
    claims about unrelated things are not candidates.
    """
    claims = list(state.claims.values())
    out = []
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            if state.pair(a["id"], b["id"]) in state.compared:
                continue
            score = relatedness(a, b)
            if score >= threshold:
                out.append({"a": a["id"], "b": b["id"], "score": round(score, 2)})
    return sorted(out, key=lambda p: -p["score"])


# --------------------------------------------------------------------------- #
# Tool definitions (JSON Schema)
# --------------------------------------------------------------------------- #

TOOL_SCHEMAS = [
    {
        "name": "extract_claims",
        "description": (
            "Read one or more chunks and pull out the checkable statements in them. "
            "Use while chunks are unread. Do not use to compare statements — use "
            "compare_claims for that. Do not re-run on a scanned chunk."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Chunk IDs from the observation, e.g. ['c012','c013'].",
                }
            },
            "required": ["chunk_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_related_claims",
        "description": (
            "Given one claim, find other claims about the same subject that could "
            "plausibly disagree with it. Use when a claim looks important but has no "
            "obvious partner. Returns claim IDs, not a verdict."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "claim_id": {"type": "string", "description": "The claim to find partners for."},
                "k": {"type": "integer", "minimum": 1, "maximum": 10,
                      "description": "How many to return. Defaults to 5."},
            },
            "required": ["claim_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "compare_claims",
        "description": (
            "Decide whether two claims contradict each other. The only tool that "
            "produces a finding. Numeric values are normalised and compared in Python "
            "first. Do not run on a pair that has already been compared."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "claim_a": {"type": "string", "description": "First claim ID."},
                "claim_b": {"type": "string", "description": "Second claim ID."},
            },
            "required": ["claim_a", "claim_b"],
            "additionalProperties": False,
        },
    },
]


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


class ToolError(RuntimeError):
    """A tool couldn't do its job. Becomes ok=False, never an escaping exception."""


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """Find a quote in a chunk. Returns its real span, or None.

    The quote is a locator, not content. Matching is on the sequence of words with
    any punctuation or whitespace allowed between them, so a sentence copied back as
    one line still matches a hard-wrapped source and a trailing "." still matches a
    source ",". Being loose costs nothing: the claim's text is sliced out of the
    source at the returned span, never taken from the model.

    What it still refuses: words that aren't there, or aren't in that order. That is
    what makes a fabricated sentence impossible to turn into a claim.
    """
    words = re.findall(r"\w+", quote)
    if not words:
        return None
    pattern = r"\b" + r"\W+".join(re.escape(w) for w in words) + r"\b"
    m = re.search(pattern, text, flags=re.IGNORECASE)
    return m.span() if m else None


def _extract_claims(state: State, params: dict) -> dict:
    chunks = [c for c in (state.chunk(cid) for cid in params["chunk_ids"]) if c]
    if not chunks:
        raise ToolError(f"no such chunks: {params['chunk_ids']}")

    reply = state.llm.complete_json(
        system=prompts.EXTRACT_SYSTEM,
        user=prompts.extract_user(chunks),
        schema=EXTRACTION_SCHEMA,
        name="extraction",
        fast=True,
    )

    added, dropped = [], 0
    for item in reply.get("claims", []):
        quote = (item.get("quote") or "").strip()

        found = None
        for c in chunks:
            span = locate(quote, c["text"])
            if span:
                found = (c, span)
                break

        if not found:
            dropped += 1          # not in the text — this is where invented claims die
            continue

        chunk, (lo, hi) = found
        claim = {
            "id": state.next_id("clm", state.claims),
            "section": chunk["section"],
            "text": chunk["text"][lo:hi],
            "type": item["type"],
            "subject": item["subject"],
            "value": normalize(item.get("value_raw")),
            "position": [chunk["start"] + lo, chunk["start"] + hi],
        }
        state.claims[claim["id"]] = claim
        added.append({k: claim[k] for k in ("id", "section", "type", "subject", "text")})

    for c in chunks:
        state.scanned.add(c["id"])

    return {
        "chunks_scanned": [c["id"] for c in chunks],
        "claims_added": added,
        "claims_dropped_unverifiable": dropped,
    }


def _find_related_claims(state: State, params: dict) -> dict:
    target = state.claims.get(params["claim_id"])
    if not target:
        raise ToolError(f"no such claim: {params['claim_id']}")

    related = [
        {"id": o["id"], "section": o["section"], "subject": o["subject"],
         "text": o["text"], "score": round(relatedness(target, o), 2),
         "already_compared": state.pair(target["id"], o["id"]) in state.compared}
        for o in state.claims.values() if o["id"] != target["id"]
    ]
    related = sorted((r for r in related if r["score"] > 0), key=lambda r: -r["score"])
    return {"claim_id": target["id"], "related": related[: params.get("k", 5)]}


def _compare_claims(state: State, params: dict) -> dict:
    a = state.claims.get(params["claim_a"])
    b = state.claims.get(params["claim_b"])
    if not a or not b:
        missing = [p for p in (params["claim_a"], params["claim_b"]) if p not in state.claims]
        raise ToolError(f"no such claim(s): {missing}")

    # The tool description tells the model not to repeat a pair, but that's
    # only a prompt hint - nothing enforced it. reflect once gave the same
    # instruction two rounds running and the second call quietly recorded
    # the same contradiction a second time, so the report showed 3 findings
    # for 2 real ones. Refusing here means a repeat costs a cheap ok:False
    # instead of a wasted LLM call and a duplicate finding.
    pair_key = state.pair(a["id"], b["id"])
    if pair_key in state.compared:
        raise ToolError(f"already compared: {pair_key[0]}/{pair_key[1]}")

    arithmetic, values_equal = compare_values(a["value"], b["value"])

    verdict = state.llm.complete_json(
        system=prompts.COMPARE_SYSTEM,
        user=prompts.compare_user(a, b, arithmetic),
        schema=COMPARISON_SCHEMA,
        name="comparison",
    )

    is_contradiction = verdict["verdict"] == "contradiction" and verdict["same_subject"]
    override = None

    # The one place Python overrules the model. Equal quantities cannot contradict,
    # whatever the model decided. Note this only ever rejects.
    if values_equal and is_contradiction:
        is_contradiction = False
        override = "same_value: equal once units are normalised"

    state.compared.add(state.pair(a["id"], b["id"]))

    # Cleared pairs are remembered too. Three reasons: the same pair is never sent
    # to the model twice, the same false positive can't come back on a later run,
    # and repeating an action becomes provably useless, which helps the loop finish.
    if not is_contradiction:
        state.rejected.append({
            "claims": [a["id"], b["id"]],
            "guard": override.split(":")[0] if override else verdict["guard"],
            "why": override or verdict["reasoning"],
        })

    return {
        "claim_a": a["id"], "claim_b": b["id"],
        "sections": [a["section"], b["section"]],
        "quotes": [a["text"], b["text"]],
        "is_contradiction": is_contradiction,
        "type": verdict["type"] if is_contradiction else "NONE",
        "same_subject": verdict["same_subject"],
        "guard": verdict["guard"],
        "confidence": verdict["confidence"],
        "reasoning": verdict["reasoning"],
        "arithmetic": arithmetic,
        "python_override": override,
    }


_HANDLERS: dict[str, Callable[[State, dict], dict]] = {
    "extract_claims": _extract_claims,
    "find_related_claims": _find_related_claims,
    "compare_claims": _compare_claims,
}


def build_tools(state: State) -> dict[str, dict]:
    """The registry act() dispatches through: {name: {"schema", "handler"}}.

    Handlers close over the run's state, which is how act(plan, tools) keeps the
    two-argument signature the challenge specifies.
    """

    def wrap(name: str, fn: Callable[[State, dict], dict]):
        def handler(params: dict) -> dict:
            t0 = time.perf_counter()
            try:
                if state.faults:
                    state.faults.tool_call(name)
                data, ok, error = fn(state, params), True, None
            except Exception as exc:  # a failing tool is data, not a crash
                data, ok = {}, False
                error = {"class": type(exc).__name__, "message": str(exc)}
            return {"ok": ok, "tool": name, "data": data, "error": error,
                    "ms": int((time.perf_counter() - t0) * 1000)}

        return handler

    schemas = {s["name"]: s for s in TOOL_SCHEMAS}
    return {name: {"schema": schemas[name], "handler": wrap(name, fn)}
            for name, fn in _HANDLERS.items()}
