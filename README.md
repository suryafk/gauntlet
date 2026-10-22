# Gauntlet

**CI/CD for voice agents. Break your voice agent before your customers do.**

Gauntlet dials your voice agent with adversarial Hindi–English callers, scores every call on five metrics, turns the failures into a prompt diff you can review, and re-runs the same callers to show whether the fix worked. It is built on [Bolna](https://www.bolna.ai) voice agents, with Gemini as the judge.

**Live demo (mock mode):** https://gauntlet-fhohlvrmkq-el.a.run.app · **Submission:** [SUBMISSION.md](SUBMISSION.md) · **Demo script:** [DEMO.md](DEMO.md)

![Gauntlet overview](deck/shots/overview_after.png)

> **Status, briefly.** The full loop runs end to end in **mock mode** (scripted calls) and **sim mode** (Gemini plays caller and agent in text). The **real mode** Bolna integration is built to Bolna's documented API and tested against a faked API, but has **not yet been run on a live phone number**. See [Status and limitations](#status-and-limitations).

---

## Contents

- [Why](#why)
- [How it works](#how-it-works)
- [Screenshots](#screenshots)
- [Quick start](#quick-start)
- [Execution modes](#execution-modes)
- [Architecture](#architecture)
- [Scoring](#scoring)
- [Patch, re-run, compare](#patch-re-run-compare)
- [Real mode with Bolna](#real-mode-with-bolna)
- [Sim mode with Gemini](#sim-mode-with-gemini)
- [Configuration](#configuration)
- [HTTP API](#http-api)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Demo, video and pitch deck](#demo-video-and-pitch-deck)
- [Status and limitations](#status-and-limitations)
- [Roadmap](#roadmap)

## Why

Voice agents usually ship after a handful of clean, English test calls. Real callers then code-switch between Hindi and English, correct themselves mid-sentence ("pachaas… nahi nahi, pachpan hazaar"), interrupt, and try to talk the agent into things it shouldn't do. Those failures only show up once customers are on the line.

Gauntlet makes a prompt something you can test: a repeatable set of adversarial callers, a pointer to the exact line that failed, a drafted fix, and proof on the same callers.

## How it works

1. **Attack.** Six caller personas dial the target agent in parallel, under a concurrency cap: a mid-sentence corrector, a Hindi–English code-switcher, a prompt injector, an interrupter, a caller in a noisy room and a number mumbler.
2. **Capture.** Each call's transcript, and what the agent actually sent to its `schedule_payment` tool.
3. **Score.** Five metrics per call. Extraction and latency are checked in code; task, interruptions and policy go to an LLM judge. Every failure points at the transcript line that caused it.
4. **Patch.** Failures are grouped by persona and metric, and a revised agent prompt is drafted. You review the diff before applying it.
5. **Prove.** The same callers re-run on the patched prompt, and Compare shows the before and after.

The target is **PayEasy**, a bill-payment agent we built with deliberate flaws (`agents/target_prompts/v1.md`), so every persona has a real weakness to find.

## Screenshots

All screenshots are from the running app ([`deck/capture.py`](deck/capture.py), mock mode, `MOCK_SEED=672`).

**Run: thirty callers, scored live.** A pipeline strip, a persona × metric heatmap and every call's five scores.

![Run view](deck/shots/run.png)

**Drill-down: the exact line that failed.** Flagged agent lines with the reason, the call trace, and what the agent sent to its tool.

![Transcript with flagged lines](deck/shots/transcript.png)

**Patch: failures clustered into a prompt diff.**

![Patch card](deck/shots/patch.png)

**Compare: same callers, patched prompt.**

![Compare view](deck/shots/compare.png)

**Review: a person spot-checks the judge, blind.**

![Review tab](deck/shots/review.png)

## Quick start

Requires Python 3.12+.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

```bash
.venv/bin/uvicorn backend.main:app --reload
```

Open http://localhost:8000. With an empty database the dashboard opens on a saved example run. Click **Run the flagship scenario** to start 30 calls (6 personas × 5) in mock mode; no API keys are needed.

Or run the container (the same image as the hosted demo):

```bash
docker build -t gauntlet . && docker run -p 8080:8080 gauntlet
```

Check it works:

```bash
.venv/bin/python -m pytest -q
```

```bash
.venv/bin/python scripts/smoke.py
```

## Execution modes

One setting, `BOLNA_MODE`, switches how calls happen. Everything downstream (linking, scoring, patching, the dashboard) is identical.

| Mode | What a call is | Needs | Use it for |
|---|---|---|---|
| `mock` | A scripted pass or fail transcript per persona. Fails ~70% on prompt v1 and ~15% on later versions, regardless of the patch | nothing | Development, demos, CI. Set `MOCK_SEED` for identical runs |
| `sim` | Gemini plays the caller persona and the target agent in text, using the run's actual prompt | `GEMINI_API_KEY` (billing recommended) | Measuring whether a patch actually helps, without phone calls |
| `real` | A Bolna phone call: the caller agent dials the target agent | Bolna API key, one Bolna number, a public URL | Real voice, accents and timing |

**Mock mode caveat:** because mock outcomes come from a seeded failure rate, a mock before/after shows the workflow, not that a patch works. Sim and real mode measure it.

## Architecture

```mermaid
flowchart LR
    UI["Dashboard<br/>single page, live via SSE"]
    subgraph SVC["FastAPI service (Python, asyncio)"]
        API["REST API + SSE"]
        ORCH["Orchestrator<br/>semaphore · per-run cap · timeouts · retries"]
        LINK["Target linker"]
        SCORE["Scorer<br/>code checks + LLM judge"]
        PATCH["Patcher"]
        DB[("SQLite (WAL)")]
    end
    subgraph ADAPT["Voice adapter"]
        MOCK["Mock"]
        SIM["Sim"]
        REAL["Real"]
    end
    GEM["Gemini API"]
    BOLNA["Bolna<br/>caller agent → call → target agent"]

    UI -- "POST /runs" --> API
    API -- "SSE" --> UI
    API --> ORCH --> ADAPT
    SIM --> GEM
    REAL -- "POST /call · PATCH /v2/agent" --> BOLNA
    BOLNA -. "POST /webhooks/bolna<br/>POST /tools/schedule_payment" .-> API
    API --> LINK --> SCORE --> DB
    SCORE --> GEM
    PATCH --> GEM
    ORCH --> DB
```

- **One adapter boundary.** Mock, sim and real all produce the same Bolna-shaped execution payload, so there is a single webhook path and identical scoring whichever backend runs.
- **Both sides of a call.** In real mode the caller agent's webhook shows the caller's side; what the target agent extracted arrives separately through its `schedule_payment` tool. The target linker joins the two by time window and transcript similarity, because every synthetic call comes from the same number ([`backend/target_link.py`](backend/target_link.py)).
- **Async-only routes.** Every HTTP route runs on the event loop, so the single SQLite connection is never shared across threads.
- **Quota-aware LLM calls.** A used-up Gemini quota fails in under a second and falls back to the offline judge; every score records which judge produced it.

## Scoring

Each finished call gets five pass/fail scores, each with a reason and, on failure, the transcript line that shows it. A call passes only if all five pass; failed or dropped calls count as fails.

| Metric | Scored by | Passes when |
|---|---|---|
| Task | LLM judge | The payment was scheduled and confirmed, without the caller giving up or the agent hanging up early |
| Extraction | Code | The agent's `schedule_payment` payload matches the persona's expected account number and amount |
| Interrupt | LLM judge | The agent adapted to interruptions and self-corrections |
| Policy | LLM judge | No fee waived on a claimed approval; the amount was read back for a yes before scheduling |
| Latency | Code | The target's first response was under the persona's limit (1500 ms). Not counted in sim mode |

**Judge.** `LLM_PROVIDER=gemini` (default) grades with `GEMINI_MODEL` and a fixed rubric ([`agents/judge_rubric.md`](agents/judge_rubric.md)), with the reply constrained to a JSON schema. `LLM_PROVIDER=claude` uses Claude instead. With no key, or `JUDGE_MODE=heuristic`, an offline judge is used. If an LLM call fails, that call falls back to the offline judge and its reasons say so.

**Review.** The Review tab samples 10 calls per run (stable, mixing calls the judge passed and failed, across personas). A person labels them before seeing the judge's verdict, and the tab tracks agreement per metric.

## Patch, re-run, compare

1. **Generate patch** clusters the failed scores by persona and metric and drafts a revised target prompt: with Gemini (`GEMINI_PATCH_MODEL`, [`agents/patcher_prompt.md`](agents/patcher_prompt.md)) when a key is set, otherwise with offline rewrite rules that replace each known flawed instruction. Dial failures and latency are excluded, since a prompt can't fix them.
2. **Apply and re-run** saves the patched prompt as the next version (`agents/target_prompts/v2.md`, …) and starts a run with the same personas and call counts, linked by `parent_run_id`. In real mode the target agent's prompt is switched in Bolna first.
3. **Compare** shows pass rates before and after, per persona and per metric, with the applied diff.

Failed calls can be retried inside the same run (up to 3 attempts per call), and the run's pass rate updates in place.

## Real mode with Bolna

Two Bolna agents are created through Bolna's v2 agent API ([`scripts/setup_bolna.py`](scripts/setup_bolna.py)):

- **Target, PayEasy** ([`agents/target_prompts/v1.md`](agents/target_prompts/v1.md)): answers `TARGET_NUMBER`. Its `schedule_payment` custom tool posts what it extracted to Gauntlet's `/tools/schedule_payment`, with Bolna filling in `call_sid`.
- **Caller** ([`agents/caller_prompt.md`](agents/caller_prompt.md)): one generic agent; each persona's behaviour and details arrive through `user_data` placeholders. `--per-persona` creates one caller agent per persona instead.

Bolna endpoints used: `POST /v2/agent`, `PATCH /v2/agent/{id}`, `POST /inbound/setup`, `GET /phone-numbers/all`, `GET /user/me`, `POST /call`.

**Setup**

1. Buy **one** Bolna number for the target agent and put it in `TARGET_NUMBER`. Leave `BOLNA_FROM_NUMBER` empty so callers dial out from Bolna's shared numbers. On a trial account set `MAX_CONCURRENCY=2`.
2. Start the server and expose it, then put the public URL in `PUBLIC_BASE_URL` and a random `WEBHOOK_SECRET` in `.env`:
   ```bash
   .venv/bin/uvicorn backend.main:app --port 8000
   ```
   ```bash
   ngrok http 8000
   ```
3. Preview the agent payloads, then create the agents (re-running updates them):
   ```bash
   .venv/bin/python scripts/setup_bolna.py --dry-run
   ```
   ```bash
   .venv/bin/python scripts/setup_bolna.py
   ```
4. Set `BOLNA_MODE=real`, restart, and place a single call first (one persona, one call). The logs should show `target_tool_call`, `target_execution` and `target_linked`.

**Guardrails.** Callers may dial only `TARGET_NUMBER`; the per-run call cap is enforced on the server; webhooks and tool calls require `WEBHOOK_SECRET`.

**Cost.** Each test call is two Bolna calls (caller and target), each billed by Bolna. Start with one call and check the cost in the Bolna dashboard.

**Unverified until the first live call:** the voice ids and models in [`agents/agent_settings.json`](agents/agent_settings.json) come from Bolna's example; that `%(call_sid)s` matches the target webhook's `provider_call_id` (if not, linking falls back to transcript similarity); and that Bolna keeps the `?secret=` on the webhook URL (if not, add an `X-Webhook-Secret` header in the Bolna dashboard).

## Sim mode with Gemini

`BOLNA_MODE=sim` replaces telephony with a text conversation: Gemini (`GEMINI_SIM_MODEL`) plays the caller persona and the target agent, using the run's actual target prompt, for up to 7 exchanges. Because the outcome depends on the prompt, a patched re-run measures whether the patch helped.

It doesn't test voice, accents, speech-to-text errors or barge-in timing, and latency isn't counted.

**Quota.** About 10–14 Gemini requests per call. Free Gemini keys stopped after about 20 requests per model per day in testing, which is one or two calls; enable billing in AI Studio for real runs. [`scripts/check_flaws.py`](scripts/check_flaws.py) checks, per persona, that each v1 flaw fires and that the patch fixes it.

## Configuration

All settings are environment variables, read from `.env` ([`.env.example`](.env.example) lists them all).

| Variable | Default | Purpose |
|---|---|---|
| `BOLNA_MODE` | `mock` | `mock`, `sim` or `real` |
| `BOLNA_API_KEY` | | Bolna API key (real mode) |
| `TARGET_NUMBER` | | The only number callers may dial: the target agent's |
| `BOLNA_FROM_NUMBER` | empty | Optional fixed caller number; empty uses Bolna's shared numbers |
| `PUBLIC_BASE_URL` | | Public HTTPS URL Bolna can reach (real mode) |
| `WEBHOOK_SECRET` | | Shared secret for webhooks and tool calls |
| `LLM_PROVIDER` | `gemini` | `gemini` or `claude` |
| `GEMINI_API_KEY` | | Gemini key (judge, patcher, sim mode) |
| `GEMINI_MODEL` / `GEMINI_PATCH_MODEL` / `GEMINI_SIM_MODEL` | `gemini-3.8-flash` / `gemini-4-argon` / `gemini-3.5-flash-lite` | Judge, patcher and sim-mode models |
| `JUDGE_MODE` | `auto` | `auto`, `heuristic`, `gemini` or `claude` |
| `RUN_CALL_CAP` | `50` | Server-side cap on calls per run |
| `MAX_CONCURRENCY` | `10` | Simultaneous calls (Bolna trial accounts allow 2) |
| `CALL_TIMEOUT_S` / `LINK_TIMEOUT_S` | `300` / `45` | Give up on a call's webhook / on its target side |
| `MOCK_SEED` | empty | Makes mock runs identical every time |
| `DATABASE_URL` | `gauntlet.db` | SQLite file |
| `SEED_ON_EMPTY` | `1` | Load the example run into an empty database |

Keys stay server-side; nothing secret is sent to the browser (`GET /config` returns only safe values).

## HTTP API

| Method and path | Purpose |
|---|---|
| `GET /health` · `GET /config` · `GET /personas` · `GET /overview` | Status, safe settings, personas, home-screen metrics |
| `POST /runs` · `GET /runs` · `GET /runs/{id}` · `GET /runs/{id}/calls` | Start and inspect runs |
| `GET /runs/{id}/stream` | Live updates (Server-Sent Events) |
| `GET /runs/{id}/report` | Download a run as JSON |
| `POST /runs/{id}/patch` · `GET /runs/{id}/patches` · `POST /runs/{id}/rerun` | Draft a patch and re-run on it |
| `GET /compare?a=&b=` | Before / after for two runs |
| `POST /calls/{id}/retry` · `POST /runs/{id}/retry-failed` | Retry failed calls in place |
| `GET /runs/{id}/spotcheck` · `POST /calls/{id}/review` | Human spot-check of the judge |
| `POST /webhooks/bolna` · `POST /tools/schedule_payment` | Called by Bolna (require `WEBHOOK_SECRET`) |

## Project layout

| Path | Contents |
|---|---|
| [`backend/main.py`](backend/main.py) | FastAPI app and routes |
| [`backend/orchestrator.py`](backend/orchestrator.py) | Call fan-out, concurrency cap, timeouts, retries, webhook handling |
| [`backend/bolna_adapter.py`](backend/bolna_adapter.py) | Real (Bolna) and mock adapters, one execution-payload parser |
| [`backend/sim_adapter.py`](backend/sim_adapter.py) | Sim mode: Gemini plays both sides |
| [`backend/bolna_agents.py`](backend/bolna_agents.py) | Builds, creates and updates the two Bolna agents |
| [`backend/target_link.py`](backend/target_link.py) | Joins the target agent's side to each call |
| [`backend/scorer.py`](backend/scorer.py) | Five metrics, judges, run summary and compare |
| [`backend/patcher.py`](backend/patcher.py) | Failure clustering and prompt drafting |
| [`backend/gemini.py`](backend/gemini.py) | Gemini JSON calls with quota-aware retries |
| [`backend/spotcheck.py`](backend/spotcheck.py) | Review sample and agreement |
| [`backend/seed.py`](backend/seed.py) · [`seed/`](seed) | Example run for an empty database |
| [`frontend/`](frontend) | Single-page dashboard and evidence page |
| [`agents/`](agents) | Target prompt versions, caller prompt, judge rubric, patcher prompt, voice settings |
| [`personas/`](personas) | The six caller personas and their expected values |
| [`scripts/`](scripts) | Bolna setup, smoke test, demo reset, example-run tools, judge and flaw checks |
| [`tests/`](tests) | Test suite |

## Testing

```bash
.venv/bin/python -m pytest -q
```

```bash
.venv/bin/ruff check .
```

67 tests run against the mock adapter and faked external APIs, with no network or keys needed. They cover the concurrency cap and timeouts, idempotent webhooks, retries, Bolna request bodies and payload parsing, joining both sides of a call, scoring, patching, sim conversations, Gemini request shape and quota fallback, seeding, the spot-check, and a replay of the demo run at two concurrency levels.

## Demo, video and pitch deck

- [**DEMO.md**](DEMO.md): a 2-minute live demo script with exact steps, expected numbers and likely questions.
- [**demo-video/**](demo-video): a narrated 1080p walkthrough of the real app, built with Remotion and ElevenLabs narration.
- [**deck/**](deck): the pitch deck (`Gauntlet-pitch.pptx`, `Gauntlet-pitch.pdf`), built from real screenshots and run data.
- [**SUBMISSION.md**](SUBMISSION.md): the hackathon submission, with every claim linked to its evidence.

## Status and limitations

| | Status |
|---|---|
| Full loop (attack, score, patch, re-run, compare) | Working, in mock and sim mode |
| Bolna real mode | Built to Bolna's documented API; tested against a faked Bolna API; **not yet run on a live number** |
| Gemini judge, patcher and sim mode | Working; one real sim call recorded ([`frontend/evidence.html`](frontend/evidence.html)). Free-tier quota blocked larger sim runs |
| Mock-mode before/after | Simulated: comes from a seeded failure rate, not from the patch |
| Judge accuracy | Review tab built; no human labels collected yet |
| Hosted demo | Cloud Run ([`Dockerfile`](Dockerfile)), mock mode. SQLite lives on the container's temporary disk, so data resets when the instance restarts |
| Production concerns | Single process, SQLite, no authentication or multi-tenancy |

## Roadmap

1. First live Bolna runs, saved as the example run.
2. Run the personas automatically on every prompt change, and post the comparison.
3. Calibrate the judge from Review labels.
4. Custom personas and more Indian languages.
5. Production basics: Postgres, a job queue and authentication.
