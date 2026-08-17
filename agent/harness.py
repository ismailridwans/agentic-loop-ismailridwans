"""Milestone 3 — retry, fallbacks, guardrails.

A working loop is not a reliable one. Everything here exists because of a specific
thing that goes wrong when you run this for real:

    429 rate limit          -> back off, honour retry-after
    timeout / 5xx           -> back off
    unparseable JSON        -> the repair ladder in `extract_json` + `repair_prompt`
    bad request / auth      -> fail fast; retrying can't help
    tool raises             -> ok=False observation, the loop adapts
    memory down             -> continue with no recall, log a warning
    runs forever            -> max_iterations
    burns tokens            -> budget
    spins on one step       -> stuck detection
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #

RETRYABLE = "retryable"
FATAL = "fatal"


def is_quota_exhausted(exc: Exception) -> bool:
    """True for a daily/quota limit rather than a short burst limit.

    Both arrive as a 429, but they need opposite handling. A per-minute limit
    clears in seconds, so backing off works. A per-DAY limit ("tokens per day
    (TPD)") clears in hours — backing off four times just turns an instant
    failure into a two-minute one and then fails anyway.
    """
    text = str(exc).lower()
    return "per day" in text or "tpd" in text or "quota" in text


def classify(exc: Exception) -> tuple[str, str]:
    """(policy, class name). Which errors are worth trying again, and which aren't.

    "Retry everything" is the wrong answer: a malformed request will be malformed
    the second time too, and a bad key will still be bad. Retrying those just turns
    a fast failure into a slow one.
    """
    name = type(exc).__name__
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )

    if status == 429 or "RateLimit" in name:
        if is_quota_exhausted(exc):
            return FATAL, "QuotaExhausted"
        return RETRYABLE, "RateLimitError"
    if status in (408, 500, 502, 503, 504) or "Timeout" in name or "Connection" in name:
        return RETRYABLE, name
    if status in (401, 403):
        return FATAL, "AuthError"
    if status == 400 or "BadRequest" in name or "InvalidRequest" in name:
        return FATAL, "InvalidRequest"
    return FATAL, name


def retry_after_seconds(exc: Exception) -> float | None:
    """Groq tells us how long to wait. Believe it over our own backoff maths."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    for key in ("retry-after", "Retry-After"):
        if key in headers:
            try:
                return float(headers[key])
            except (TypeError, ValueError):
                pass
    return None


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #


@dataclass
class RetryConfig:
    max_attempts: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    honour_retry_after: bool = True


def backoff_delay(attempt: int, cfg: RetryConfig, rng: random.Random | None = None) -> float:
    """Exponential, with full jitter.

    Doubling: hammering a busy service makes it busier. Jitter: if several calls
    fail at the same moment and all retry after exactly 2s, they collide again.
    """
    rng = rng or random
    ceiling = min(cfg.base_delay_s * (2 ** attempt), cfg.max_delay_s)
    return rng.uniform(0, ceiling)


def call_with_retry(fn: Callable[[], Any], cfg: RetryConfig, *, label: str = "",
                    logger: Any = None, sleep: Callable[[float], None] = time.sleep) -> Any:
    last: Exception | None = None

    for attempt in range(cfg.max_attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            policy, cls = classify(exc)
            last = exc

            if policy == FATAL:
                if logger:
                    logger.event("retry_abandoned", label=label, error_class=cls,
                                 reason="not retryable")
                raise

            if attempt == cfg.max_attempts - 1:
                break

            delay = backoff_delay(attempt, cfg)
            if cfg.honour_retry_after:
                hinted = retry_after_seconds(exc)
                if hinted is not None:
                    delay = min(hinted, cfg.max_delay_s)

            if logger:
                logger.event("retry", label=label, attempt=attempt + 1,
                             error_class=cls, delay_s=round(delay, 2))
            sleep(delay)

    assert last is not None
    raise last


# --------------------------------------------------------------------------- #
# The JSON repair ladder
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> dict | None:
    """Rung 2 — dig a JSON object out of prose. Costs nothing, catches the most
    common failure: the model wrapping its answer in commentary."""
    if not text:
        return None

    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass

    fenced = _FENCE.search(text)
    if fenced:
        try:
            value = json.loads(fenced.group(1))
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    # Brace matching, so a nested object doesn't truncate the scan early.
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : i + 1])
                        if isinstance(value, dict):
                            return value
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)

    return None


