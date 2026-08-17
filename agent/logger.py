"""Structured step logger.

One JSON object per line to logs/run-<id>.jsonl. Same events also go to the
console, so the two can't disagree about what happened.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StepLogger:
    def __init__(self, run_id: str, *, jsonl_path: str | None = None,
                 console: bool = True, max_field_chars: int = 200) -> None:
        self.run_id = run_id
        self.console = console
        self.max_field_chars = max_field_chars
        self.lines = 0

        self._fh = None
        self.path = None
        if jsonl_path:
            path = Path(jsonl_path.format(run_id=run_id))
            path.parent.mkdir(parents=True, exist_ok=True)
            self.path = path
            self._fh = path.open("a", encoding="utf-8")

    def _trim(self, value: Any) -> Any:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if len(text) <= self.max_field_chars:
            return text
        return text[:self.max_field_chars] + f"… (+{len(text) - self.max_field_chars} chars)"

    def step(self, *, iteration: int, step: str, summary_in: Any = "",
             summary_out: Any = "", ms: int = 0, error: dict | None = None,
             **extra: Any) -> None:
        self._write({
            "ts": _now(),
            "run_id": self.run_id,
            "iteration": iteration,
            "step": step,
            "in": self._trim(summary_in),
            "out": self._trim(summary_out),
            "ms": ms,
            "error": error,
            **extra,
        })

    def event(self, kind: str, **fields: Any) -> None:
        self._write({"ts": _now(), "run_id": self.run_id, "step": kind, **fields})

    def warn(self, message: str, **fields: Any) -> None:
        self._write({"ts": _now(), "run_id": self.run_id, "step": "warning",
                     "message": message, **fields})
        if self.console:
            print(f"  ! {message}", file=sys.stderr)

    def _write(self, record: dict) -> None:
        self.lines += 1
        if self._fh:
            self._fh.write(json.dumps(record, default=str) + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    # console output

    def round_header(self, iteration: int, coverage: dict, claims: int, findings: int) -> None:
        if self.console:
            print(f"\nROUND {iteration}  ·  {coverage['scanned']}/{coverage['total']} chunks"
                  f"  ·  {claims} claims  ·  {findings} findings")

    def say(self, label: str, text: str) -> None:
        if self.console:
            print(f"  {label:<9} {text}")


class Timer:
    """with Timer() as t: ...  then t.ms"""

    def __enter__(self) -> "Timer":
        self._t0 = time.perf_counter()
        self.ms = 0
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = int((time.perf_counter() - self._t0) * 1000)
