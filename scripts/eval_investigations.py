"""Score Portfolio Analyzer's portfolio-change investigations on evals/portfolio_change.jsonl.

Each case builds its snapshots with tests/fixtures/snapshot_pairs.py in a throwaway
database, injects its fault (another account scope, a slow snapshot read), checks
that the investigation itself returns the expected status and flagged checks, then
asks the real agent (``agent.run_agent`` with ``investigations.investigation_tool``,
as ``POST /api/chat`` does) and grades the answer in code, never by how it reads:

- tool selection: ``investigate_portfolio_change`` was called where the case needs it
  (an ambiguous question may instead ask a clarifying question);
- evidence: the expected figures and names appear;
- arithmetic: every amount and percentage matches a number the tools returned;
- unsupported claims: no external cause ("due to RBI news") that the result does not
  contain, and no "mainly market gains" when the market-effect estimate is a minority
  of the change;
- warnings: a flagged check (scope, reconciliation, staleness, timeout) is mentioned;
- insufficient-data honesty: a missing comparison is never reported as "no change".

Live INDmoney tools are offered but answer with an error, so the eval is offline
apart from the AI provider. Nothing touches .data/ or the real master key.

    set -a; source .env; set +a
    python -m scripts.eval_investigations --provider ollama --delay 0
    python -m scripts.eval_investigations          # only check the fixtures (no model calls)

Exit status is 1 when fewer than ``--min-pass`` cases pass in a run, 2 when a fixture
does not produce the investigation result its case expects.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile

if __name__ == "__main__":
    # Before importing app: app.db reads APP_DATA_DIR at import, and .env may point it at real data.
    _SCRATCH = tempfile.mkdtemp(prefix="ca-mode-eval-")
    atexit.register(shutil.rmtree, _SCRATCH, True)
    os.environ["APP_DATA_DIR"] = _SCRATCH
    os.environ["BACKUP_DIR"] = os.path.join(_SCRATCH, "backups")
    os.environ["SNAPSHOT_SCHEDULER_ENABLED"] = "false"
    os.environ.pop("APP_MASTER_KEY_FILE", None)
    from cryptography.fernet import Fernet

    os.environ["APP_MASTER_KEY"] = Fernet.generate_key().decode()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import dataclasses  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Final  # noqa: E402

from app import agent, db, investigations, number_check, privacy, snapshots, unlock, users  # noqa: E402
from app.sealing import ProfileKeys  # noqa: E402
from scripts.eval_numbers import mentions  # noqa: E402
from tests.fixtures.indmoney_sample import holdings  # noqa: E402
from tests.fixtures.snapshot_pairs import baseline_rows, build_results, later_rows  # noqa: E402

EVAL_VERSION: Final = "portfolio_change.v1"
DATASET: Final = Path(__file__).resolve().parent.parent / "evals" / "portfolio_change.jsonl"
TOOL: Final = "investigate_portfolio_change"
ROWS: Final = {"baseline": baseline_rows, "later": later_rows}
PIN: Final = "2468"
TIMEOUT_STEP_SECONDS: Final = 0.5
SLOW_READ_SECONDS: Final = 2.0

LIVE_TOOLS: Final = [
    {"name": "networth_snapshot", "description": "Live INDmoney net worth summary.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "networth_holdings", "description": "Live INDmoney holdings of one asset type.",
     "inputSchema": {"type": "object", "properties": {"asset_type": {"type": "string"}}, "required": ["asset_type"]}},
]
LIVE_UNAVAILABLE: Final = json.dumps({"error": "Live INDmoney data is not available right now; use the saved snapshots."})

METRICS: Final = (
    ("tool", "Tool selection"),
    ("evidence", "Evidence completeness"),
    ("arithmetic", "Arithmetic"),
    ("claims", "No unsupported claims"),
    ("warning", "Warning mentioned"),
    ("honesty", "Insufficient-data honesty"),
    ("instructions", "Case instructions"),
)

# ── Grading ────────────────────────────────────────────────────────────────────

_SENTENCE_SPLIT: Final = re.compile(r"(?<=[.!?])\s+|\n+")
_NEGATION: Final = re.compile(
    r"\b(?:not|no|never|can't|cannot|couldn't|isn't|aren't|wasn't|doesn't|don't|didn't|without|unable|unclear|unknown)\b"
    r"|n't\b", re.IGNORECASE)
_CAUSAL: Final = re.compile(
    r"\b(?:because of|due to|driven by|caused by|as a result of|on the back of|thanks to|triggered by|owing to|"
    r"fuell?ed by|in response to|in reaction to|attributed to|result of|reflects?|reflecting)\b", re.IGNORECASE)
# Causes the investigation never checks; naming one as the reason is an unsupported claim.
_EXTERNAL_CAUSE: Final = re.compile(
    r"\b(?:news|rbi|repo rate|interest rates?|rate (?:cut|hike|decision)s?|monetary policy|inflation|the fed|"
    r"federal reserve|earnings|quarterly results|budget|elections?|war|geopolitic\w*|sentiment|rally|sell-?off|"
    r"crash|recession|fiis?|diis?|foreign (?:institutional )?investors|global cues|crude|oil prices?|tariffs?|"
    r"monsoon|sector rotation|macro\w*)\b", re.IGNORECASE)
_MARKET_MAJORITY: Final = (
    re.compile(r"\b(?:mainly|mostly|largely|primarily|predominantly|chiefly|majority of|bulk of|most of)\b"
               r"\W+(?:[\w-]+\W+){0,3}?(?:market|price)", re.IGNORECASE),
    re.compile(r"\b(?:market|price)[\w-]*\W+(?:[\w-]+\W+){0,3}?(?:main|primary|biggest|largest|major|key) "
               r"(?:driver|reason|cause|factor|contributor|source)", re.IGNORECASE),
)
_NO_CHANGE: Final = re.compile(
    r"\b(?:no change|not changed|hasn't changed|haven't changed|has not changed|have not changed|did not change|"
    r"didn't change|unchanged|stayed (?:the )?same|remained (?:the )?same|remained flat|stayed flat)\b", re.IGNORECASE)
# "No change" only counts when it is said about the whole portfolio, not one holding that really did not move.
_NO_CHANGE_SUBJECT: Final = re.compile(r"\b(?:portfolio|net ?worth|overall|total|your money|investments)\b", re.IGNORECASE)
_NO_CHANGE_EXEMPT: Final = re.compile(
    r"not the same as|isn't the same as|doesn't mean|does not mean|not mean|can't (?:tell|say|confirm|know|report)|"
    r"cannot (?:tell|say|confirm|know|report)|unable to|no way to|no change (?:can|could) be", re.IGNORECASE)
_HONEST_GAP: Final = re.compile(
    r"not enough|enough (?:saved )?(?:history|data|snapshots)|insufficient|only one|"
    r"no (?:earlier|previous|prior|older|saved) (?:snapshot|data|history)|nothing (?:earlier )?to compare|"
    r"no (?:saved )?history|(?:don't|do not|doesn't|does not) have (?:an? |any )?(?:earlier|previous|prior|older|saved)|"
    r"(?:don't|do not) have (?:an? |any )?(?:\w+ ){0,2}snapshot|(?:isn't|is not|aren't|are not) (?:an? |any )?"
    r"(?:\w+ ){0,2}snapshot|no snapshot|earliest|timed out|time-?out|couldn't|could not|unable|failed|try again",
    re.IGNORECASE)
_SPACES: Final = re.compile(r"[\u00a0\u2007\u2009\u202f]")
_AGENT_WARNING: Final = "\n\n> ⚠️ I couldn't match these figures"


def _plain(answer: str) -> str:
    """The answer as the phrase checks read it: straight apostrophes, plain spaces, no Markdown emphasis."""
    return _SPACES.sub(" ", answer).replace("\u2019", "'").replace("\u2018", "'").replace("*", "")


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]


def investigation_payload(tool_log: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last ``investigate_portfolio_change`` result the model saw, parsed."""
    for call in reversed(tool_log):
        if call["name"] == TOOL:
            try:
                return json.loads(call["result"])
            except (TypeError, ValueError):
                return None
    return None


