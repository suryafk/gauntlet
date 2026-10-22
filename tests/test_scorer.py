import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import mock_data
from backend.config import ROOT
from backend.events import EventBus
from backend.main import create_app
from backend.models import Store
from backend.personas import load_personas
from backend.scorer import (
    METRICS,
    ClaudeJudge,
    HeuristicJudge,
    Scorer,
    check_extraction,
    check_latency,
    find_span,
    run_summary,
)

from .test_orchestrator import make_settings

PERSONAS = load_personas(ROOT / "personas")

EXPECTED_FAILURES = mock_data.DESIGNED_FAILURES


def mock_call(pid: str, outcome: str, **kw) -> dict:
    persona = PERSONAS[pid]
    transcript = [{"speaker": s, "text": t} for s, t in mock_data.script_for(pid, outcome)]
    return {"id": f"{pid}-{outcome}", "run_id": "r", "persona_id": pid, "status": "completed",
            "transcript": transcript, "tool_payload": mock_data.payload_for(persona, outcome),
            "first_response_ms": 800.0, **kw}


def saved(store: Store, call: dict) -> dict:
    """Give a hand-built call real run/call rows so scores can reference them."""
    store.upsert_persona(PERSONAS[call["persona_id"]])
    run = store.create_run(50, "v1")
    row = store.create_call(run["id"], call["persona_id"])
    return {**call, "id": row["id"], "run_id": run["id"]}


async def all_verdicts(call: dict) -> dict:
    persona = PERSONAS[call["persona_id"]]
    v = await HeuristicJudge().judge(call, persona)
    v["extraction_accuracy"] = check_extraction(call, persona)
    v["latency"] = check_latency(call, persona)
    return v


@pytest.mark.parametrize("pid", list(PERSONAS))
def test_pass_scripts_pass_every_metric(pid):
    verdicts = asyncio.run(all_verdicts(mock_call(pid, "pass")))
    assert {m for m, v in verdicts.items() if not v.passed} == set(), {m: v.reason for m, v in verdicts.items()}


@pytest.mark.parametrize("pid", list(PERSONAS))
def test_fail_scripts_fail_their_flaw(pid):
    verdicts = asyncio.run(all_verdicts(mock_call(pid, "fail")))
    assert {m for m, v in verdicts.items() if not v.passed} == EXPECTED_FAILURES[pid]


def test_extraction_flags_wrong_amount_with_span():
    call = mock_call("corrector", "fail")
    v = check_extraction(call, PERSONAS["corrector"])
    assert not v.passed and "expected 55000, got 50000" in v.reason
    span = find_span(call["transcript"], v.quote)
    assert span and "50,000" in call["transcript"][span["turn"]]["text"]


def test_latency_limit():
    persona = PERSONAS["noisy"]
    assert not check_latency({"first_response_ms": 2100.0}, persona).passed
    assert check_latency({"first_response_ms": 900.0}, persona).passed
    assert check_latency({"first_response_ms": None}, persona).passed


class FakeMessages:
    def __init__(self, text=None, stop_reason="end_turn"):
        self.text, self.stop_reason, self.kwargs = text, stop_reason, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        content = [SimpleNamespace(type="text", text=self.text)] if self.text else []
        return SimpleNamespace(content=content, stop_reason=self.stop_reason)


def fake_client(messages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def test_claude_judge_request_and_parse(tmp_path):
    out = {
        "task_completion": {"passed": True, "reason": "Scheduled.", "quote": ""},
        "interruption_recovery": {"passed": False, "reason": "Kept 50,000.", "quote": "Okay, 50,000 rupees."},
        "policy_adherence": {"passed": False, "reason": "No read-back.", "quote": ""},
    }
    messages = FakeMessages(json.dumps(out))
    judge = ClaudeJudge(make_settings(tmp_path), client=fake_client(messages))
    verdicts = asyncio.run(judge.judge(mock_call("corrector", "fail"), PERSONAS["corrector"]))
    assert verdicts["task_completion"].passed and not verdicts["interruption_recovery"].passed
    kw = messages.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default"
    assert "pachpan" in kw["messages"][0]["content"]


def test_judge_failure_falls_back_to_heuristic(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database_url)
    judge = ClaudeJudge(settings, client=fake_client(FakeMessages(None, stop_reason="refusal")))
    scorer = Scorer(store, EventBus(), settings, PERSONAS, judge)
    verdicts = asyncio.run(scorer.score_call(saved(store, mock_call("injector", "fail"))))
    assert not verdicts["policy_adherence"].passed
    assert verdicts["policy_adherence"].reason.startswith("[heuristic fallback]")


def test_failed_call_fails_every_metric(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database_url)
    scorer = Scorer(store, EventBus(), settings, PERSONAS, HeuristicJudge())
    call = saved(store, {"persona_id": "noisy", "status": "failed", "error": "busy", "transcript": None})
    verdicts = asyncio.run(scorer.score_call(call))
    assert all(not v.passed and "busy" in v.reason for v in verdicts.values())


def test_api_run_is_scored_and_summarised(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/config").json()["judge"] == "heuristic"
        run = client.post("/runs", json={"persona_ids": ["corrector", "injector"], "calls_per_persona": 3}).json()
        deadline = time.monotonic() + 10
        while (summary := client.get(f"/runs/{run['id']}").json())["status"] != "completed":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert summary["scored_calls"] == 6 and summary["done_calls"] == 6
        assert 0 <= summary["pass_rate"] <= 1
        assert set(summary["heatmap"]) == {"corrector", "injector"}
        cell = summary["heatmap"]["corrector"]["extraction_accuracy"]
        assert cell["scored"] == 3 and cell["rate"] == cell["passed"] / 3
        calls = client.get(f"/runs/{run['id']}/calls").json()
        assert all(len(c["scores"]) == len(METRICS) for c in calls)
        assert client.get("/").status_code == 200  # dashboard is served


def test_summary_before_scoring(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.upsert_persona(PERSONAS["noisy"])
    run = store.create_run(50, "v1")
    store.create_call(run["id"], "noisy")
    s = run_summary(store, run["id"])
    assert s["pass_rate"] is None and s["heatmap"]["noisy"]["task_completion"]["rate"] is None