def repair_prompt(original_user: str, bad_output: str, error: str) -> str:
    """Rung 3 — show the model what it produced and what was wrong with it."""
    return (
        f"{original_user}\n\n"
        f"---\nYour previous reply could not be used.\n"
        f"You returned:\n{bad_output[:500]}\n\n"
        f"The problem: {error}\n\n"
        f"Reply again with a single JSON object and nothing else. No commentary, "
        f"no code fences."
    )


def simplified_prompt(original_user: str) -> str:
    """Rung 4 — strip it back and ask for the smallest possible answer."""
    head = original_user.split("\n\n")[0]
    return (
        f"{head}\n\n"
        f"Reply with one JSON object matching the schema. Keep every string short. "
        f"No explanation."
    )


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #

COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
STUCK = "STUCK"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
FAILED = "FAILED"


def fingerprint(reflection: dict) -> str:
    """Hash of the parts that indicate progress.

    `progress` matters as much as the wording. While the agent is reading through a
    document, "keep reading the next chunks" is the *correct* instruction several
    rounds running — hashing the words alone declared that stuck and killed a run
    that was working fine. Two rounds are only genuinely stuck when neither the plan
    nor the state moved.

    Timestamps are excluded: two rounds differing only in when they happened have
    not made progress.
    """
    payload = json.dumps(
        {"is_done": reflection.get("is_done"),
         "next": reflection.get("next_instruction"),
         "finding": (reflection.get("finding") or {}).get("id"),
         "progress": reflection.get("progress")},
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


@dataclass
class Guardrails:
    """Hard limits, checked once per round."""

    max_iterations: int = 10
    max_tokens: int = 150_000
    warn_at: int = 100_000
    logger: Any = None

    _prints: list[str] = field(default_factory=list)
    _warned: bool = False

    def before_round(self, iteration: int, tokens_used: int) -> str | None:
        """Returns a terminal status if the run must stop now."""
        if iteration > self.max_iterations:
            return PARTIAL

        if tokens_used >= self.max_tokens:
            if self.logger:
                self.logger.warn(f"token budget exhausted ({tokens_used:,})")
            return BUDGET_EXCEEDED

        if tokens_used >= self.warn_at and not self._warned:
            self._warned = True
            if self.logger:
                self.logger.warn(
                    f"token budget {tokens_used:,}/{self.max_tokens:,} — approaching the limit"
                )
        return None

    def after_round(self, reflection: dict) -> str | None:
        """Stuck detection: the same reflection twice running means nothing changed."""
        current = fingerprint(reflection)
        repeated = bool(self._prints) and self._prints[-1] == current
        self._prints.append(current)

        if repeated:
            if self.logger:
                self.logger.warn("reflection repeated — stopping as STUCK")
            return STUCK
        return None


def verify_quotes(findings: list[dict], raw: str) -> tuple[list[dict], list[dict]]:
    """Drop any finding quoting text that isn't in the document.

    Cheap, and it makes a fabricated quote structurally impossible to report rather
    than merely unlikely. Whitespace is normalised because the source is wrapped.
    """
    flat = " ".join(raw.split())
    kept, dropped = [], []
    for f in findings:
        if all(" ".join(q.split()) in flat for q in f["quotes"]):
            kept.append(f)
        else:
            dropped.append(f)
    return kept, dropped


# --------------------------------------------------------------------------- #
# Fault injection — so failure handling can be demonstrated, not just described
# --------------------------------------------------------------------------- #

MODES = ("rate_limit", "bad_json", "tool_error", "memory_down")


class InjectedRateLimit(Exception):
    status_code = 429


class Faults:
    """Forces a specific failure the first `times` opportunities."""

    def __init__(self, mode: str | None = None, times: int = 2) -> None:
        if mode and mode not in MODES:
            raise ValueError(f"unknown fault: {mode}. One of {MODES}.")
        self.mode = mode
        self.times = times
        self.fired = 0

    def _take(self, mode: str) -> bool:
        if self.mode != mode or self.fired >= self.times:
            return False
        self.fired += 1
        return True

    def llm_call(self) -> None:
        if self._take("rate_limit"):
            raise InjectedRateLimit("injected: rate limit exceeded")

    def llm_response(self, content: str) -> str:
        if self._take("bad_json"):
            return "Sure! Here is the plan you asked for — hope this helps. {oops"
        return content

    def tool_call(self, name: str) -> None:
        if name == "extract_claims" and self._take("tool_error"):
            raise RuntimeError("injected: tool backend unavailable")

    def memory_read(self) -> None:
        if self._take("memory_down"):
            raise RuntimeError("injected: memory backend unreachable")
