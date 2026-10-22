"""Check that the target's v1 flaws really fire in sim mode, and that the patch fixes them.

For each persona, runs sim conversations (Gemini plays both sides) against v1 and against
v1 with every offline patch rule applied, then scores them: extraction in code, the rest by
the Gemini judge. Prints which metrics fail per persona and version, and saves transcripts
to reports/flaw_check.md.

  python scripts/check_flaws.py                           # all personas, 1 call each per version
  python scripts/check_flaws.py --personas corrector injector --repeats 2

About 12 Gemini requests per call (10-11 conversation + 1 judge). Needs GEMINI_API_KEY.
"""

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import mock_data  # noqa: E402
from backend.config import ROOT, Settings  # noqa: E402
from backend.patcher import PATCHABLE, TemplatePatcher  # noqa: E402
from backend.personas import load_personas  # noqa: E402
from backend.scorer import METRIC_LABELS, GeminiJudge, check_extraction  # noqa: E402
from backend.sim_adapter import SimAdapter  # noqa: E402

CHECKED = ["task_completion", "extraction_accuracy", "interruption_recovery", "policy_adherence"]


async def one_call(adapter, judge, persona, version, prompt, sem):
    async with sem:
        started = time.monotonic()
        try:
            turns, payload = await adapter.converse(persona, prompt)
        except Exception as exc:  # noqa: BLE001
            return {"persona": persona, "version": version, "error": f"{type(exc).__name__}: {exc}"}
        call = {"transcript": [{"speaker": w, "text": t} for w, t in turns], "tool_payload": payload}
        verdicts = {"extraction_accuracy": check_extraction(call, persona)}
        try:
            verdicts.update(await judge.judge(call, persona))
        except Exception as exc:  # noqa: BLE001
            return {"persona": persona, "version": version, "turns": turns, "payload": payload,
                    "error": f"judge failed: {type(exc).__name__}: {exc}"}
        print(f"  {persona['name']} on {version}: done in {time.monotonic() - started:.0f}s", flush=True)
        return {"persona": persona, "version": version, "turns": turns, "payload": payload,
                "failed": {m for m in CHECKED if not verdicts[m].passed},
                "reasons": {m: verdicts[m].reason for m in CHECKED}}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--personas", nargs="*")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--judge-model", help="defaults to GEMINI_MODEL")
    args = parser.parse_args()

    settings = Settings()
    if not settings.gemini_api_key:
        sys.exit("GEMINI_API_KEY is not set in .env")
    if args.judge_model:
        settings.gemini_model = args.judge_model
    settings.target_prompts_dir = Path(tempfile.mkdtemp())
    v1 = (ROOT / "agents" / "target_prompts" / "v1.md").read_text()
    patched = (await TemplatePatcher().draft(v1, [{"metric": m} for m in PATCHABLE]))["revised_prompt"]

    personas = load_personas(settings.personas_dir)
    chosen = [personas[p] for p in (args.personas or personas)]
    adapter, judge = SimAdapter(settings), GeminiJudge(settings)
    sem = asyncio.Semaphore(args.concurrency)
    jobs = [one_call(adapter, judge, p, v, prompt, sem)
            for p in chosen for v, prompt in (("v1", v1), ("patched", patched)) for _ in range(args.repeats)]
    print(f"Running {len(jobs)} sim calls (conversation: {settings.gemini_sim_model}, "
          f"judge: {settings.gemini_model})…", flush=True)
    results = await asyncio.gather(*jobs)

    label = lambda ms: ", ".join(METRIC_LABELS[m] for m in sorted(ms)) or "none"  # noqa: E731
    lines = ["# Flaw check", "", f"Conversation model `{settings.gemini_sim_model}`, judge `{settings.gemini_model}`, "
             f"{args.repeats} call(s) per persona per version.", "",
             "| Persona | Built to break | v1 fails | Patched fails | Flaw fired on v1 | Fixed by patch |",
             "|---|---|---|---|---|---|"]
    for p in chosen:
        designed = mock_data.DESIGNED_FAILURES[p["id"]] & set(CHECKED)
        runs = {v: [r for r in results if r["persona"] is p and r["version"] == v] for v in ("v1", "patched")}
        ok = {v: [r for r in rs if "failed" in r] for v, rs in runs.items()}
        fired = sum(bool(r["failed"] & designed) for r in ok["v1"])
        fixed = sum(not (r["failed"] & designed) for r in ok["patched"])
        fails = {v: [label(r["failed"]) for r in ok[v]] + ["error"] * (len(runs[v]) - len(ok[v])) for v in runs}
        lines.append(f"| {p['name']} | {label(designed)} | {'; '.join(fails['v1'])} | {'; '.join(fails['patched'])}"
                     f" | {fired}/{len(ok['v1'])} | {fixed}/{len(ok['patched'])} |")
    lines += ["", "## Transcripts", ""]
    for r in results:
        lines.append(f"### {r['persona']['name']} on {r['version']}")
        if "error" in r:
            lines.append(f"Error: {r['error']}")
        for who, text in r.get("turns", []):
            lines.append(f"- **{who}:** {text}")
        if "turns" in r:
            lines.append(f"- *tool:* `{r.get('payload')}`")
        for m, reason in r.get("reasons", {}).items():
            lines.append(f"- {METRIC_LABELS[m]}: {'FAIL' if m in r['failed'] else 'pass'}: {reason}")
        lines.append("")
    report = "\n".join(lines) + "\n"
    out = ROOT / "reports" / "flaw_check.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(report)
    print("\n".join(lines[:len(chosen) + 6]))
    print(f"\nSaved {out}")


if __name__ == "__main__":
    asyncio.run(main())
