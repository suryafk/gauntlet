"""Captures real screenshots and run data from the running Gauntlet app for the pitch deck.

Starts its own server (mock mode, MOCK_SEED=672, throwaway database and prompts), runs the
flagship scenario, patches, re-runs, and saves 2x screenshots to shots/ plus the real numbers
the slides quote to shots/data.json.

  ../.venv/bin/python capture.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SHOTS = HERE / "shots"
PORT = 8791
BASE = f"http://localhost:{PORT}"


def start_server(tmp: Path) -> subprocess.Popen:
    prompts = tmp / "prompts"
    prompts.mkdir()
    shutil.copy(ROOT / "agents" / "target_prompts" / "v1.md", prompts / "v1.md")
    env = {**os.environ, "BOLNA_MODE": "mock", "JUDGE_MODE": "heuristic", "MOCK_SEED": "672",
           "MAX_CONCURRENCY": "10", "MOCK_DIAL_FAIL_RATE": "0.05", "DATABASE_URL": str(tmp / "deck.db"),
           "TARGET_PROMPTS_DIR": str(prompts), "GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": ""}
    proc = subprocess.Popen([str(ROOT / ".venv" / "bin" / "uvicorn"), "backend.main:app", "--port", str(PORT)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if httpx.get(f"{BASE}/health", timeout=0.5).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    proc.kill()
    sys.exit("server did not start")


def wait_new_run(page, previous: str | None) -> str:
    """Waits until the page shows a different run than `previous`, and that run has finished.
    The status chip can briefly show the previous run's state, so completion comes from the API."""
    page.wait_for_function("id => { const v = document.querySelector('#run-select')?.value; return v && v !== id; }",
                           arg=previous or "", timeout=30000)
    run_id = page.eval_on_selector("#run-select", "s => s.value")
    for _ in range(600):
        if httpx.get(f"{BASE}/runs/{run_id}").json()["status"] == "completed":
            break
        time.sleep(0.1)
    page.wait_for_function("() => document.querySelector('#run-status')?.textContent === 'completed'")
    page.wait_for_timeout(1200)
    return run_id


def main() -> None:
    SHOTS.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    server = start_server(tmp)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=2,
                                    color_scheme="dark")
            shot = lambda name, **kw: page.screenshot(path=str(SHOTS / f"{name}.png"), **kw)  # noqa: E731
            el = lambda sel, name: page.locator(sel).first.screenshot(path=str(SHOTS / f"{name}.png"))  # noqa: E731

            page.goto(BASE + "/")
            page.wait_for_selector("#ov-runs tr[data-run-id]")
            page.wait_for_timeout(500)
            shot("overview")

            example_id = page.evaluate("() => state.runs.find(r => r.status === 'completed')?.id")
            page.click("#flagship")
            base_id = wait_new_run(page, example_id)
            page.evaluate("window.scrollTo(0, 0)")
            shot("run")
            el("#stages", "stages")
            el("#heatmap", "heatmap")

            page.click('#heatmap .cell[data-p="corrector"][data-m="extraction_accuracy"]')
            page.wait_for_timeout(500)
            # the cell opens the first failing call, which may be the one that never connected;
            # pick a completed call that failed on extraction (the "50,000" call)
            call_id = httpx.get(f"{BASE}/runs/{base_id}/calls").json()
            call_id = next(c["id"] for c in call_id if c["persona_id"] == "corrector" and c["status"] == "completed"
                           and any(s["metric"] == "extraction_accuracy" and not s["passed"] for s in c["scores"]))
            page.click(f'#calls tr[data-id="{call_id}"]')
            page.wait_for_timeout(500)
            page.locator("#transcript").scroll_into_view_if_needed()
            el("#transcript", "transcript")

            page.click("#patch")
            page.wait_for_selector("#apply")
            page.wait_for_timeout(400)
            el("#patch-body", "patch")
            page.click("#apply")
            rerun_id = wait_new_run(page, base_id)

            page.evaluate("window.scrollTo(0, 0)")
            page.click("#run-lineage a[data-compare]")
            page.wait_for_selector("#cmp-body .big")
            page.wait_for_timeout(500)
            shot("compare")
            el("#cmp-body .card", "compare_headline")
            el("#cmp-body .split", "compare_bars")

            page.goto(BASE + "/evidence.html")
            page.wait_for_timeout(500)
            el("main .card:nth-of-type(2)", "evidence_call")
            el("main .card:nth-of-type(3)", "evidence_scores")

            page.goto(BASE + "/")
            page.click("nav button[data-view=overview]")  # the app reopens the last tab it showed
            page.wait_for_selector("#ov-runs tr[data-run-id]")
            page.wait_for_timeout(500)
            shot("overview_after")
            page.click("nav button[data-view=review]")
            page.wait_for_selector("#rev-item .seg")
            page.wait_for_timeout(400)
            shot("review")
            browser.close()

        get = lambda path: httpx.get(BASE + path).json()  # noqa: E731
        data = {
            "base": get(f"/runs/{base_id}"),
            "rerun": get(f"/runs/{rerun_id}"),
            "compare": get(f"/compare?a={base_id}&b={rerun_id}"),
            "personas": get("/personas"),
        }
        (SHOTS / "data.json").write_text(json.dumps(data, indent=1, default=str) + "\n")
        print("saved", sorted(p.name for p in SHOTS.iterdir()))
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