def market_effect_share(payload: dict[str, Any] | None) -> float | None:
    """The market-effect estimate as a share of the change in total current value (None if unknown)."""
    findings = (payload or {}).get("findings") or []
    estimate = next((item.get("valueInr") for item in findings if item.get("kind") == "estimate"), None)
    total = next((item.get("changeInr") for item in findings
                  if str(item.get("claim", "")).startswith("Total current value")), None)
    if estimate is None or not total:
        return None
    return estimate / total


def unsupported_claims(answer: str, payload: dict[str, Any] | None) -> list[str]:
    """Sentences that assert a cause the investigation result does not contain."""
    supported = " ".join([str((payload or {}).get("summary") or "")]
                         + [str(item.get("claim", "")) for item in (payload or {}).get("findings") or []]).lower()
    share = market_effect_share(payload)
    flagged = []
    for sentence in _sentences(answer):
        if _NEGATION.search(sentence):
            continue
        external = [m.group(0) for m in _EXTERNAL_CAUSE.finditer(sentence) if m.group(0).lower() not in supported]
        if external and _CAUSAL.search(sentence):
            flagged.append(sentence)
        elif share is not None and share < 0.5 and any(pattern.search(sentence) for pattern in _MARKET_MAJORITY):
            flagged.append(sentence)
    return flagged


