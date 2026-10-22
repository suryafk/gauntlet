"""Gemini judge and patcher, with the google-genai client faked."""

import asyncio
import json
from types import SimpleNamespace

from backend import mock_data
from backend.config import ROOT, pick_llm
from backend.events import EventBus
from backend.gemini import GeminiJSON, GeminiQuotaExhausted
from backend.models import Store
from backend.patcher import ClaudePatcher, GeminiPatcher, TemplatePatcher, make_patcher
from backend.personas import load_personas
from backend.scorer import JUDGE_SCHEMA, ClaudeJudge, GeminiJudge, HeuristicJudge, Scorer, make_judge

from .test_orchestrator import make_settings

PERSONAS = load_personas(ROOT / "personas")


class FakeModels:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.kwargs = text, error, None

    async def generate_content(self, **kw):
        self.kwargs = kw
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.text)


def fake_client(models):
    return SimpleNamespace(aio=SimpleNamespace(models=models))


def corrector_call():
    persona = PERSONAS["corrector"]
    transcript = [{"speaker": s, "text": t} for s, t in mock_data.script_for("corrector", "fail")]
    return {"id": "c", "run_id": "r", "persona_id": "corrector", "status": "completed",
            "transcript": transcript, "tool_payload": mock_data.payload_for(persona, "fail"),
            "first_response_ms": 900.0}


def test_provider_selection(tmp_path):
    s = make_settings(tmp_path, judge_mode="auto", llm_provider="gemini", gemini_api_key="", anthropic_api_key="")
    assert pick_llm(s) is None and isinstance(make_judge(s), HeuristicJudge)
    assert isinstance(make_patcher(s), TemplatePatcher)

    s.gemini_api_key = "g"
    assert pick_llm(s) == "gemini"
    assert isinstance(make_judge(s), GeminiJudge) and isinstance(make_patcher(s), GeminiPatcher)

    s.llm_provider = "claude"  # claude chosen, but only a Gemini key: stay offline
    assert pick_llm(s) is None
    s.anthropic_api_key = "a"
    assert isinstance(make_judge(s), ClaudeJudge) and isinstance(make_patcher(s), ClaudePatcher)

    s.judge_mode = "heuristic"
    assert pick_llm(s) is None
    s.judge_mode = "gemini"
    assert pick_llm(s) == "gemini"


def test_gemini_judge_request_and_parse(tmp_path):
    out = {
        "task_completion": {"passed": True, "reason": "Scheduled.", "quote": ""},
        "interruption_recovery": {"passed": False, "reason": "Kept 50,000.", "quote": "Okay, 50,000 rupees."},
        "policy_adherence": {"passed": False, "reason": "No read-back.", "quote": ""},
    }
    fake = FakeModels(json.dumps(out))
    judge = GeminiJudge(make_settings(tmp_path), client=fake_client(fake))
    verdicts = asyncio.run(judge.judge(corrector_call(), PERSONAS["corrector"]))
    assert verdicts["task_completion"].passed and not verdicts["interruption_recovery"].passed
    kw, config = fake.kwargs, fake.kwargs["config"]
    assert kw["model"] == "gemini-3.8-flash"
    assert config.response_json_schema == JUDGE_SCHEMA and config.response_mime_type == "application/json"
    assert config.thinking_config.thinking_level.value == "MEDIUM"
    assert "pachpan" in kw["contents"] and "PayEasy" in config.system_instruction


def test_gemini_patcher_uses_patch_model(tmp_path):
    out = {"summary": "s", "changes": [{"metric": "policy_adherence", "change": "c"}], "revised_prompt": "new"}
    fake = FakeModels(json.dumps(out))
    patcher = GeminiPatcher(make_settings(tmp_path), client=fake_client(fake))
    cluster = {"persona": "Prompt injector", "metric": "policy_adherence", "failed": 3, "calls": 5,
               "reasons": ["waived"], "examples": ["I've waived your late fee."]}
    assert asyncio.run(patcher.draft("old prompt", [cluster])) == out
    assert fake.kwargs["model"] == "gemini-4-argon"
    assert fake.kwargs["config"].thinking_config.thinking_level.value == "HIGH"
    assert "<current_prompt>\nold prompt" in fake.kwargs["contents"]


def test_empty_reply_raises(tmp_path):
    llm = GeminiJSON(make_settings(tmp_path), client=fake_client(FakeModels("")))
    try:
        asyncio.run(llm.ask(model="m", system="s", prompt="p", schema={}))
    except Exception as exc:
        assert "empty response" in str(exc)
    else:
        raise AssertionError("expected an error")


class QuotaError(Exception):
    code = 429

    def __init__(self, quota_id, retry_delay):
        super().__init__("RESOURCE_EXHAUSTED")
        self.details = {"error": {"details": [
            {"violations": [{"quotaId": quota_id}]},
            {"retryDelay": retry_delay},
        ]}}


def test_daily_quota_fails_fast_without_retrying(tmp_path):
    calls = []

    class Models:
        async def generate_content(self, **kw):
            calls.append(kw)
            raise QuotaError("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "68193s")

    llm = GeminiJSON(make_settings(tmp_path), client=fake_client(Models()))
    try:
        asyncio.run(llm.ask(model="gemini-3.8-flash", system="s", prompt="p", schema={}))
    except GeminiQuotaExhausted as exc:
        assert "gemini-3.8-flash" in str(exc)
    else:
        raise AssertionError("expected GeminiQuotaExhausted")
    assert len(calls) == 1


def test_gemini_failure_falls_back_to_heuristic(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database_url)
    store.upsert_persona(PERSONAS["corrector"])
    run = store.create_run(50, "v1")
    row = store.create_call(run["id"], "corrector")
    call = {**corrector_call(), "id": row["id"], "run_id": run["id"]}

    judge = GeminiJudge(settings, client=fake_client(FakeModels(error=RuntimeError("quota exhausted"))))
    verdicts = asyncio.run(Scorer(store, EventBus(), settings, PERSONAS, judge).score_call(call))
    assert verdicts["policy_adherence"].reason.startswith("[heuristic fallback]")
    judges = {s["metric"]: s["judge"] for s in store.scores_for_run(run["id"])}
    assert judges["policy_adherence"] == "heuristic fallback" and judges["extraction_accuracy"] == "code"
