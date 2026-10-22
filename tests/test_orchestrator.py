import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from backend.bolna_adapter import DialError, MockBolnaAdapter, parse_transcript
from backend.config import ROOT, Settings
from backend.events import EventBus
from backend.main import create_app
from backend.models import Store
from backend.orchestrator import Orchestrator, RunRejected
from backend.personas import load_personas


def make_settings(tmp_path, **overrides) -> Settings:
    s = Settings()
    s.bolna_mode = "mock"
    s.database_url = str(tmp_path / "test.db")
    s.personas_dir = ROOT / "personas"
    s.target_number = "+910000000000"
    s.run_call_cap = 50
    s.max_concurrency = 3
    s.call_timeout_s = 5
    s.webhook_secret = "s3cret"
    s.mock_call_min_s = 0.05
    s.mock_call_max_s = 0.15
    s.mock_dial_fail_rate = 0.0
    s.judge_mode = "heuristic"  # never call Claude from tests
    s.seed_on_empty = False
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def make_orch(tmp_path, adapter=None, **overrides):
    settings = make_settings(tmp_path, **overrides)
    store = Store(settings.database_url)
    personas = load_personas(settings.personas_dir)
    for p in personas.values():
        store.upsert_persona(p)
    adapter = adapter or MockBolnaAdapter(settings, seed=1)
    return Orchestrator(store, adapter, settings, EventBus(), personas), store


async def wait_run(store: Store, run_id: str, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = store.get_run(run_id)
        if run["status"] == "completed":
            return run
        await asyncio.sleep(0.02)
    raise AssertionError("run did not complete")


def test_personas_load():
    personas = load_personas(ROOT / "personas")
    assert len(personas) == 6
    assert {"interrupter", "code_switcher", "corrector", "noisy", "injector", "number_mumbler"} == set(personas)


def test_run_completes_all_calls(tmp_path):
    async def main():
        orch, store = make_orch(tmp_path)
        run = orch.start_run(["interrupter", "corrector"], 3)
        await wait_run(store, run["id"])
        calls = store.list_calls(run["id"])
        assert len(calls) == 6
        assert all(c["status"] == "completed" for c in calls)
        assert all(c["transcript"] and c["bolna_call_id"] for c in calls)
        await orch.shutdown()

    asyncio.run(main())


def test_call_cap_is_enforced(tmp_path):
    orch, _ = make_orch(tmp_path, run_call_cap=10)
    with pytest.raises(RunRejected):
        orch.start_run(["interrupter", "corrector", "noisy"], 4)  # 12 > 10


def test_semaphore_limits_concurrency(tmp_path):
    class CountingAdapter(MockBolnaAdapter):
        active = peak = 0

        async def start_call(self, **kw):
            CountingAdapter.active += 1
            CountingAdapter.peak = max(CountingAdapter.peak, CountingAdapter.active)
            return await super().start_call(**kw)

        async def _simulate(self, *args):
            await super()._simulate(*args)  # the terminal webhook frees the slot right here
            CountingAdapter.active -= 1

    async def main():
        settings = make_settings(tmp_path, max_concurrency=2)
        adapter = CountingAdapter(settings, seed=2)
        orch, store = make_orch(tmp_path, adapter=adapter, max_concurrency=2)
        run = orch.start_run(["interrupter", "noisy", "injector"], 3)
        await wait_run(store, run["id"])
        assert CountingAdapter.peak == 2
        await orch.shutdown()

    asyncio.run(main())


def test_failed_calls_do_not_fail_the_run(tmp_path):
    class FlakyAdapter(MockBolnaAdapter):
        n = 0

        async def start_call(self, **kw):
            FlakyAdapter.n += 1
            if FlakyAdapter.n % 2 == 0:
                raise DialError("boom")
            return await super().start_call(**kw)

    async def main():
        settings = make_settings(tmp_path)
        orch, store = make_orch(tmp_path, adapter=FlakyAdapter(settings, seed=3))
        run = orch.start_run(["interrupter", "corrector"], 2)
        await wait_run(store, run["id"])
        statuses = sorted(c["status"] for c in store.list_calls(run["id"]))
        assert statuses == ["completed", "completed", "failed", "failed"]
        await orch.shutdown()

    asyncio.run(main())


def test_missing_webhook_times_out_as_failed(tmp_path):
    class SilentAdapter(MockBolnaAdapter):
        async def start_call(self, **kw):
            return f"never-calls-back-{kw['call_id']}"

    async def main():
        settings = make_settings(tmp_path, call_timeout_s=0.2)
        orch, store = make_orch(tmp_path, adapter=SilentAdapter(settings), call_timeout_s=0.2)
        run = orch.start_run(["noisy"], 2)
        await wait_run(store, run["id"])
        calls = store.list_calls(run["id"])
        assert all(c["status"] == "failed" and "no terminal webhook" in c["error"] for c in calls)
        await orch.shutdown()

    asyncio.run(main())


def test_adapter_refuses_other_numbers(tmp_path):
    settings = make_settings(tmp_path)
    adapter = MockBolnaAdapter(settings)
    persona = load_personas(ROOT / "personas")["noisy"]
    with pytest.raises(DialError):
        asyncio.run(adapter.start_call(call_id="x", persona=persona, to_number="+919999999999",
                                       prompt_version="v1"))


def test_webhook_is_idempotent(tmp_path):
    async def main():
        orch, store = make_orch(tmp_path)
        run = orch.start_run(["corrector"], 1)
        await wait_run(store, run["id"])
        call = store.list_calls(run["id"])[0]
        finished = []

        async def on_done(c):
            finished.append(c["id"])

        orch.on_call_finished.append(on_done)
        replay = {"id": call["bolna_call_id"], "status": "completed", "transcript": "user: different"}
        first = await orch.handle_webhook(replay)
        second = await orch.handle_webhook(replay)
        assert first["duplicate"] and second["duplicate"]
        assert finished == []
        assert store.get_call(call["id"])["transcript"] == call["transcript"]
        await orch.shutdown()

    asyncio.run(main())


def test_parse_transcript_maps_speakers():
    turns = parse_transcript("assistant: Hello\nuser: Hi there\ncontinued line")
    assert turns == [{"speaker": "caller", "text": "Hello"}, {"speaker": "agent", "text": "Hi there continued line"}]


def test_api_end_to_end(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "mock"}
        assert client.post("/runs", json={"persona_ids": ["noisy"], "calls_per_persona": 99}).status_code == 400

        run = client.post("/runs", json={"persona_ids": ["noisy", "injector"], "calls_per_persona": 2}).json()
        deadline = time.monotonic() + 10
        while client.get(f"/runs/{run['id']}").json()["status"] != "completed":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        summary = client.get(f"/runs/{run['id']}").json()
        assert summary["total_calls"] == 4
        assert summary["status_counts"] == {"completed": 4}

        calls = client.get(f"/runs/{run['id']}/calls").json()
        assert len(calls) == 4 and calls[0]["transcript"][0]["speaker"] == "agent"

        assert client.post("/webhooks/bolna", json={"id": "x"}).status_code == 401
        resp = client.post("/webhooks/bolna", json={"id": calls[0]["bolna_call_id"], "status": "completed"},
                           headers={"X-Webhook-Secret": "s3cret"})
        assert resp.json()["duplicate"] is True
