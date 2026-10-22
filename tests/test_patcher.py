import asyncio
import json
import shutil
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.bolna_agents import TARGET_PROMPTS_DIR, target_prompt
from backend.main import create_app
from backend.patcher import ClaudePatcher, TemplatePatcher, next_version, unified_diff

from .test_orchestrator import make_settings


def prompt_settings(tmp_path, **kw):
    """Settings with a private copy of the prompt versions, so tests never write into agents/."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    shutil.copy(TARGET_PROMPTS_DIR / "v1.md", prompts / "v1.md")
    return make_settings(tmp_path, target_prompts_dir=prompts, **kw)


def cluster(metric, persona="Mid-sentence corrector"):
    return {"persona_id": "corrector", "persona": persona, "metric": metric, "metric_label": metric,
            "failed": 3, "calls": 4, "reasons": ["r"], "examples": ["Okay, 50,000 rupees."]}


def test_template_patch_replaces_flawed_lines():
    v1 = target_prompt("v1")
    out = asyncio.run(TemplatePatcher().draft(v1, [cluster("policy_adherence")]))
    revised = out["revised_prompt"]
    assert "- Waiver rule:" not in revised and "Never waive fees" in revised
    assert "- Speed rule:" not in revised and "read back the account number, amount and date" in revised
    # only policy was failing, so the other flawed rules are left alone
    for kept in ("- Script rule:", "- Amount rule:", "- Account number rule:", "- Date rule:", "- Clarity rule:"):
        assert kept in revised
    assert {c["metric"] for c in out["changes"]} == {"policy_adherence"} and len(out["changes"]) == 2
    diff = unified_diff(v1, revised, "v1.md", "patched.md")
    assert "-- Waiver rule:" in diff and "+- Never waive fees" in diff


def test_template_patch_fixes_every_v1_flaw():
    v1 = target_prompt("v1")
    out = asyncio.run(TemplatePatcher().draft(v1, [cluster(m) for m in (
        "extraction_accuracy", "policy_adherence", "interruption_recovery", "task_completion")]))
    revised = out["revised_prompt"]
    assert len(out["changes"]) == 7
    for flaw in ("Script rule:", "Amount rule:", "Account number rule:", "Date rule:", "Waiver rule:",
                 "Clarity rule:", "Speed rule:"):
        assert flaw not in revised


def test_template_patch_on_patched_prompt_is_a_no_op():
    first = asyncio.run(TemplatePatcher().draft(target_prompt("v1"), [cluster(m) for m in (
        "extraction_accuracy", "policy_adherence", "interruption_recovery", "task_completion")]))
    again = asyncio.run(TemplatePatcher().draft(first["revised_prompt"], [cluster("policy_adherence")]))
    assert again["revised_prompt"] == first["revised_prompt"] and again["changes"] == []


def test_next_version(tmp_path):
    assert next_version(tmp_path) == "v1"
    (tmp_path / "v1.md").write_text("x")
    (tmp_path / "v3.md").write_text("x")
    (tmp_path / "notes.md").write_text("x")
    assert next_version(tmp_path) == "v4"


def test_claude_patcher_request(tmp_path):
    out = {"summary": "s", "changes": [{"metric": "policy_adherence", "change": "c"}], "revised_prompt": "new"}

    class Messages:
        kwargs = None

        async def create(self, **kw):
            Messages.kwargs = kw
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))])

    patcher = ClaudePatcher(make_settings(tmp_path), client=SimpleNamespace(beta=SimpleNamespace(messages=Messages())))
    result = asyncio.run(patcher.draft("old prompt", [cluster("policy_adherence")]))
    assert result == out
    assert Messages.kwargs["model"] == "claude-opus-5-5"
    assert Messages.kwargs["output_config"]["format"]["schema"]["required"] == ["summary", "changes", "revised_prompt"]
    assert "old prompt" in Messages.kwargs["messages"][0]["content"]


def wait_completed(client, run_id, timeout=10):
    deadline = time.monotonic() + timeout
    while (s := client.get(f"/runs/{run_id}").json())["status"] != "completed":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return s


def test_patch_rerun_compare_loop(tmp_path):
    settings = prompt_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/config").json()["patcher"] == "rules"
        assert client.post("/runs", json={"persona_ids": ["noisy"], "calls_per_persona": 1,
                                          "prompt_version": "../etc"}).status_code == 400

        base = client.post("/runs", json={"persona_ids": ["corrector", "injector", "number_mumbler"],
                                          "calls_per_persona": 4}).json()
        assert client.post(f"/runs/{base['id']}/patch").status_code in (201, 409)  # may still be running
        summary = wait_completed(client, base["id"])
        if summary["failing_calls"] == 0:
            return  # vanishingly unlikely with v1's 70% failure rate; nothing to patch

        patch = client.post(f"/runs/{base['id']}/patch").json()
        assert patch["base_version"] == "v1" and patch["source"] == "rules"
        assert patch["failure_cluster"] and patch["proposed_diff"].startswith("--- v1.md")
        assert client.get(f"/runs/{base['id']}/patches").json()[0]["id"] == patch["id"]

        rerun = client.post(f"/runs/{base['id']}/rerun", json={"patch_id": patch["id"]}).json()
        assert rerun["parent_run_id"] == base["id"] and rerun["target_prompt_version"] == "v2"
        assert (settings.target_prompts_dir / "v2.md").read_text() == patch["new_prompt"]
        # applying twice returns the same re-run instead of starting another
        assert client.post(f"/runs/{base['id']}/rerun", json={"patch_id": patch["id"]}).json()["id"] == rerun["id"]

        after = wait_completed(client, rerun["id"])
        assert after["total_calls"] == summary["total_calls"]

        cmp = client.get(f"/compare?a={base['id']}&b={rerun['id']}").json()
        assert cmp["a"]["pass_rate"] == summary["pass_rate"] and cmp["b"]["pass_rate"] == after["pass_rate"]
        assert {p["persona_id"] for p in cmp["personas"]} == {"corrector", "injector", "number_mumbler"}
        assert len(cmp["metrics"]) == 5 and cmp["patch"]["id"] == patch["id"]


def test_patch_rejects_run_without_failures(tmp_path):
    app = create_app(prompt_settings(tmp_path))
    with TestClient(app) as client:
        store = app.state.store
        run = store.create_run(50, "v1")
        store.set_run_status(run["id"], "completed")
        resp = client.post(f"/runs/{run['id']}/patch")
        assert resp.status_code == 400 and "no prompt-fixable failures" in resp.json()["detail"]
