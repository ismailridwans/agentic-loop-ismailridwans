"""python main.py --doc test/data/policy.md

Milestone 3: the harness wraps the loop from the outside. The loop itself knows
nothing about retries or budgets, which is why it is still readable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent import config, harness, loop, memory_manager, segmenter
from agent.llm import GroqLLM, LLMError
from agent.logger import StepLogger

BAR = "─" * 70


def build_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Find contradictions inside a document.")
    ap.add_argument("--doc", default="test/data/policy.md")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--max-iterations", type=int, help="overrides config.yaml")
    ap.add_argument("--model", help="overrides config.yaml")
    ap.add_argument("--no-memory", action="store_true",
                    help="run with memory disabled, to show the difference")
    ap.add_argument("--clear-memory", action="store_true",
                    help="wipe the store first, so a demo is repeatable")
    ap.add_argument("--inject-failure", choices=harness.MODES,
                    help="force a failure so the harness can be seen handling it")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--json", dest="json_out", metavar="PATH")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = build_args(argv)
    load_dotenv()

    if not Path(args.doc).exists():
        print(f"No such document: {args.doc}", file=sys.stderr)
        return 2

    cfg = config.load(args.config,
                      loop__max_iterations=args.max_iterations,
                      llm__model=args.model,
                      memory__backend="none" if args.no_memory else None,
                      logging__console=False if args.quiet else None)

    raw = Path(args.doc).read_text(encoding="utf-8")
    run_id = "run_" + segmenter.doc_id(raw)

    logger = StepLogger(run_id, jsonl_path=cfg.logging.jsonl_path,
                        console=cfg.logging.console,
                        max_field_chars=cfg.logging.max_field_chars)
    faults = harness.Faults(args.inject_failure) if args.inject_failure else None

    try:
        memory = memory_manager.configure(cfg.memory.backend, cfg.memory.persist_dir)
    except Exception as exc:  # noqa: BLE001 — a missing store is not a reason to stop
        logger.warn(f"memory unavailable ({exc}); continuing without it")
        memory = memory_manager.NullMemory()

    if args.clear_memory:
        memory.clear()

    try:
        llm = GroqLLM(cfg, logger=logger, faults=faults)
    except LLMError as exc:
        print(exc, file=sys.stderr)
        return 2

    print(f"\nChecking {args.doc}")
    print(f"  model    {cfg.llm.model} / {cfg.llm.fast_model}")
    print(f"  memory   {cfg.memory.backend}")
    if faults:
        print(f"  faults   injecting {args.inject_failure}")
    if logger.path:
        print(f"  log      {logger.path}")

    try:
        report = loop.run_loop(args.doc, llm, cfg=cfg, memory=memory,
                               logger=logger, faults=faults)
    except Exception as exc:  # noqa: BLE001 — always return something
        logger.warn(f"unrecoverable: {type(exc).__name__}: {exc}")
        report = {"status": harness.FAILED, "iterations": 0, "doc": args.doc,
                  "coverage": {"scanned": 0, "total": 0}, "claims": [], "findings": [],
                  "dropped_findings": [], "pairs_compared": 0, "degraded": True,
                  "trace": [], "usage": {}, "error": str(exc)}
    finally:
        logger.close()

    print_report(report)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report written to {args.json_out}")

    return 0


def print_report(report: dict) -> None:
    cov, usage = report["coverage"], report.get("usage", {})
    print(f"\n{BAR}")
    line = (f"{report['status']}  ·  {report['iterations']} rounds  "
            f"·  {cov['scanned']}/{cov['total']} chunks  "
            f"·  {len(report['claims'])} claims  "
            f"·  {report['pairs_compared']} pairs compared")
    if report.get("degraded"):
        line += "  ·  DEGRADED"
    print(line)
    if usage:
        print(f"{usage['calls']} LLM calls  ·  "
              f"{usage['tokens_in']:,} in / {usage['tokens_out']:,} out")
    print(BAR)

    if report.get("dropped_findings"):
        print(f"\n{len(report['dropped_findings'])} finding(s) dropped: "
              f"quotes not found in the source.")

    findings = report["findings"]
    if not findings:
        print("\nNo contradictions found.")
        return

    print(f"\n{len(findings)} CONTRADICTION{'S' if len(findings) > 1 else ''}\n")
    for f in findings:
        print(f"  [{f['type']}]  §{f['sections'][0]} vs §{f['sections'][1]}"
              f"   (confidence {f['confidence']:.2f})")
        for quote in f["quotes"]:
            print(f'      "{quote}"')
        print(f"      -> {f['explanation']}\n")


if __name__ == "__main__":
    raise SystemExit(main())