def says_no_change(answer: str) -> list[str]:
    return [s for s in _sentences(answer)
            if _NO_CHANGE.search(s) and _NO_CHANGE_SUBJECT.search(s) and not _NO_CHANGE_EXEMPT.search(s)]


def grade(case: dict[str, Any], answer: str, tool_log: list[dict[str, Any]], *, error: str | None = None) -> dict[str, Any]:
    """Grade one answer. Each metric is True/False, or None when it does not apply to the case."""
    answer = answer.split(_AGENT_WARNING, 1)[0]  # The agent's own number warning; graded below instead.
    text = _plain(answer)
    called = any(call["name"] == TOOL for call in tool_log)
    payload = investigation_payload(tool_log)
    problems: list[str] = []
    scores: dict[str, bool | None] = {key: None for key, _ in METRICS}
    if error:
        problems.append(f"error: {error[:160]}")

    if case["tool"] == "required":
        scores["tool"] = called
    elif case.get("clarify_ok"):
        scores["tool"] = called or "?" in text
    if scores["tool"] is False:
        problems.append("did not call the tool" + (" or ask a clarifying question" if case.get("clarify_ok") else ""))

    if evidence := case.get("evidence"):
        missing = [str(v) for v in evidence.get("figures", []) if not mentions(answer, v)]
        missing += [term for term in evidence.get("terms", []) if term.lower() not in text.lower()]
        scores["evidence"] = not missing and not error
        if missing:
            problems.append("missing evidence " + ", ".join(missing))

    unverified = number_check.unverified_numbers(answer, [*(call["result"] for call in tool_log), case["question"]])
    scores["arithmetic"] = not unverified and not error
    if unverified:
        problems.append("untraceable figures " + ", ".join(unverified[:6]))

    claims = unsupported_claims(text, payload)
    scores["claims"] = not claims and not error
    problems += [f"unsupported claim: {sentence[:120]!r}" for sentence in claims]

    if pattern := case.get("warning"):
        scores["warning"] = bool(re.search(pattern, text, re.IGNORECASE))
        if not scores["warning"]:
            problems.append("warning not mentioned")

    if case.get("honesty"):
        no_change = says_no_change(text)
        scores["honesty"] = not no_change and bool(_HONEST_GAP.search(text))
        problems += [f"reported no change: {sentence[:120]!r}" for sentence in no_change]
        if not _HONEST_GAP.search(text):
            problems.append("did not say the data is insufficient")

    if "must_mention" in case or "forbid" in case:
        ok = not error
        if (pattern := case.get("must_mention")) and not re.search(pattern, text, re.IGNORECASE):
            ok = False
            problems.append("missing required statement")
        for pattern in case.get("forbid", []):
            if found := re.search(pattern, text, re.IGNORECASE):
                ok = False
                problems.append(f"forbidden {found.group(0)!r}")
        scores["instructions"] = ok

    passed = not error and all(value is not False for value in scores.values())
    return {"passed": passed, "scores": scores, "problems": problems, "called": called}


