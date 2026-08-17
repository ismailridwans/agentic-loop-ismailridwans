"""The only module that talks to the LLM.

Everything goes through here so tests can replace it (test/fake_llm.py) and so
there is exactly one place for retries and the JSON repair ladder.

Provider is Groq via its OpenAI-compatible endpoint. The model choice follows from
the architecture: on Groq only openai/gpt-oss-120b and openai/gpt-oss-20b support
*strict* structured outputs, where constrained decoding makes a schema violation
impossible. The loop depends on `reason` returning a parseable plan every round.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from agent import harness


class LLMError(RuntimeError):
    """Any failure talking to the model."""


class LLMParseError(LLMError):
    """Every rung of the repair ladder failed. The caller falls back."""


class LLMProtocol(Protocol):
    def complete_json(self, *, system: str, user: str, schema: dict, name: str,
                      fast: bool = False) -> dict[str, Any]: ...


class GroqLLM:
    def __init__(self, cfg: Any = None, *, api_key: str | None = None,
                 logger: Any = None, faults: harness.Faults | None = None) -> None:
        from openai import OpenAI

        if cfg is None:
            from agent import config
            cfg = config.load()

        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise LLMError(
                "GROQ_API_KEY is not set. Copy .env.example to .env and add your key."
            )

        self.cfg = cfg
        self.model = cfg.llm.model
        self.fast_model = cfg.llm.fast_model
        self.logger = logger
        self.faults = faults or harness.Faults()

        self._retry = harness.RetryConfig(**dict(cfg.retry))
        self._client = OpenAI(api_key=key, base_url=cfg.llm.base_url,
                              timeout=cfg.llm.timeout_s)

        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    # -- one raw call, wrapped in retry ------------------------------------
    def _once(self, model: str, system: str, user: str, schema: dict,
              name: str, strict: bool) -> str:
        # gpt-oss models accept reasoning_effort; other models 400 on it. Only
        # send it when it's configured, so switching provider stays a config
        # change rather than a code change.
        extra: dict[str, Any] = {}
        effort = self.cfg.llm.get("reasoning_effort")
        if effort:
            extra["reasoning_effort"] = effort

        def send() -> str:
            self.faults.llm_call()
            response = self._client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=self.cfg.llm.temperature,
                max_completion_tokens=self.cfg.llm.max_completion_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": name, "strict": strict, "schema": schema},
                },
                **extra,
            )
            self.calls += 1
            if response.usage:
                self.tokens_in += response.usage.prompt_tokens or 0
                self.tokens_out += response.usage.completion_tokens or 0
            return response.choices[0].message.content or ""

        raw = harness.call_with_retry(send, self._retry, label=name, logger=self.logger)
        return self.faults.llm_response(raw)

    # -- the ladder ---------------------------------------------------------
    def complete_json(self, *, system: str, user: str, schema: dict, name: str,
                      fast: bool = False) -> dict[str, Any]:
        """Rungs 1-4. Rung 5 (a hardcoded plan) lives in the caller, because only
        the caller knows what a sensible default action is."""
        model = self.fast_model if fast else self.model
        strict = bool(self.cfg.llm.strict_schema)

        raw = self._once(model, system, user, schema, name, strict)

        # 1. straight parse   2. dig it out of prose
        parsed = harness.extract_json(raw)
        if parsed is not None:
            return parsed

        # 3. show the model its own bad output and the error
        if self.logger:
            self.logger.warn(f"{name}: unparseable reply, trying repair prompt")
        raw2 = self._once(model, system,
                          harness.repair_prompt(user, raw, "not a JSON object"),
                          schema, name, strict)
        parsed = harness.extract_json(raw2)
        if parsed is not None:
            return parsed

        # 4. strip the prompt back and ask for the smallest possible answer
        if self.logger:
            self.logger.warn(f"{name}: repair failed, trying simplified prompt")
        raw3 = self._once(model, system, harness.simplified_prompt(user),
                          schema, name, strict)
        parsed = harness.extract_json(raw3)
        if parsed is not None:
            return parsed

        raise LLMParseError(f"{name}: no usable JSON after 3 attempts: {raw3[:200]!r}")

    def usage(self) -> dict[str, int]:
        return {"calls": self.calls, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "tokens_total": self.tokens_in + self.tokens_out}
