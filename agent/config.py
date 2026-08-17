"""Config loading. CLI flag > env var > config.yaml > the defaults below.

Defaults are here so it still runs if config.yaml is missing.
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "llm": {
        "provider": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-20b",
        "fast_model": "openai/gpt-oss-20b",
        "temperature": 0,
        "max_completion_tokens": 4096,
        "reasoning_effort": "low",
        "strict_schema": True,
        "timeout_s": 120,
    },
    "loop": {"max_iterations": 10, "focus_size": 6},
    "budget": {"max_tokens": 150_000, "warn_at": 100_000},
    "retry": {"max_attempts": 4, "base_delay_s": 1.0, "max_delay_s": 30.0,
              "honour_retry_after": True},
    "memory": {"backend": "chroma", "persist_dir": ".memory", "recall_k": 6},
    "logging": {"jsonl_path": "logs/run-{run_id}.jsonl", "console": True,
                "max_field_chars": 200},
}

# env var -> (section, key, cast)
ENV_MAP = {
    "GROQ_MODEL": ("llm", "model", str),
    "GROQ_FAST_MODEL": ("llm", "fast_model", str),
    "AGENT_MAX_ITERATIONS": ("loop", "max_iterations", int),
    "AGENT_MAX_TOKENS": ("budget", "max_tokens", int),
    "AGENT_MEMORY_BACKEND": ("memory", "backend", str),
}


class Section(dict):
    """dict you can also read with dots, so cfg.llm.model works."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _merge(base: dict, override: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out


def load(path: str | Path = "config.yaml", **cli: Any) -> Section:
    """CLI overrides come in as section__key=value."""
    # deepcopy, not dict(). A shallow copy shares the nested section dicts and
    # writing an override would mutate DEFAULTS for the rest of the process.
    data = deepcopy(DEFAULTS)

    p = Path(path)
    if p.exists():
        data = _merge(data, yaml.safe_load(p.read_text(encoding="utf-8")) or {})

    for env_name, (section, key, cast) in ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw:
            data[section][key] = cast(raw)

    for name, value in cli.items():
        if value is None:
            continue
        section, _, key = name.partition("__")
        if key and section in data:
            data[section][key] = value

    return Section({k: Section(v) if isinstance(v, dict) else v for k, v in data.items()})
