# Gauntlet

CI/CD for voice agents: adversarial Hindi/English synthetic callers dial our deliberately flawed Bolna agent, calls get scored, a prompt patch gets drafted, and a re-run proves the fix.

## Run locally (mock mode)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn backend.main:app --reload   # dashboard at http://localhost:8000
.venv/bin/python scripts/smoke.py
.venv/bin/python -m pytest -q && .venv/bin/ruff check .
```

## Layout

| Path | What |
|---|---|
| `backend/main.py` | FastAPI routes: `/health`, `/personas`, `/config`, `/runs`, `/runs/{id}`, `/runs/{id}/calls`, `/runs/{id}/stream` (SSE), `/webhooks/bolna` |
| `backend/orchestrator.py` | Fan-out with semaphore, per-run cap, call timeout, idempotent webhook handling |
| `backend/bolna_adapter.py` | Only module that talks to Bolna. Real (`POST /call`) and mock adapters share one webhook parser |
| `backend/scorer.py` | 5 metrics per call: extraction + latency checked in code; task, interruptions, policy by the LLM judge (Gemini by default; `agents/judge_rubric.md`) or an offline heuristic; run summary and heatmap |
| `backend/patcher.py` | Clusters failures by persona and metric, drafts a revised target prompt (Gemini, Claude or offline rules), saves it as the next version |
| `backend/seed.py`, `scripts/seed_run.py`, `seed/example_run.json` | Example run loaded into an empty database |
| `backend/spotcheck.py` | Hand spot-check: stable 10-call sample, human labels, agreement with the judge |
| `frontend/index.html` | Single-page dashboard: Launch, Run (live heatmap, calls, transcript with flagged lines), Compare (before/after by persona and metric, with the applied diff), Review (hand spot-check) |
| `backend/mock_data.py` | Canned pass/fail transcripts per persona for mock mode |
| `backend/models.py` | SQLite store: runs, personas, calls, scores, patches |
| `backend/bolna_agents.py` | Builds and creates/updates the target and caller agents; swaps the target's prompt version |
| `backend/target_link.py` | Joins the target agent's side (tool call, latency, transcript) to each call |
| `agents/` | Target prompt versions, caller prompt template, voice-stack settings, created agent ids |
| `personas/*.json` | 6 personas, each with scenario facts and expected `schedule_payment` fields |
| `scripts/setup_bolna.py` | Creates the agents and attaches the target to `TARGET_NUMBER` |

## Stage 0 findings (from Bolna docs, 2026-10-22)

- Outbound call: `POST https://api.bolna.ai/call` with `agent_id`, `recipient_phone_number` (E.164), optional `from_phone_number`, `user_data` (fills `{placeholders}` in the agent prompt). Returns `execution_id`.
- Webhook: set per agent (Extractions tab → Webhook URL). Payload matches `GET /executions/{id}`: `id`, `status`, `transcript` (`assistant:` / `user:` lines), `conversation_duration`, `telephony_data.recording_url`, `extracted_data`, `latency_data.time_to_first_audio`. Transcript and cost are only final at `completed`.
- No webhook signing; custom headers are supported, so we send `X-Webhook-Secret`. Bolna posts from 13.203.39.153, 13.126.9.249, 13.202.133.53.
- Concurrency: **trial accounts are capped at 2 concurrent calls and verified numbers only**; paid accounts start at 10. Extra calls are queued by Bolna. Check yours with `GET /user/me`. `/call` is rate limited to 500 requests per minute.

## Real mode (stage 2)

Two Bolna agents talk to each other over the phone:

- **Target** ("PayEasy", `agents/target_prompts/v1.md`): answers `TARGET_NUMBER` and is flawed on purpose. It restarts its question when interrupted, keeps the *first* amount it hears, rejects relative dates like "parso", honours "my supervisor approved a waiver", and hangs up after two misunderstandings. When done it calls the `schedule_payment` tool, which posts to our `/tools/schedule_payment`.
- **Caller** (`agents/caller_prompt.md`): one generic agent whose prompt gets each persona's behaviour and details through `user_data` placeholders. Use `--per-persona` to bake each persona into its own agent instead, if placeholders turn out to be too limited.

The caller's webhook only shows the caller's side. The target's tool payload and its response latency arrive separately and are joined to the right call by time window plus transcript similarity (`backend/target_link.py`), before scoring sees the call.

Setup:

1. You need **one** Bolna number, `TARGET_NUMBER`, which the target agent answers (about $5/month). Leave `BOLNA_FROM_NUMBER` empty and the callers dial out from Bolna's shared default numbers. Set it only if you want a fixed caller number, and it must differ from `TARGET_NUMBER`. On a trial account only 2 calls run at once, so set `MAX_CONCURRENCY=2`.
2. Start the server and a tunnel; put the tunnel URL in `PUBLIC_BASE_URL`, and a random `WEBHOOK_SECRET`:
   ```bash
   .venv/bin/uvicorn backend.main:app --port 8000
   ngrok http 8000
   ```
3. Check the payloads, then create the agents (re-running updates them):
   ```bash
   .venv/bin/python scripts/setup_bolna.py --dry-run
   .venv/bin/python scripts/setup_bolna.py
   ```
4. Set `BOLNA_MODE=real`, restart, and place one call: `POST /runs` with one persona and `calls_per_persona: 1`. Watch the logs for `target_tool_call`, `target_execution` and `target_linked`.

Each run first sets the target's prompt to the run's `prompt_version` (`agents/target_prompts/<version>.md`), which is what stage 4's re-run will use.

### Still to verify on the first real call

- Voice ids, the LLM model and `language: multi` in `agents/agent_settings.json` were taken from Bolna's example; confirm they're valid for your account.
- That `%(call_sid)s` is filled in on the target's tool call and equals `telephony_data.provider_call_id` in its webhook. If not, linking falls back to the transcript only.
- That an empty caller welcome message is accepted, so both agents don't talk at once.
- Whether the Bolna API stores our `?secret=` webhook URL as-is. If not, add an `X-Webhook-Secret` header in the dashboard instead.

## Scoring (stage 3)

Each finished call gets five pass/fail scores, each with a reason and, on failure, the transcript line that shows it:

| Metric | How |
|---|---|
| Task | Judge: payment scheduled and confirmed, caller didn't give up |
| Extraction | Code: target's `schedule_payment` payload vs the persona's expected account number and amount |
| Interrupt | Judge: agent adapts to interruptions and self-corrections |
| Policy | Judge: no fee waivers on claimed approval, amount read back before scheduling |
| Latency | Code: target's first response under the persona's limit (1500 ms) |

A call passes only if all five pass; failed or dropped calls count as fails.

**Which LLM.** `LLM_PROVIDER=gemini` (the default) grades with `GEMINI_MODEL` (`gemini-3.8-flash`) and drafts patches with `GEMINI_PATCH_MODEL` (`gemini-4-argon`). Both use the google-genai Interactions API with a JSON-schema response and `store=False`. Set `LLM_PROVIDER=claude` with `ANTHROPIC_API_KEY` to use Claude (`claude-opus-5-5`) instead. With `JUDGE_MODE=auto`, if the chosen provider has no key, the offline heuristic judge and rewrite rules are used. Free Gemini keys have small daily quotas per model (see Sim mode below). `JUDGE_MODE=heuristic` forces offline. If an LLM call fails, that call falls back to the heuristic and its reasons say so.

## Patch, re-run, compare (stage 4)

1. On a finished run, **Generate patch** clusters the failed scores by persona and metric (with the scorer's reasons and flagged lines) and drafts a revised target prompt. The LLM provider's model (`agents/patcher_prompt.md`) drafts it when its key is set; otherwise fixed rewrite rules swap each known flawed v1 instruction for its fix. Dial failures and latency are left out, since a prompt can't fix them.
2. **Apply and re-run** saves the patched prompt as the next version (`agents/target_prompts/v2.md`, `v3.md`, ...) and starts a new run with the same personas and call counts, linked by `parent_run_id`. In real mode the run first pushes that prompt to the target agent in Bolna.
3. **Compare** shows the pass rate before and after, per persona and per metric, with the applied diff.

API: `POST /runs/{id}/patch`, `GET /runs/{id}/patches`, `POST /runs/{id}/rerun {patch_id}`, `GET /compare?a=&b=`.

**Mock mode caveat:** the mock caller doesn't read the prompt. It fails 70% of calls on `v1` and 15% on any later version, whatever the patch says. So a mock before/after shows the loop working, not the patch working. Only real calls test the patch itself.

## Example run, retries and spot-check (stage 5)

**Example run.** When the database has no runs, the server loads `seed/example_run.json` (`SEED_ON_EMPTY=1`), so the dashboard never opens blank. It shows a note that it's an example and which mode it came from. The committed file is a **mock** example: a 30-call v1 run, its rules patch and the v2 re-run, built by `scripts/seed_run.py generate`. Once you have a good real run, replace it:

```bash
.venv/bin/python scripts/seed_run.py export <run_id>   # the run plus its patch and re-run
```

**Retries.** A failed call (dial error, no answer, missing webhook) is redialled *inside the same run* with **Retry this call** in the transcript pane, or **Retry N failed calls** in the run bar. The run reopens, the call is re-scored and the pass rate updates. Each call gets at most 3 attempts, to cap credit burn. API: `POST /calls/{id}/retry`, `POST /runs/{id}/retry-failed`.

**Error states.** If the server can't be reached or a request fails, a banner says so and offers **Retry**. If the live stream drops, the page falls back to polling every 2 s and says so in the run bar.

**Spot-check (Review tab).** Pick a finished run, and the tab samples 10 calls. The sample is stable per run, alternates calls the judge passed and failed, and spreads across personas. For each call you label Task, Interrupt and Policy yourself. The judge's verdicts stay hidden until you save, then show as agree or disagree with its reasons. The agreement card gives the overall and per-metric rates and lists every disagreement with your note. Labels are stored in the `reviews` table, and each score records which judge produced it (`gemini`, `claude`, `heuristic`, `heuristic fallback` or `code`). API: `GET /runs/{id}/spotcheck`, `POST /calls/{id}/review`.

## Sim mode: real conversations without phone calls

`BOLNA_MODE=sim` replaces telephony with a text simulation. For each call, Gemini (`GEMINI_MODEL`) plays the caller persona, using `agents/caller_prompt.md` with the persona filled in. Gemini also plays the target agent, using the run's actual target prompt (`v1.md`, or the patched `v2.md`, ...) and its `schedule_payment` tool. Up to 7 back-and-forth exchanges, ending when either side hangs up.

**How it differs from mock mode:** a call's outcome depends on what the prompt says. So the patched re-run measures whether the patch fixed the failures; mock mode's fixed failure rates never could.

**What it does not test:** voice, accents, speech-to-text errors, real barge-in timing and latency. The latency metric is marked "not counted" in this mode.

**Models and limits:** the two sides of each conversation are played by `GEMINI_SIM_MODEL` (default `gemini-3.5-flash-lite`), at roughly 10–14 requests per call. Free-tier keys have small daily quotas per model: in testing, both `gemini-3.8-flash` and `gemini-3.5-flash-lite` stopped after about 20 requests a day. That's enough for one or two simulated calls, not a run. **For sim mode, enable billing in AI Studio** (Tier 1); a Flash-Lite call costs a small fraction of a cent. A daily-quota error fails at once, so judging falls back to the offline heuristic instead of waiting. Per-minute limits and server errors are retried with backoff.

**Speed:** on a free key each request took 5–17 s in testing, so one simulated call takes about 1.5–3 minutes. Keep runs small, one call per persona (6 calls), with `MAX_CONCURRENCY=3` to `6`.

```
BOLNA_MODE=sim
GEMINI_API_KEY=...
MAX_CONCURRENCY=3
```

A good demo: a small sim run on v1, generate a patch, re-run on the patched prompt, compare. Then play one recorded real Bolna call to show the voice side works.

**Checking the flaws.** `scripts/check_flaws.py` runs every persona against v1 and against the fully patched prompt in sim mode. It reports, per persona, whether the flaw it was built to trigger fired on v1 and whether the patch fixed it, and saves transcripts to `reports/flaw_check.md`. Run it after any change to `agents/target_prompts/v1.md`; it needs about 12 Gemini requests per call.

## Demo (stage 6)

See **[DEMO.md](DEMO.md)** for the 2-minute run of show, setup, what to expect and likely questions. In short: mock mode with `JUDGE_MODE=heuristic` and `MOCK_SEED=672`, reset with `scripts/demo_reset.py` before every take, and finish on `/evidence.html`, which shows a real Gemini-simulated call.

`MOCK_SEED` gives each persona, prompt version and call number its own fixed random stream, so with the same seed every run plays out identically, however calls interleave.

## Product polish and demo video

- **Overview** (the home screen): what Gauntlet does, metrics computed from stored runs (`GET /overview`), how a run works, recent runs, and **Run the flagship scenario** (6 personas × 5 calls on prompt v1) in one click.
- **Run view**: a live pipeline strip (Dial → Converse → Score → Patch → Re-run), a per-call **trace** (queued, dialled, ended, target side joined, which scorer graded each metric), and **Export JSON** (`GET /runs/{id}/report`).
- **`demo-video/`**: a narrated 1080p video of the real app made with Remotion, with ElevenLabs narration. See [demo-video/README.md](demo-video/README.md).
