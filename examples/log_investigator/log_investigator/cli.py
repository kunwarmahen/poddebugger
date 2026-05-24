"""CLI entrypoint — ``log-investigator <log-file>``."""

from __future__ import annotations

import argparse
import json
import os
import sys

from inquiro import LLMClient

from .collector import collect
from .engine import MiniEngine


def _make_llm() -> LLMClient:
    """Pick an LLM client.

    For this example we don't ship our own LLM clients — we just import the
    PodDebugger ones if they're on the path (since the user already has API
    keys configured for them). Real apps would build their own client list.
    """
    provider = os.environ.get("LOG_INVESTIGATOR_LLM_PROVIDER", "anthropic")
    model = os.environ.get("LOG_INVESTIGATOR_LLM_MODEL", "")
    base_url = os.environ.get("LOG_INVESTIGATOR_LLM_BASE_URL", "")
    try:
        from poddebugger.llm import get_llm
    except ImportError as exc:
        raise SystemExit(
            "log_investigator needs an LLMClient. The simplest path is to "
            "install poddebugger alongside it (`pip install -e ../../agent`) "
            "and use its providers. Or wire your own."
        ) from exc
    return get_llm(provider, model, base_url)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="log-investigator")
    parser.add_argument("logfile", help="path to a log file")
    parser.add_argument("--tail", type=int, default=500,
                        help="how many trailing lines to read (default: 500)")
    parser.add_argument("--json", action="store_true",
                        help="emit the finding as JSON")
    args = parser.parse_args(argv)

    ctx = collect(args.logfile, tail_lines=args.tail)
    if not ctx.lines:
        print(f"log file {args.logfile!r} is empty", file=sys.stderr)
        return 1

    engine = MiniEngine(_make_llm())
    finding = engine.investigate(ctx)

    if args.json:
        print(json.dumps(finding.__dict__, indent=2))
    else:
        print("=" * 60)
        print("  log_investigator — Finding")
        print("=" * 60)
        print(f"Summary:        {finding.summary}")
        print(f"Classification: {finding.classification}")
        print(f"Confidence:     {finding.confidence:.0%}")
        print(f"Likely cause:   {finding.likely_cause}")
        if finding.evidence:
            print("Evidence:")
            for e in finding.evidence:
                print(f"  - {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
