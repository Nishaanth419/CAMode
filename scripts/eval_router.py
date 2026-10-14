"""Score request routing on evals/request_routing.jsonl.

Keyword rules are always scored (offline, free). With ``--provider`` the AI
classifier is scored too, which calls the real provider once per case:

    set -a; source .env; set +a
    python -m scripts.eval_router --provider openai --min-accuracy 0.9

Exit status is 1 when the classifier scores below ``--min-accuracy``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from app import agent, router

DATASET = Path(__file__).resolve().parent.parent / "evals" / "request_routing.jsonl"


def load_cases() -> list[dict[str, Any]]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def report(name: str, cases: list[dict[str, Any]], predictions: list[str]) -> float:
    correct = sum(case["expected"] == predicted for case, predicted in zip(cases, predictions))
    accuracy = correct / len(cases)
    print(f"\n{name}: {correct}/{len(cases)} correct ({accuracy:.1%})")
    per_label = Counter(case["expected"] for case in cases)
    hits = Counter(case["expected"] for case, predicted in zip(cases, predictions) if case["expected"] == predicted)
    for label in router.CATEGORIES:
        print(f"  {label:16} {hits[label]}/{per_label[label]}")
    for case, predicted in zip(cases, predictions):
        if case["expected"] != predicted:
            print(f"  miss: {case['message'][:60]!r} expected {case['expected']}, got {predicted}")
    return accuracy


async def classify_one(case: dict[str, Any], config: dict[str, str], provider: str) -> tuple[str, float]:
    """Classify one case, retrying rate limits (this script sends requests faster than people do)."""
    for backoff_seconds in (5, 10, 20, 40, None):
        started = time.perf_counter()
        try:
            category = await router.classify(case["message"], case["history"], config, provider=provider)
            return category, (time.perf_counter() - started) * 1000
        except router.RouterError as exc:
            if "HTTP 429" not in str(exc) or backoff_seconds is None:
                return f"error: {exc}", (time.perf_counter() - started) * 1000
            await asyncio.sleep(backoff_seconds)
    raise AssertionError("unreachable")


async def classify_all(cases: list[dict[str, Any]], provider: str, delay_seconds: float) -> list[str]:
    config = agent.provider_config(provider)
    predictions, latencies = [], []
    for case in cases:
        category, latency_ms = await classify_one(case, config, provider)
        predictions.append(category)
        latencies.append(latency_ms)
        await asyncio.sleep(delay_seconds)
    latencies.sort()
    print(f"\nlatency p50 {statistics.median(latencies):.0f} ms, p95 {latencies[int(0.95 * (len(latencies) - 1))]:.0f} ms")
    return predictions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=sorted(agent.PROVIDERS), help="also score the AI classifier")
    parser.add_argument("--min-accuracy", type=float, default=0.9)
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between classifier calls")
    args = parser.parse_args()

    cases = load_cases()
    report("Keyword rules", cases, [agent._keyword_category(case["message"], case["history"]) for case in cases])
    if not args.provider:
        return 0
    config = agent.provider_config(args.provider)
    accuracy = report(
        f"AI classifier ({args.provider}, {config['router_model']}, {router.ROUTER_PROMPT_VERSION})",
        cases, asyncio.run(classify_all(cases, args.provider, args.delay)),
    )
    return 0 if accuracy >= args.min_accuracy else 1


if __name__ == "__main__":
    sys.exit(main())
