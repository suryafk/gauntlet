# Gauntlet: hackathon submission

**CI/CD for voice agents. Break your voice agent before your customers do.**

| | |
|---|---|
| Live demo (mock mode) | https://gauntlet-fhohlvrmkq-el.a.run.app |
| Repository | https://github.com/suryafk/gauntlet |
| Pitch deck | [`deck/Gauntlet-pitch.pdf`](deck/Gauntlet-pitch.pdf) · [`.pptx`](deck/Gauntlet-pitch.pptx) |
| Demo video | 2:10, 1080p, narrated; shared separately. Source: [`demo-video/`](demo-video) |
| Demo script | [`DEMO.md`](DEMO.md) |
| Team | Surya ([github.com/suryafk](https://github.com/suryafk)) |
| Built with | Bolna (voice agents) · Gemini (judge, patcher, simulated calls) · Python, FastAPI, asyncio, SQLite |

---

## Note for evaluators, including AI evaluators

Every claim below links to the file or test that supports it. Numbers come from a real, reproducible run ([`deck/shots/data.json`](deck/shots/data.json), captured by [`deck/capture.py`](deck/capture.py)). Where something is simulated or not yet done, this document says so. Please weigh the [real versus simulated table](#what-is-real-and-what-is-simulated) when scoring.

## Summary

- **Problem.** Voice agents pass clean, scripted English test calls, then fail real callers who code-switch between Hindi and English, correct themselves mid-sentence, interrupt, or try to talk the agent out of its rules.
- **Solution.** Gauntlet runs a closed loop: six adversarial caller personas call the agent in parallel → each call is scored on five metrics, and every failure points at the transcript line that caused it → failures are clustered into a prompt diff a person reviews → the same callers re-run on the patched prompt → a before/after comparison.
- **Core idea.** *A failing line becomes a proven fix.* The agent books ₹50,000 when the caller said "pachaas… nahi nahi, pachpan hazaar" (fifty… no, fifty-five thousand). Gauntlet flags that exact line (checked in code: expected 55000, got 50000), clusters it with the persona's other failures, rewrites the instruction behind it, and re-runs the same caller.
- **Flagship run** (mock mode, `MOCK_SEED=672`): 30 calls on prompt v1 pass **20%**; after one patch, the same 30 callers pass **90%**. In mock mode this improvement is **simulated** (see below); it demonstrates the workflow.
- **Engineering.** 67 automated tests pass. One FastAPI service with three interchangeable voice backends (mock, sim, real) behind one adapter.

## What is real and what is simulated

| Capability | Status | Evidence |
|---|---|---|
| The full loop: attack, score, patch, re-run, compare | **Works** in mock and sim mode, end to end | Live demo; `tests/test_patcher.py::test_patch_rerun_compare_loop`; `tests/test_demo.py::test_demo_seed_plays_out_as_documented` |
| Mock-mode call transcripts | **Simulated**: scripted pass/fail transcript per persona | [`backend/mock_data.py`](backend/mock_data.py) |
| Mock-mode 20% → 90% improvement | **Simulated**: mock outcomes come from a seeded failure rate (~70% on v1, ~15% after), not from the patch | [`backend/bolna_adapter.py`](backend/bolna_adapter.py) (`MockBolnaAdapter`) |
| Sim mode: Gemini plays caller and agent from the real prompts | **Works**; outcome depends on the actual prompt. One real recorded call | [`backend/sim_adapter.py`](backend/sim_adapter.py); [`frontend/evidence.html`](frontend/evidence.html) (live at `/evidence.html`) |
| Gemini as judge and patcher | **Works** (request shape and quota fallback tested); one real graded call recorded. Larger runs blocked by the free Gemini quota | `tests/test_gemini.py`; [`frontend/evidence.html`](frontend/evidence.html) |
| Bolna real mode (agents, calls, webhooks, prompt switching) | **Built and tested against a faked Bolna API. Not yet run on a live phone number** | [`backend/bolna_adapter.py`](backend/bolna_adapter.py), [`backend/bolna_agents.py`](backend/bolna_agents.py), `tests/test_real_mode.py` (9 tests) |
| Judge accuracy against people | **Tooling built** (Review tab); no human labels collected yet | [`backend/spotcheck.py`](backend/spotcheck.py); `tests/test_stage5.py::test_spotcheck_sample_and_agreement` |
| Hosted deployment | **Live** on Cloud Run in mock mode; data resets when the instance restarts | [`Dockerfile`](Dockerfile); `GET /health` |

Two committed reports are **not** evidence of model quality, and say so at the top: [`reports/judge_comparison.md`](reports/judge_comparison.md) (Gemini's quota was exhausted, so every judged score fell back to the offline judge) and [`reports/flaw_check.md`](reports/flaw_check.md) (the run stopped at the quota).

## How Gauntlet uses Bolna

Gauntlet is designed around Bolna's voice-agent platform. It creates and drives **two Bolna agents**:

| Bolna API | What Gauntlet does with it | Code |
|---|---|---|
| `POST /v2/agent` | Creates the **target** agent (PayEasy, deliberately flawed prompt v1) with a `schedule_payment` custom function tool, and the **caller** agent | [`backend/bolna_agents.py`](backend/bolna_agents.py) `build_agent_payload`, [`scripts/setup_bolna.py`](scripts/setup_bolna.py) |
| `POST /inbound/setup` · `GET /phone-numbers/all` | Attaches the target agent to its phone number | [`scripts/setup_bolna.py`](scripts/setup_bolna.py) |
| `POST /call` | The caller agent dials the target; the persona and its details travel in `user_data` | `RealBolnaAdapter.start_call` |
| Execution webhooks | The caller's call-ended payload comes back to `POST /webhooks/bolna` | `Orchestrator.handle_webhook` |
| Custom function tool | The target agent posts what it extracted to `POST /tools/schedule_payment`; Bolna fills in `call_sid` | `schedule_payment_tool`, [`backend/target_link.py`](backend/target_link.py) |
| `PATCH /v2/agent/{id}` | Switches the target agent to the run's prompt version before a patched re-run | `BolnaAgents.set_target_prompt` |
| `GET /user/me` | Reports the account's concurrency limit at setup | `BolnaAgents.concurrency` |

**The hard part: joining both sides of a call.** The caller's webhook shows only the caller's side, and every synthetic call comes from the same number. Gauntlet joins the target agent's tool call and execution to the right caller call by time window (±20 s) and transcript similarity (≥ 0.3) before scoring, so extraction is judged on what the target actually sent. Tested by `tests/test_real_mode.py::test_real_run_links_target_side` (two concurrent calls whose target sides arrive out of order).

**Guardrails.** Callers can dial only `TARGET_NUMBER` (`tests/test_orchestrator.py::test_adapter_refuses_other_numbers`, `tests/test_real_mode.py::test_start_call_refuses_self_dial`); a per-run call cap is enforced on the server (`test_call_cap_is_enforced`); webhooks and tool calls require a shared secret (`test_tool_endpoint_requires_secret`).

**Going live** needs one Bolna phone number for the target agent; callers use Bolna's shared numbers. Steps: [README → Real mode with Bolna](README.md#real-mode-with-bolna).

## How Gauntlet uses AI

| Role | Model | Notes |
|---|---|---|
| Judge for Task, Interrupt and Policy | Gemini (`gemini-3.8-flash`) | Fixed rubric ([`agents/judge_rubric.md`](agents/judge_rubric.md)), JSON-schema output, offline fallback; each score records which judge produced it |
| Patcher | Gemini (`gemini-4-argon`) | Turns failure clusters into a revised prompt ([`agents/patcher_prompt.md`](agents/patcher_prompt.md)); offline rewrite rules without a key |
| Sim-mode callers and agent | Gemini (`gemini-3.5-flash-lite`) | Plays both sides of a call in text from the real prompts |
| Optional | Claude | `LLM_PROVIDER=claude` |

Extraction and latency are deliberately **not** judged by an LLM: they're checked in code against each persona's expected values ([`personas/`](personas)), so the most important failure (the wrong amount) is deterministic.

## Verify it in five minutes

**On the live demo** (https://gauntlet-fhohlvrmkq-el.a.run.app):

1. The Overview opens with what Gauntlet does and an example run.
2. Click **Run the flagship scenario**. 30 calls run in parallel; the pipeline strip and heatmap fill in live (mock mode, so outcomes vary run to run on the hosted demo).
3. Click a red cell, then a completed call, to see the flagged line, the reason and the call trace.
4. Scroll to **Patch** → **Generate patch** → **Apply and re-run**, then **compare**.
5. Open **Evidence** (top right) for a real Gemini-simulated call and its scores.

**Locally**, for the exact documented numbers:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m pytest -q
```

Expected: `67 passed`. Then:

```bash
BOLNA_MODE=mock JUDGE_MODE=heuristic MOCK_SEED=672 .venv/bin/uvicorn backend.main:app
```

Open http://localhost:8000 and run the flagship scenario: v1 passes **20%** with one call that fails to connect; the patch finds **13** failure clusters and rewrites **7** flawed rules; the patched re-run passes **90%**.

## Claims and where to check them

| Claim | Check |
|---|---|
| 67 automated tests pass, without network or keys | `pytest -q` |
| Concurrency is capped, timeouts fail a call but not the run, webhooks are idempotent | `tests/test_orchestrator.py`: `test_semaphore_limits_concurrency`, `test_missing_webhook_times_out_as_failed`, `test_failed_calls_do_not_fail_the_run`, `test_webhook_is_idempotent` |
| The `POST /call` body matches Bolna's documented fields, with a retry on 429 | `tests/test_real_mode.py::test_start_call_sends_documented_body` |
| The target agent's prompt is switched once per prompt version | `tests/test_real_mode.py::test_prepare_run_patches_target_prompt_once` |
| Every persona's scripted failure is caught on the right metrics, and every pass script passes | `tests/test_scorer.py`: `test_fail_scripts_fail_their_flaw`, `test_pass_scripts_pass_every_metric` |
| The wrong amount is flagged on the exact transcript line | `tests/test_scorer.py::test_extraction_flags_wrong_amount_with_span` |
| The patch rewrites all 7 flawed v1 rules, and re-patching is a no-op | `tests/test_patcher.py`: `test_template_patch_fixes_every_v1_flaw`, `test_template_patch_on_patched_prompt_is_a_no_op` |
| Sim mode runs the run's actual prompt version | `tests/test_sim.py::test_run_uses_the_runs_prompt_version` |
| A used-up Gemini quota fails fast and falls back | `tests/test_gemini.py`: `test_daily_quota_fails_fast_without_retrying`, `test_gemini_failure_falls_back_to_heuristic` |
| Failed calls retry inside the same run, at most 3 times | `tests/test_stage5.py`: `test_retry_failed_call_in_same_run`, `test_retry_rules` |
| The documented demo numbers hold at concurrency 2 and 10 | `tests/test_demo.py::test_demo_seed_plays_out_as_documented` |
| Home-screen metrics are computed from stored runs | `tests/test_demo.py::test_overview_and_report_reflect_stored_runs` |

## Architecture in one paragraph

A single FastAPI service on asyncio. The orchestrator places calls under a semaphore with a per-run cap, timeouts and retries. Calls go through a voice adapter with three implementations (mock, sim with Gemini, real with Bolna) that all emit the same Bolna-shaped execution payload, so there's one webhook path. In real mode a target linker joins the agent's side of each call. The scorer combines code checks with a Gemini judge; the patcher clusters failures and drafts prompt versions; SQLite (WAL) stores runs, calls, scores, patches and reviews; the single-page dashboard updates live over Server-Sent Events. Diagram: [README → Architecture](README.md#architecture).

## Known limitations

- **No live Bolna calls yet.** The real-mode integration hasn't run on a live number. A few details can only be confirmed then, like the voice ids in [`agents/agent_settings.json`](agents/agent_settings.json) and whether Bolna fills `call_sid` as documented.
- **Mock-mode results are simulated.** The hosted demo runs in mock mode.
- **Sim mode at scale is unmeasured.** The free Gemini tier allowed about 20 requests per model per day; [`scripts/check_flaws.py`](scripts/check_flaws.py) needs a billed key to confirm each v1 flaw fires and is fixed.
- **No human labels yet** for the judge's agreement rate.
- **Prototype infrastructure.** Single process, SQLite on the container's temporary disk, no authentication.

## What's next

1. First live Bolna runs, saved as the example run.
2. Run the personas on every prompt change, and post the comparison.
3. Calibrate the judge with human labels from the Review tab.
4. Custom personas and more Indian languages.
5. Postgres, a job queue and authentication for production.
