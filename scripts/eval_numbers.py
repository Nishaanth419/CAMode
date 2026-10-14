"""Score Portfolio Analyzer's numeric accuracy on a synthetic portfolio.

Each case asks a question about tests/fixtures/indmoney_sample.py (made-up data with
the real INDmoney layout) through the real AI provider and checks that:

- every expected figure appears in the answer (any rounding or lakh/crore form), and
- the answer cites no amount or percentage that the data does not contain.

    set -a; source .env; set +a
    python -m scripts.eval_numbers --provider groq

Exit status is 1 when fewer than ``--min-pass`` of the cases pass.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from app import agent, number_check, snapshots  # noqa: E402
from fixtures import indmoney_sample  # noqa: E402

CASES: list[dict[str, Any]] = [
    {"question": "What is my net worth?", "expect": [1200000]},
    {"question": "What is my total profit or loss on my investments, in rupees and percent?", "expect": [140000, 16.87]},
    {"question": "List my holdings grouped by broker with the value at each broker.", "expect": [470000, 130000]},
    {"question": "How concentrated is my portfolio? What share is my largest holding and my top 5?", "expect": [30, 80]},
    {"question": "What is my allocation by asset type, in percent?", "expect": [40, 12, 8]},
    {"question": "How much are my US stocks worth in dollars?", "expect": [1200]},
    {"question": "What share of my whole portfolio is Infosys?", "expect": [14.4]},
    {"question": "What profit have I made on HDFC Bank?", "expect": [], "forbid": [r"\b100\s?%"]},
]


def mentions(answer: str, expected: float) -> bool:
    """Whether ``answer`` states ``expected`` in any rounding or unit (₹12 lakh for 1200000)."""
    target = Decimal(str(expected))
    for match in number_check._NUMBER_RE.finditer(answer):
        value = number_check._decimal(match.group("number"))
        if value is None:
            continue
        multiplier = number_check._UNITS.get((match.group("unit") or "").lower(), Decimal(1))
        slack = max(number_check._rounding_slack(match.group("number"), multiplier), abs(target) * Decimal("0.005"))
        if abs(value * multiplier - target) <= slack:
            return True
    return False


def fixture_context() -> agent.PortfolioContext:
    snapshot = {"asOfDate": "2026-10-14", "capturedAt": int(time.time() * 1000),
                "results": indmoney_sample.results(), "errors": {}}

    async def get_portfolio_snapshot(arguments: dict[str, Any]) -> str:
        payload = snapshots._snapshot_payload(snapshot, include_all_holdings=arguments.get("include_all_holdings") is True)
        return snapshots.json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    real = next(tool for tool in snapshots.snapshot_tools("unused", snapshots.ProfileKeys())
                if tool.name == "get_portfolio_snapshot")
    tool = agent.LocalTool(real.name, real.description, real.input_schema, get_portfolio_snapshot)
    return agent.PortfolioContext(local_tools=(tool,), cached_mcp_tools=[
        {"name": "networth_snapshot", "description": "Live net worth (not available in this evaluation)."},
    ])


async def run_case(case: dict[str, Any], provider: str) -> tuple[bool, str]:
    try:
        answer = str(await agent.run_agent(case["question"], "https://unused.invalid", "unused", [], "portfolio",
                                           provider, context=fixture_context()))
    except agent.AgentRunError as exc:
        return False, f"error: {str(exc)[:160]}"
    problems = [f"missing {value}" for value in case["expect"] if not mentions(answer, value)]
    if "couldn't match these figures" in answer:
        problems.append("untraceable figures: " + answer.split("data: ", 1)[1].split(". Treat", 1)[0])
    problems += [f"forbidden {pattern}" for pattern in case.get("forbid", []) if re.search(pattern, answer)]
    return not problems, "; ".join(problems) or "ok"


async def main_async(provider: str, delay_seconds: float) -> int:
    passed = 0
    for case in CASES:
        ok, detail = await run_case(case, provider)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {case['question'][:60]:60}  {detail}")
        await asyncio.sleep(delay_seconds)
    print(f"\n{passed}/{len(CASES)} passed")
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=sorted(agent.PROVIDERS), required=True)
    parser.add_argument("--delay", type=float, default=8.0, help="seconds between cases (rate limits)")
    parser.add_argument("--min-pass", type=int, default=len(CASES) - 1)
    args = parser.parse_args()
    os.environ.setdefault("REQUEST_ROUTER", "keywords")  # Measure answers, not routing; halves the calls.
    passed = asyncio.run(main_async(args.provider, args.delay))
    return 0 if passed >= args.min_pass else 1


if __name__ == "__main__":
    sys.exit(main())