# ── Fixtures ───────────────────────────────────────────────────────────────────

def load_cases() -> list[dict[str, Any]]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def _results(spec: dict[str, Any]) -> dict[str, str]:
    rows = ROWS[spec["rows"]]()
    results = build_results(rows)
    if drop := spec.get("drop_holding"):
        asset_type, code = drop
        kept = [row for row in rows[asset_type] if row["investment_code"] != code]
        results[f"networth_holdings:{asset_type}"] = json.dumps(holdings(kept))  # The summary still counts it.
    return results


def new_profile(name: str) -> dict[str, Any]:
    """A profile that saves history, accepted the notice and is unlocked, like ``main.current_user`` returns."""
    user = users.create_user(name, "eval-profile-no-sign-in")
    unlocked = unlock.create_keys(user["id"], PIN)
    privacy.record_consent(user, privacy.ConsentRequest(policyVersion=privacy.POLICY_VERSION, storageMode="device"))
    user = users.get_user(user["id"]) or {}
    user["keys"] = ProfileKeys(public_key=unlocked.private_key.public_key, private_key=unlocked.private_key)
    return user


def store_case_snapshots(user_id: str, case: dict[str, Any], now: datetime) -> dict[str, str]:
    """Save the case's snapshots; returns the account scope to inject per snapshot date."""
    scopes = {}
    for spec in case["snapshots"]:
        stored = snapshots.store_snapshot(user_id, now - timedelta(days=spec["days_ago"]), _results(spec), {})
        if scope := spec.get("account_scope"):
            scopes[stored["asOfDate"]] = scope
    return scopes


@contextlib.contextmanager
def faults(case: dict[str, Any], scopes: dict[str, str]) -> Iterator[None]:
    """Inject the case's fault into the investigation workflow, restoring it afterwards."""
    normalize, step_timeout, previous = (investigations.normalize_snapshot, investigations.STEP_TIMEOUT_SECONDS,
                                         snapshots.previous_snapshot)
    if scopes:
        # The app has a single account scope today, so another scope can only come from here.
        def other_scope(stored: dict[str, Any]) -> Any:
            snapshot = normalize(stored)
            scope = scopes.get(snapshot.as_of_date)
            return dataclasses.replace(snapshot, account_scope=scope) if scope else snapshot

        investigations.normalize_snapshot = other_scope
    if case.get("fault") == "timeout":
        def slow_previous(*args: Any, **kwargs: Any) -> Any:
            time.sleep(SLOW_READ_SECONDS)
            return previous(*args, **kwargs)

        investigations.STEP_TIMEOUT_SECONDS = TIMEOUT_STEP_SECONDS
        snapshots.previous_snapshot = slow_previous
    try:
        yield
    finally:
        investigations.normalize_snapshot = normalize
        investigations.STEP_TIMEOUT_SECONDS = step_timeout
        snapshots.previous_snapshot = previous


def fixture_problems(case: dict[str, Any], record: dict[str, Any]) -> list[str]:
    """Whether the investigation itself produced what the case expects (a harness check, not a model score)."""
    expect = case["expect"]
    problems = []
    if record["status"] != expect["status"]:
        problems.append(f"status {record['status']}, expected {expect['status']}")
    if "flagged" in expect:
        checks = (record.get("result") or {}).get("checks") or []
        flagged = sorted(check["check"] for check in checks if check["status"] in ("warning", "failed"))
        if flagged != sorted(expect["flagged"]):
            problems.append(f"flagged {flagged}, expected {sorted(expect['flagged'])}")
    return problems


