# Gauntlet demo (mock mode, about 2 minutes)

The demo runs in **mock mode**: call transcripts are scripted, so it costs nothing and plays out the same way every take. Say so up front. The Launch screen says it too. Never present the before/after numbers as a measured result; they show the workflow.

## Setup (once)

In `.env`:

```
BOLNA_MODE=mock
JUDGE_MODE=heuristic
MOCK_SEED=672
MAX_CONCURRENCY=10
MOCK_DIAL_FAIL_RATE=0.05
```

- `JUDGE_MODE=heuristic` keeps everything offline. Without it, the app tries Gemini, hits the free quota and labels scores "[heuristic fallback]".
- `MOCK_SEED=672` makes every take identical. It was chosen so that v1 fails clearly, the patched run mostly passes, and exactly one call fails to dial, so the retry can be shown.
- `MAX_CONCURRENCY=10` makes the 30-call run finish in about 10 seconds instead of a minute.

## Before every take

Stop the server (Ctrl-C), then:

```bash
.venv/bin/python scripts/demo_reset.py --yes
```

```bash
.venv/bin/uvicorn backend.main:app --port 8000
```

Open http://localhost:8000 in a full-screen browser window at about 1280×800, zoomed to 110–125%. Hide bookmarks and other tabs. The dashboard opens on the **Overview**: what Gauntlet does, numbers from the stored runs, how a run works, and recent runs.

A narrated 2-minute video of this exact flow is in `demo-video/` (see its README); render it as a backup.

## What you'll see with seed 672

| Step | Result |
|---|---|
| v1 run, 30 calls | **20%** pass (6/30). Mid-sentence corrector 0/5; one corrector call fails to dial |
| Retry the failed call | It connects and still fails: the flaw is real, not the line |
| Generate patch | 13 failure clusters; the rules rewrite all 7 flawed v1 instructions |
| Patched re-run (v2) | **90%** pass (27/30) |
| Compare | 20% → 90%, **+70 pts**, every persona up |

## Run of show

**0:00–0:15 · The problem** (on the dashboard showing the example run)
> "Voice agents ship after a few clean English test calls. Then real callers code-switch, correct themselves and try to talk the agent into things. Gauntlet is CI/CD for voice agents: it attacks your agent with realistic callers, scores every call, drafts a fix, and re-runs to check it."

**0:15–0:30 · Flagship scenario**
On the Overview, point at "How a run works", then click **Run the flagship scenario** (6 personas × 5 = 30 calls against prompt v1).
> "Calls are simulated here so we don't burn credits on stage. Everything after the call, the scoring, patching and re-run, is the real pipeline."

(To pick personas and call counts yourself, use **Launch**.)

**0:30–0:55 · Live heatmap**
The heatmap fills as calls finish.
> "Thirty callers in parallel. The strip on top shows the pipeline: dial, converse, score, patch, re-run. Rows are personas, columns are what we score: task done, details captured, handling interruptions, policy, latency."

Click the red **Mid-sentence corrector × Extraction** cell. The transcript opens with the failing line flagged:
> "The caller said 'pachaas... nahi, pachpan hazaar', fifty, no, fifty-five thousand. The agent booked 50,000. Here's the exact line and the reason, and the call trace shows what scored each metric."

**0:55–1:05 · Retry** (optional; skip if short on time)
Click **Retry 1 failed call** in the run bar.
> "One call never connected. Retry redials it inside the same run, and it fails again: the flaw is in the agent, not the line."

**1:05–1:30 · Patch and re-run**
Scroll to **Patch** and click **Generate patch**. Point at the failure clusters, then the diff:
> "Gauntlet groups the failures and rewrites the instructions behind them: keep the corrected amount, read details back, never waive fees on a claimed approval."

Click **Apply and re-run**. It saves prompt v2 and dials the same 30 callers again.

**1:30–1:45 · Compare**
When the re-run completes, click **compare** in the run bar.
> "Same callers, patched prompt: 20% to 90%. In mock mode these outcomes are simulated. Sim mode measures them for real: Gemini plays the caller and the agent using the actual prompts."

**1:45–2:00 · What's real**
Click **Evidence** in the header.
> "Here's a real conversation Gemini simulated and graded: the corrector call, with the judge's reasons. The real Bolna integration is built too: agent setup, webhooks and per-run prompt switching. Real mode needs one Bolna number."

Optionally click **Review**:
> "And because LLM judges make mistakes, a person spot-checks ten calls and we track how often they agree with the judge."

## If something goes wrong

- **Server error or a stuck run:** Ctrl-C, run `demo_reset.py`, restart. The example run is there within seconds.
- **The browser misbehaves:** switch to the backup recording.
- **Running long:** skip the retry and the Review tab.
- **Record a backup first:** do one clean take with QuickTime (File → New Screen Recording) before the live demo, and keep it open in another window.

## Likely questions, and honest answers

- **"Are these real phone calls?"** Not in this demo; they're scripted to keep it free and repeatable. Real mode places Bolna calls (one Bolna number for the target agent); sim mode has Gemini play both sides from the real prompts.
- **"Is the 20% → 90% real?"** In mock mode, no. It shows the workflow. Sim mode makes the outcome depend on the actual prompt, and `scripts/check_flaws.py` measures, per persona, whether each flaw fires on v1 and is fixed by the patch.
- **"How do you score calls?"** Extraction and latency are checked in code against the persona's expected values. Task, interruptions and policy go to an LLM judge (Gemini) with a fixed rubric, or an offline judge when there's no key. Every score has a reason and points at the line that failed.
- **"How do you know the judge is right?"** The Review tab: a person labels a stable sample of 10 calls without seeing the judge's verdict, and we track agreement per metric.
- **"Who writes the fix?"** Gemini drafts a revised prompt from the clustered failures, or offline rewrite rules do when there's no key. Either way you see the diff before applying it.
- **"What would it cost to run for real?"** About 60 calls for a baseline plus re-run, each billed on both the caller and the target agent; there's a per-run call cap. One Bolna number for the target.
- **"What's next?"** Run it on every prompt change in CI, custom personas, more Indian languages, and calibrating the judge against human labels.
