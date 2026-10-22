"""Sim mode: Gemini plays caller and target in text. Gemini is replaced by scripted fakes."""

import asyncio
import json
import shutil
from types import SimpleNamespace

from backend import gemini
from backend.bolna_agents import TARGET_PROMPTS_DIR
from backend.config import ROOT
from backend.events import EventBus
from backend.gemini import GeminiJSON
from backend.models import Store
from backend.orchestrator import Orchestrator
from backend.personas import load_personas
from backend.scorer import HeuristicJudge, Scorer
from backend.sim_adapter import CALLER_SCHEMA, SimAdapter

from .test_orchestrator import make_settings, wait_run

PERSONAS = load_personas(ROOT / "personas")


class ScriptedLLM:
    """Plays the corrector call: the agent keeps the first amount, then schedules 50,000."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.caller_lines = iter([
            "Account 8820 1177. Amount hai pachaas... nahi nahi, pachpan hazaar.",
            "30 tareekh ko.",
        ])
        self.agent_turns = iter([
            {"say": "Okay, 50,000 rupees. On which date?", "call_schedule_payment": False},
            {"say": "Your payment of 50,000 rupees is scheduled for 30 October.", "call_schedule_payment": True},
        ])

    async def ask(self, *, model, system, prompt, schema, thinking_level="medium"):
        self.calls.append({"model": model, "system": system, "prompt": prompt, "schema": schema})
        if self.fail:
            raise RuntimeError("model unavailable")
        if schema is CALLER_SCHEMA:
            line = next(self.caller_lines, None)
            return {"say": line or "Theek hai, bye.", "hang_up": line is None}
        turn = next(self.agent_turns, {"say": "Anything else?", "call_schedule_payment": False})
        return {"say": turn["say"], "hang_up": False, "call_schedule_payment": turn["call_schedule_payment"],
                "schedule_payment": {"account_number": "8820 1177", "amount": 50000,
                                     "payment_date": "2026-10-30", "waive_late_fee": False}}


def sim_settings(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    shutil.copy(TARGET_PROMPTS_DIR / "v1.md", prompts / "v1.md")
    (prompts / "v2.md").write_text("PATCHED PROMPT: always use the corrected amount.\n")
    return make_settings(tmp_path, bolna_mode="sim", target_prompts_dir=prompts)


def test_converse_builds_transcript_and_tool_payload(tmp_path):
    llm = ScriptedLLM()
    adapter = SimAdapter(sim_settings(tmp_path), llm=llm)
    transcript, payload = asyncio.run(adapter.converse(PERSONAS["corrector"], "AGENT PROMPT"))
    assert transcript[0] == ("agent", adapter.welcome)
    assert [who for who, _ in transcript] == ["agent", "caller", "agent", "caller", "agent", "caller"]
    assert transcript[-1] == ("caller", "Theek hai, bye.")  # the goodbye is kept, then the call ends
    assert payload == {"account_number": "88201177", "amount": 50000, "payment_date": "2026-10-30",
                       "waive_late_fee": False}
    # the caller hung up on its third turn, so the loop stopped there
    assert sum(c["schema"] is CALLER_SCHEMA for c in llm.calls) == 3
    caller_call, agent_call = llm.calls[0], llm.calls[1]
    assert "pachpan" in caller_call["system"] and "8820 1177" in caller_call["system"]
    assert agent_call["system"].startswith("AGENT PROMPT") and "schedule_payment" in agent_call["system"]
    assert "You: Namaste" in agent_call["prompt"] and "Caller: Account 8820" in agent_call["prompt"]


def test_run_uses_the_runs_prompt_version(tmp_path):
    async def main():
        settings = sim_settings(tmp_path)
        llm = ScriptedLLM()
        store = Store(settings.database_url)
        for p in PERSONAS.values():
            store.upsert_persona(p)
        orch = Orchestrator(store, SimAdapter(settings, llm=llm), settings, EventBus(), PERSONAS)
        orch.on_call_finished.append(Scorer(store, EventBus(), settings, PERSONAS, HeuristicJudge()).score_call)
        run = orch.start_run(["corrector"], 1, "v2")
        await wait_run(store, run["id"])
        call = store.list_calls(run["id"])[0]
        assert call["status"] == "completed" and call["bolna_call_id"].startswith("sim-")
        assert call["transcript"][1]["speaker"] == "caller" and call["tool_payload"]["amount"] == 50000
        assert call["first_response_ms"] is None and call["duration_s"] > 0
        assert any(c["system"].startswith("PATCHED PROMPT") for c in llm.calls)
        scores = {s["metric"]: s for s in store.scores_for_run(run["id"])}
        assert not scores["extraction_accuracy"]["passed"]  # 50,000 instead of 55,000
        assert scores["latency"]["passed"] and "not counted" in scores["latency"]["reason"]
        await orch.shutdown()

    asyncio.run(main())


def test_llm_failure_fails_only_that_call(tmp_path):
    async def main():
        settings = sim_settings(tmp_path)
        store = Store(settings.database_url)
        for p in PERSONAS.values():
            store.upsert_persona(p)
        orch = Orchestrator(store, SimAdapter(settings, llm=ScriptedLLM(fail=True)), settings, EventBus(), PERSONAS)
        run = orch.start_run(["noisy"], 2)
        await wait_run(store, run["id"])
        calls = store.list_calls(run["id"])
        assert all(c["status"] == "failed" and "simulation failed" in c["error"] for c in calls)
        await orch.shutdown()

    asyncio.run(main())


def test_make_adapter_picks_sim(tmp_path):
    from backend.bolna_adapter import make_adapter
    assert isinstance(make_adapter(make_settings(tmp_path, bolna_mode="sim", gemini_api_key="x")), SimAdapter)


def test_gemini_retries_rate_limits(tmp_path, monkeypatch):
    slept = []

    async def no_sleep(s):
        slept.append(s)

    monkeypatch.setattr(gemini.asyncio, "sleep", no_sleep)

    class RateLimited(Exception):
        code = 429  # a per-minute limit: no per-day quota in its details, so it is retried

    class Models:
        n = 0

        async def generate_content(self, **kw):
            Models.n += 1
            if Models.n < 3:
                raise RateLimited("slow down")
            return SimpleNamespace(text=json.dumps({"ok": True}))

    llm = GeminiJSON(make_settings(tmp_path), client=SimpleNamespace(aio=SimpleNamespace(models=Models())))
    assert asyncio.run(llm.ask(model="m", system="s", prompt="p", schema={})) == {"ok": True}
    assert slept == [2, 4]

    class BadRequest(Exception):
        code = 400

    class Broken:
        async def generate_content(self, **kw):
            raise BadRequest("bad schema")

    llm = GeminiJSON(make_settings(tmp_path), client=SimpleNamespace(aio=SimpleNamespace(models=Broken())))
    try:
        asyncio.run(llm.ask(model="m", system="s", prompt="p", schema={}))
    except BadRequest:
        pass
    else:
        raise AssertionError("400s must not be retried")
