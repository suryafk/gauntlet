"""Run a small mock run graded by the Claude judge, then grade the same calls with the
heuristic judge and compare both against what each mock script was built to show.

  python scripts/compare_judges.py              # 12 calls: 1 per persona on prompt v1 and on v2
  python scripts/compare_judges.py --dry-run    # no API calls: heuristic stands in for Claude

Needs ANTHROPIC_API_KEY in .env (except with --dry-run). Uses a throwaway database.
Writes reports/judge_comparison.md.
"""

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import mock_data  # noqa: E402
from backend.bolna_adapter import MockBolnaAdapter  # noqa: E402
from backend.config import ROOT, Settings  # noqa: E402
from backend.events import EventBus  # noqa: E402
from backend.models import Store  # noqa: E402
from backend.orchestrator import Orchestrator  # noqa: E402
from backend.personas import load_personas  # noqa: E402
from backend.scorer import JUDGED, METRIC_LABELS, ClaudeJudge, HeuristicJudge, Scorer  # noqa: E402


async def run_mock(settings: Settings, judge, versions: list[str], per_persona: int, seed: int):
    store = Store(settings.database_url)
    personas = load_personas(settings.personas_dir)
    for p in personas.values():
        store.upsert_persona(p)
    orch = Orchestrator(store, MockBolnaAdapter(settings, seed=seed), settings, EventBus(), personas)
    scorer = Scorer(store, EventBus(), settings, personas, judge)
    orch.on_call_finished.append(scorer.score_call)

    runs = [orch.start_run(list(personas), per_persona, v) for v in versions]
    started = time.monotonic()
    while any(store.get_run(r["id"])["status"] != "completed" for r in runs):
        await asyncio.sleep(0.2)
    elapsed = time.monotonic() - started
    await orch.shutdown()
    calls = [c for r in runs for c in store.list_calls(r["id"])]
    claude_scores = {}
    for r in runs:
        for s in store.scores_for_run(r["id"]):
            claude_scores[(s["call_id"], s["metric"])] = s
    return calls, claude_scores, personas, elapsed


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({(100 * n / d):.0f}%)" if d else "–"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="use the heuristic for both sides; no API calls")
    parser.add_argument("--per-persona", type=int, default=1, help="calls per persona per prompt version")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    settings = Settings()
    settings.bolna_mode = "mock"
    settings.database_url = str(Path(tempfile.mkdtemp()) / "judge_compare.db")
    settings.mock_call_min_s, settings.mock_call_max_s = 0.2, 0.6
    settings.mock_dial_fail_rate = 0.0
    if not args.dry_run and not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is not set. Put it in .env (copy .env.example), or use --dry-run.")

    judge = HeuristicJudge() if args.dry_run else ClaudeJudge(settings)
    label = "heuristic (dry run)" if args.dry_run else f"Claude ({settings.judge_model})"
    print(f"Running mock calls graded by {label}…")
    calls, claude_scores, personas, elapsed = await run_mock(settings, judge, ["v1", "v2"], args.per_persona, args.seed)

    heuristic = HeuristicJudge()
    rows, disagreements = [], []
    agree = {m: [0, 0] for m in JUDGED}           # claude vs heuristic
    claude_right = {m: [0, 0] for m in JUDGED}    # claude vs designed
    heur_right = {m: [0, 0] for m in JUDGED}      # heuristic vs designed
    fallbacks = 0
    for call in calls:
        persona = personas[call["persona_id"]]
        outcome = mock_data.outcome_of(call["persona_id"], call["transcript"])
        designed_fail = mock_data.DESIGNED_FAILURES[call["persona_id"]] if outcome == "fail" else set()
        h = await heuristic.judge(call, persona)
        cells = []
        for m in JUDGED:
            c = claude_scores[(call["id"], m)]
            if c["reason"].startswith("[heuristic fallback]"):
                fallbacks += 1
            c_pass, h_pass, d_pass = bool(c["passed"]), h[m].passed, m not in designed_fail
            agree[m][0] += c_pass == h_pass
            agree[m][1] += 1
            if outcome:
                claude_right[m][0] += c_pass == d_pass
                claude_right[m][1] += 1
                heur_right[m][0] += h_pass == d_pass
                heur_right[m][1] += 1
            mark = lambda ok: "✓" if ok else "✕"  # noqa: E731
            cells.append(f"{mark(c_pass)} / {mark(h_pass)} / {mark(d_pass) if outcome else '?'}")
            if c_pass != h_pass or (outcome and c_pass != d_pass):
                disagreements.append((persona["name"], outcome or "?", m, c_pass, h_pass, d_pass,
                                      c["reason"], h[m].reason))
        rows.append(f"| {persona['name']} | {outcome or '?'} | " + " | ".join(cells) + " |")

    total = lambda d: (sum(v[0] for v in d.values()), sum(v[1] for v in d.values()))  # noqa: E731
    lines = [
        "# Judge comparison",
        "",
        f"{len(calls)} mock calls (prompt v1 and v2, {args.per_persona} per persona each), graded by {label}"
        f" and by the offline heuristic. Took {elapsed:.0f}s."
        + (f" {fallbacks} judged scores fell back to the heuristic because the Claude call failed."
           if fallbacks else ""),
        "",
        "\"Designed\" is what each canned mock script was written to show (pass script: every metric passes;"
        " fail script: the persona's flaw fails). It is the closest thing to a human label we have without a"
        " hand review.",
        "",
        "## Agreement",
        "",
        "| Metric | Claude vs heuristic | Claude vs designed | Heuristic vs designed |",
        "|---|---|---|---|",
    ]
    for m in JUDGED:
        lines.append(f"| {METRIC_LABELS[m]} | {pct(*agree[m])} | {pct(*claude_right[m])} | {pct(*heur_right[m])} |")
    lines.append(f"| **All judged** | **{pct(*total(agree))}** | **{pct(*total(claude_right))}**"
                 f" | **{pct(*total(heur_right))}** |")
    lines += ["", "## Per call (Claude / heuristic / designed)", "",
              "| Persona | Script | " + " | ".join(METRIC_LABELS[m] for m in JUDGED) + " |",
              "|---|---|" + "---|" * len(JUDGED), *rows, ""]
    lines += ["## Disagreements", ""]
    if not disagreements:
        lines.append("None.")
    for name, outcome, m, c, h, d, cr, hr in disagreements:
        yn = lambda ok: "pass" if ok else "fail"  # noqa: E731
        lines += [f"**{name}, {outcome} script, {METRIC_LABELS[m]}**: "
                  f"Claude {yn(c)}, heuristic {yn(h)}, designed {yn(d)}",
                  f"- Claude: {cr}", f"- Heuristic: {hr}", ""]

    report = "\n".join(lines) + "\n"
    out = ROOT / "reports" / ("judge_comparison_dry_run.md" if args.dry_run else "judge_comparison.md")
    out.parent.mkdir(exist_ok=True)
    out.write_text(report)
    print(report)
    print(f"Saved {out}")


if __name__ == "__main__":
    asyncio.run(main())