async def prepare(case: dict[str, Any], index: int) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    user = new_profile(f"Eval {index} {case['id']}")
    scopes = store_case_snapshots(user["id"], case, datetime.now(timezone.utc))
    with faults(case, scopes):
        record = await investigations.investigate_portfolio_change(user, baseline_date=case.get("baseline_date"))
    return user, scopes, fixture_problems(case, record)


# ── Running the agent ──────────────────────────────────────────────────────────

def _recorded(tool: agent.LocalTool, log: list[dict[str, Any]]) -> agent.LocalTool:
    async def handler(arguments: dict[str, Any]) -> str:
        result = await tool.handler(arguments)
        log.append({"name": tool.name, "arguments": arguments, "result": result})
        return result

    return agent.LocalTool(tool.name, tool.description, tool.input_schema, handler)


@contextlib.contextmanager
def offline_live_tools(log: list[dict[str, Any]]) -> Iterator[None]:
    """Offer INDmoney's live tools but answer every call with an error instead of reaching INDmoney."""
    fetch, call = agent.fetch_mcp_session, agent.call_mcp_tool

    async def fake_session(url: str, token: str) -> dict[str, Any]:
        return {"tools": LIVE_TOOLS, "protocolVersion": "eval", "sessionId": None}

    async def fake_call(url: str, token: str, protocol: str, session_id: str | None, name: str,
                        arguments: dict[str, Any], *, client: Any = None) -> str:
        log.append({"name": name, "arguments": arguments, "result": LIVE_UNAVAILABLE})
        return LIVE_UNAVAILABLE

    agent.fetch_mcp_session, agent.call_mcp_tool = fake_session, fake_call
    try:
        yield
    finally:
        agent.fetch_mcp_session, agent.call_mcp_tool = fetch, call


async def run_case(case: dict[str, Any], user: dict[str, Any], scopes: dict[str, str], provider: str) -> dict[str, Any]:
    tool_log: list[dict[str, Any]] = []
    context = snapshots.portfolio_context(user, extra_tools=[investigations.investigation_tool(user, provider=provider)])
    context = dataclasses.replace(context, local_tools=tuple(_recorded(tool, tool_log) for tool in context.local_tools))
    answer, error, trace = "", None, []
    started = time.perf_counter()
    with faults(case, scopes), offline_live_tools(tool_log):
        try:
            result = await agent.run_agent(case["question"], "https://mcp.invalid/eval", "eval-token", [], "portfolio",
                                           provider, context=context)
            answer, trace = str(result), result.trace
        except agent.AgentRunError as exc:
            error, trace = str(exc), exc.trace
    latency = time.perf_counter() - started
    graded = grade(case, answer, tool_log, error=error)
    return {**graded, "id": case["id"], "answer": answer, "error": error, "latency": latency,
            "toolCalls": len(tool_log), "modelCalls": sum(1 for step in trace if step.get("kind") == "model"),
            "tools": [call["name"] for call in tool_log], "toolLog": tool_log}


def regrade(path: str) -> list[dict[str, Any]]:
    """Grade the answers saved by ``--out`` again (after changing the grader), without calling the model."""
    cases = {case["id"]: case for case in load_cases()}
    results = []
    for line in Path(path).read_text().splitlines():
        saved = json.loads(line)
        graded = grade(cases[saved["id"]], saved["answer"], saved["toolLog"], error=saved.get("error"))
        results.append({**saved, **graded})
        print(f"{'PASS' if graded['passed'] else 'FAIL'}    {saved['id']:24} {'; '.join(graded['problems']) or 'ok'}")
    return results


