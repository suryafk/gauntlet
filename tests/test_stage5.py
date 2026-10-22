"""Call retries, example-run seeding and the hand spot-check."""

import asyncio
import json
import time

from fastapi.testclient import TestClient

from backend.bolna_adapter import DialError, MockBolnaAdapter
from backend.config import ROOT
from backend.main import create_app
from backend.models import Store
from backend.seed import export_bundle, import_bundle, seed_if_empty, write_bundle
from backend.spotcheck import agreement, sample_calls

from .test_orchestrator import make_orch, make_settings, wait_run


def wait_completed(client, run_id, timeout=10):
    deadline = time.monotonic() + timeout
    while (s := client.get(f"/runs/{run_id}").json())["status"] != "completed":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return s


class FailFirstAdapter(MockBolnaAdapter):
    """Every call's first dial fails; later attempts go through."""

    seen: set = set()

    async def start_call(self, **kw):
        if kw["call_id"] not in self.seen:
            self.seen.add(kw["call_id"])
            raise DialError("busy")
        return await super().start_call(**kw)


def test_retry_failed_call_in_same_run(tmp_path):
    async def main():
        settings = make_settings(tmp_path)
        FailFirstAdapter.seen = set()
        orch, store = make_orch(tmp_path, adapter=FailFirstAdapter(settings, seed=5))
        from backend.events import EventBus
        from backend.scorer import HeuristicJudge, Scorer
        orch.on_call_finished.append(Scorer(store, EventBus(), settings, orch.personas, HeuristicJudge()).score_call)

        run = orch.start_run(["corrector"], 2)
        await wait_run(store, run["id"])
        calls = store.list_calls(run["id"])
        assert all(c["status"] == "failed" for c in calls)
        assert len(store.scores_for_run(run["id"])) == 10  # failed calls are scored as fails

        retried = orch.retry_call(calls[0]["id"])
        assert retried["status"] == "queued" and retried["attempts"] == 2
        assert store.get_run(run["id"])["status"] == "running"
        await wait_run(store, run["id"])

        call = store.get_call(calls[0]["id"])
        assert call["status"] == "completed" and call["transcript"]
        scores = [s for s in store.scores_for_run(run["id"]) if s["call_id"] == call["id"]]
        assert len(scores) == 5 and all("did not complete" not in s["reason"] for s in scores)
        await orch.shutdown()

    asyncio.run(main())


def test_retry_rules(tmp_path):
    app = create_app(make_settings(tmp_path, mock_dial_fail_rate=1.0))
    with TestClient(app) as client:
        run = client.post("/runs", json={"persona_ids": ["noisy"], "calls_per_persona": 2}).json()
        wait_completed(client, run["id"])
        call_id = client.get(f"/runs/{run['id']}/calls").json()[0]["id"]
        for attempt in (2, 3):
            assert client.post(f"/calls/{call_id}/retry").json()["attempts"] == attempt
            wait_completed(client, run["id"])
        resp = client.post(f"/calls/{call_id}/retry")
        assert resp.status_code == 400 and "3 times" in resp.json()["detail"]

        out = client.post(f"/runs/{run['id']}/retry-failed").json()
        assert len(out["retried"]) == 1 and out["skipped"][0]["call_id"] == call_id
        wait_completed(client, run["id"])


