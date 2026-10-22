"""The DEMO.md run of show, replayed headlessly: with MOCK_SEED=672 the numbers it promises
must hold, whatever the concurrency."""

import shutil
import time

import pytest
from fastapi.testclient import TestClient

from backend.bolna_agents import TARGET_PROMPTS_DIR
from backend.main import create_app

from .test_orchestrator import make_settings

ALL = ["code_switcher", "corrector", "injector", "interrupter", "noisy", "number_mumbler"]


def wait_completed(client, run_id, timeout=20):
    deadline = time.monotonic() + timeout
    while (s := client.get(f"/runs/{run_id}").json())["status"] != "completed":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return s


@pytest.mark.parametrize("concurrency", [2, 10])
def test_demo_seed_plays_out_as_documented(tmp_path, concurrency):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    shutil.copy(TARGET_PROMPTS_DIR / "v1.md", prompts / "v1.md")
    settings = make_settings(tmp_path, mock_seed="672", max_concurrency=concurrency, mock_dial_fail_rate=0.05,
                             target_prompts_dir=prompts)
    with TestClient(create_app(settings)) as client:
        base = client.post("/runs", json={"persona_ids": ALL, "calls_per_persona": 5}).json()
        s = wait_completed(client, base["id"])
        assert s["pass_rate"] == pytest.approx(0.2) and s["failed_calls"] == 1
        assert s["persona_pass"]["corrector"] == {"passed": 0, "scored": 5}

        failed = [c for c in client.get(f"/runs/{base['id']}/calls").json() if c["status"] == "failed"]
        assert [c["persona_id"] for c in failed] == ["corrector"]
        client.post(f"/calls/{failed[0]['id']}/retry")
        s = wait_completed(client, base["id"])
        assert s["failed_calls"] == 0 and s["pass_rate"] == pytest.approx(0.2)  # connects, still fails

        patch = client.post(f"/runs/{base['id']}/patch").json()
        assert len(patch["failure_cluster"]) == 13 and len(patch["changes"]) == 7
        rerun = client.post(f"/runs/{base['id']}/rerun", json={"patch_id": patch["id"]}).json()
        after = wait_completed(client, rerun["id"])
        assert after["target_prompt_version"] == "v2" and after["pass_rate"] == pytest.approx(0.9)

        cmp = client.get(f"/compare?a={base['id']}&b={rerun['id']}").json()
        assert all(p["b"] > p["a"] for p in cmp["personas"])