def report(results: list[dict[str, Any]], runs: int) -> None:
    print(f"\n{EVAL_VERSION}, {runs} run(s) × {len(results) // runs} cases")
    for key, label in METRICS:
        applicable = [r["scores"][key] for r in results if r["scores"][key] is not None]
        if applicable:
            print(f"  {label:27} {sum(applicable)}/{len(applicable)} ({sum(applicable) / len(applicable):.0%})")
    flagged = sum(1 for r in results if r["scores"]["claims"] is False)
    print(f"  {'Unsupported-claim rate':27} {flagged}/{len(results)} answers ({flagged / len(results):.0%})")
    passed = sum(r["passed"] for r in results)
    print(f"  {'Cases passed':27} {passed}/{len(results)} ({passed / len(results):.0%})")
    latencies = sorted(r["latency"] for r in results)
    print(f"  latency p50 {statistics.median(latencies):.1f} s, max {latencies[-1]:.1f} s; "
          f"tool calls {statistics.mean(r['toolCalls'] for r in results):.1f}, "
          f"model calls {statistics.mean(r['modelCalls'] for r in results):.1f} per case")


async def main_async(args: argparse.Namespace) -> int:
    cases = [case for case in load_cases() if not args.case or case["id"] in args.case]
    users.SCRYPT_N = 2**10  # Throwaway profiles; production cost would add seconds per case.
    db.initialize_db()
    results: list[dict[str, Any]] = []
    harness_failed = False
    for run in range(1, args.runs + 1):
        passed = 0
        for index, case in enumerate(cases):
            user, scopes, problems = await prepare(case, run * 100 + index)
            if problems:
                harness_failed = True
                print(f"FIXTURE {case['id']:24} {'; '.join(problems)}")
                continue
            if not args.provider:
                print(f"ok      {case['id']:24} {case['expect']['status']} {case['expect'].get('flagged', '')}")
                continue
            result = await run_case(case, user, scopes, args.provider)
            results.append(result)
            passed += result["passed"]
            print(f"{'PASS' if result['passed'] else 'FAIL'}    {case['id']:24} {result['latency']:5.1f} s  "
                  f"{','.join(result['tools']) or 'no tools':42}  {'; '.join(result['problems']) or 'ok'}", flush=True)
            if args.verbose:
                print("        " + result["answer"].replace("\n", "\n        ") + "\n")
            await asyncio.sleep(args.delay)
        if args.provider:
            print(f"run {run}: {passed}/{len(cases)} passed")
    if harness_failed:
        return 2
    if not args.provider:
        print(f"\nAll {len(cases)} fixtures produce their expected investigation result.")
        return 0
    report(results, args.runs)
    if args.out:
        Path(args.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results))
    worst_run = min(sum(r["passed"] for r in results[i:i + len(cases)]) for i in range(0, len(results), len(cases)))
    return 0 if worst_run >= args.min_pass else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=sorted(agent.PROVIDERS), help="run the agent; omit to check fixtures only")
    parser.add_argument("--delay", type=float, default=8.0, help="seconds between cases (rate limits)")
    parser.add_argument("--runs", type=int, default=1, help="repeat the whole set (local models vary between runs)")
    parser.add_argument("--min-pass", type=int, default=6, help="cases each run must pass (Ollama baseline: 6-9)")
    parser.add_argument("--case", action="append", help="run only this case id (repeatable)")
    parser.add_argument("--out", help="write each graded answer to this JSONL file")
    parser.add_argument("--regrade", metavar="PATH", help="grade the answers in an --out file again (no model calls)")
    parser.add_argument("--verbose", action="store_true", help="print each answer")
    args = parser.parse_args()
    if Path(db.DATABASE_PATH).resolve().is_relative_to(Path(__file__).resolve().parent.parent / ".data"):
        parser.error("refusing to run against the real .data/ database")
    os.environ.setdefault("REQUEST_ROUTER", "keywords")  # Measure answers, not routing; set REQUEST_ROUTER=model to include it.
    logging.basicConfig(level=logging.ERROR)  # The injected timeout and unmatched figures log warnings by design.
    if args.regrade:
        results = regrade(args.regrade)
        report(results, len(results) // len({r["id"] for r in results}))
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