def test_cannot_retry_completed_call(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        run = client.post("/runs", json={"persona_ids": ["injector"], "calls_per_persona": 1}).json()
        wait_completed(client, run["id"])
        call_id = client.get(f"/runs/{run['id']}/calls").json()[0]["id"]
        assert client.post(f"/calls/{call_id}/retry").status_code == 400


def test_seed_roundtrip(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        run = client.post("/runs", json={"persona_ids": ["corrector", "noisy"], "calls_per_persona": 2}).json()
        wait_completed(client, run["id"])
        bundle = export_bundle(app.state.store, run["id"])
    path = tmp_path / "seed.json"
    write_bundle(bundle, path)

    fresh = Store(str(tmp_path / "fresh.db"))
    bare = Store(str(tmp_path / "bare.db"))  # no personas: the import must fail cleanly
    assert seed_if_empty(bare, path) == 0 and bare.list_runs() == []
    for p in app.state.personas.values():
        fresh.upsert_persona(p)
    assert seed_if_empty(fresh, path) == 1
    assert seed_if_empty(fresh, path) == 0  # only into an empty database
    runs = fresh.list_runs()
    assert runs[0]["id"] == run["id"] and runs[0]["seeded"] == 1
    assert len(fresh.list_calls(run["id"])) == 4 and len(fresh.scores_for_run(run["id"])) == 20
    assert import_bundle(fresh, path) == 0  # importing twice adds nothing


def test_committed_seed_loads_on_empty_start(tmp_path):
    path = ROOT / "seed" / "example_run.json"
    bundle = json.loads(path.read_text())
    root = bundle["root_run_id"]
    app = create_app(make_settings(tmp_path, seed_on_empty=True, seed_path=path))
    with TestClient(app) as client:
        runs = client.get("/runs").json()
        assert {r["id"] for r in runs} == {r["id"] for r in bundle["tables"]["runs"]}
        summary = client.get(f"/runs/{root}").json()
        assert summary["status"] == "completed" and summary["seeded"] == 1 and summary["scored_calls"] == 30
        child = next(r for r in runs if r["parent_run_id"] == root)
        cmp = client.get(f"/compare?a={root}&b={child['id']}").json()
        assert cmp["patch"] and cmp["b"]["pass_rate"] is not None
        # patching an example re-run works without its prompt file on disk
        assert client.post(f"/runs/{child['id']}/patch").status_code in (201, 400)


def test_spotcheck_sample_and_agreement(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        run = client.post("/runs", json={"persona_ids": ["corrector", "injector", "noisy", "interrupter"],
                                         "calls_per_persona": 5}).json()
        wait_completed(client, run["id"])
        data = client.get(f"/runs/{run['id']}/spotcheck").json()
        items = data["items"]
        assert len(items) == 10 and data["metrics"] == ["task_completion", "interruption_recovery", "policy_adherence"]
        assert len({it["persona_id"] for it in items}) >= 3  # spread across personas
        assert items == client.get(f"/runs/{run['id']}/spotcheck").json()["items"]  # stable sample

        it = items[0]
        judge = {m: it["scores"][m]["passed"] for m in data["metrics"]}
        flipped = {**judge, "policy_adherence": not judge["policy_adherence"]}
        a = client.post(f"/calls/{it['call_id']}/review", json={"labels": flipped, "note": "read-back was fine"}).json()
        assert a["labels"] == 3 and a["reviewed_calls"] == 1 and abs(a["rate"] - 2 / 3) < 1e-9
        assert a["disagreements"][0]["metric"] == "policy_adherence"
        assert a["disagreements"][0]["note"] == "read-back was fine"
        assert a["judges"] == ["heuristic"]

        # relabelling overwrites rather than adding
        a = client.post(f"/calls/{it['call_id']}/review", json={"labels": judge}).json()
        assert a["labels"] == 3 and a["rate"] == 1.0
        assert client.get(f"/runs/{run['id']}/spotcheck").json()["items"][0]["review"] == judge

        assert client.post(f"/calls/{it['call_id']}/review", json={"labels": {"latency": True}}).status_code == 400


def test_sample_mixes_judged_pass_and_fail(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.database_url)
    store.upsert_persona(json.loads((ROOT / "personas" / "noisy.json").read_text()))
    run = store.create_run(50, "v1")
    for i in range(12):
        c = store.create_call(run["id"], "noisy")
        store.update_call(c["id"], status="completed")
        for m in ("task_completion", "interruption_recovery", "policy_adherence"):
            store.save_score(c["id"], m, None, passed=(i % 3 != 0), judge="heuristic")
    picked = sample_calls(store, run["id"], 6)
    failing = [c for c in picked
               if not all(s["passed"] for s in store.scores_for_run(run["id"]) if s["call_id"] == c["id"])]
    assert len(picked) == 6 and len(failing) == 3  # alternates fail/pass while both last
    assert agreement(store, run["id"])["rate"] is None
