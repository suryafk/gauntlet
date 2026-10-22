"""Records real footage of the running Gauntlet app for the demo video.

Starts its own server (mock mode, MOCK_SEED=672, a throwaway database and prompt folder),
drives the flagship journey in headless Chromium with a visible cursor, and writes:
  public/footage/app.webm        the continuous recording
  public/footage/marks.json      when each scene starts, in seconds from the start of the video

Each scene holds at least as long as its narration (public/audio/<scene>.mp3), so the video
can cut footage to the voiceover without freezing frames.

  ../.venv/bin/python record.py
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
PORT = 8790
BASE = f"http://localhost:{PORT}"
OUT = HERE / "public" / "footage"
AUDIO = HERE / "public" / "audio"
# Recorded at 1536x864 (Playwright captures video at page size) and scaled 1.25x to 1080p in
# Remotion, so the UI reads larger than it would in a 1920-wide browser.
W, H = 1536, 864

CURSOR_JS = """
(() => {
  const install = () => {
  if (window.__cursor) return;
  const c = document.createElement('div');
  c.style.cssText = 'position:fixed;left:0;top:0;width:18px;height:18px;margin:-3px 0 0 -3px;z-index:99999;pointer-events:none;transition:transform .08s';
  c.innerHTML = '<svg width="18" height="18" viewBox="0 0 22 22"><path d="M3 2l14 8-6 1.6L8 18z" fill="#fff" stroke="#0B0F14" stroke-width="1.5"/></svg>';
  document.documentElement.appendChild(c);
  window.__cursor = c;
  addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', () => { c.style.transform = 'scale(.8)'; }, true);
  addEventListener('mouseup', () => { c.style.transform = ''; }, true);
  };
  if (document.documentElement) install(); else addEventListener('DOMContentLoaded', install);
  addEventListener('DOMContentLoaded', install);
})();
"""


def audio_seconds(scene: str, default: float) -> float:
    path = AUDIO / f"{scene}.mp3"
    if not path.exists():
        return default
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or default)


def start_server(tmp: Path) -> subprocess.Popen:
    prompts = tmp / "prompts"
    prompts.mkdir()
    shutil.copy(ROOT / "agents" / "target_prompts" / "v1.md", prompts / "v1.md")
    env = {**os.environ, "BOLNA_MODE": "mock", "JUDGE_MODE": "heuristic", "MOCK_SEED": "672",
           "MAX_CONCURRENCY": "10", "MOCK_DIAL_FAIL_RATE": "0.05", "DATABASE_URL": str(tmp / "demo.db"),
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


class Director:
    def __init__(self, page):
        self.page = page
        self.t0 = time.monotonic()
        self.marks: dict[str, float] = {}
        self.x, self.y = W / 2, H / 2

    def mark(self, scene: str) -> float:
        self.marks[scene] = round(time.monotonic() - self.t0, 3)
        return time.monotonic()

    def hold_until(self, started: float, seconds: float) -> None:
        left = started + seconds - time.monotonic()
        if left > 0:
            self.page.wait_for_timeout(left * 1000)

    def move_to(self, selector: str, steps: int = 25) -> None:
        box = self.page.locator(selector).first.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.page.mouse.move(x, y, steps=steps)
        self.x, self.y = x, y

    def click(self, selector: str) -> None:
        self.page.locator(selector).first.scroll_into_view_if_needed()
        self.move_to(selector)
        self.page.wait_for_timeout(250)
        self.page.mouse.down()
        self.page.wait_for_timeout(90)
        self.page.mouse.up()  # a real click at the cursor's position

    def scroll_to(self, selector: str, offset: int = 90) -> None:
        self.page.evaluate("""([sel, off]) => {
            const el = document.querySelector(sel);
            window.scrollTo({top: el.getBoundingClientRect().top + window.scrollY - off, behavior: 'smooth'});
        }""", [selector, offset])
        self.page.wait_for_timeout(900)

    def wait_run_completed(self, timeout_s: float = 60) -> None:
        self.page.wait_for_function("() => document.querySelector('#run-status')?.textContent === 'completed'",
                                    timeout=timeout_s * 1000)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    server = start_server(tmp)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(tmp / "video"),
                                      record_video_size={"width": W, "height": H}, color_scheme="dark")
            ctx.add_init_script(CURSOR_JS)
            page = ctx.new_page()
            d = Director(page)

            # Overview: the flagship scenario starts here
            page.goto(BASE + "/")
            page.wait_for_selector("#ov-runs tr")
            page.mouse.move(W * 0.6, H * 0.55)
            t = d.mark("s2_overview")
            page.wait_for_timeout(2500)
            d.move_to("#ov-pipeline", steps=30)
            page.wait_for_timeout(3000)
            d.move_to("#ov-tiles .card:nth-child(3)", steps=25)
            d.hold_until(t, audio_seconds("s2_overview", 18) - 2.2)
            d.click("#flagship")

            # Live run: the heatmap fills
            t = d.mark("s3_live")
            page.wait_for_selector("#run-status")
            page.mouse.move(W * 0.32, H * 0.55, steps=30)
            d.wait_run_completed()
            d.hold_until(t, audio_seconds("s3_live", 15.5))

            # The failing line
            t = d.mark("s4_failure")
            d.click('#heatmap .cell[data-p="corrector"][data-m="extraction_accuracy"]')
            page.wait_for_timeout(600)
            d.scroll_to("#transcript", offset=110)
            d.move_to("#transcript .turn.flagged", steps=30)
            page.wait_for_timeout(5500)
            d.move_to("#transcript .trace", steps=30)
            d.hold_until(t, audio_seconds("s4_failure", 16.5))

            # Patch and re-run
            t = d.mark("s5_patch")
            d.scroll_to("#patch", offset=120)
            d.click("#patch")
            page.wait_for_selector("#apply")
            page.wait_for_timeout(900)
            d.move_to("#patch-body .clusters", steps=25)
            page.wait_for_timeout(2800)
            d.scroll_to("#patch-body pre.diff", offset=140)
            d.move_to("#patch-body pre.diff", steps=30)
            d.hold_until(t, audio_seconds("s5_patch", 18.5) - 2.0)
            d.click("#apply")
            d.hold_until(t, audio_seconds("s5_patch", 18.5))
            d.wait_run_completed()  # this wait is cut from the video

            # Before / after
            page.evaluate("window.scrollTo({top: 0})")
            page.wait_for_timeout(400)
            t = d.mark("s6_compare")
            d.click('#run-lineage a[data-compare]')
            page.wait_for_selector("#cmp-body .big")
            page.wait_for_timeout(1500)
            d.move_to("#cmp-body .card .big", steps=25)
            page.wait_for_timeout(4500)
            d.scroll_to("#cmp-body .split", offset=120)
            d.move_to("#cmp-body .bars", steps=30)
            d.hold_until(t, audio_seconds("s6_compare", 16.5))

            # Evidence, then Review
            t = d.mark("s7_evidence")
            page.goto(BASE + "/evidence.html")
            page.wait_for_timeout(1500)
            d.scroll_to(".card:nth-of-type(2) .turn.hl", offset=380)
            page.wait_for_timeout(3500)
            d.scroll_to("table", offset=160)
            page.wait_for_timeout(2200)
            d.mark("s7_review")  # from here the footage is the mock dashboard again
            page.goto(BASE + "/")
            page.wait_for_selector("nav button[data-view=review]")
            d.click("nav button[data-view=review]")
            page.wait_for_selector("#rev-item .seg")
            d.move_to("#rev-item .seg", steps=25)
            d.hold_until(t, audio_seconds("s7_evidence", 14.5))
            d.mark("end")

            page.close()
            video = page.video.path()
            ctx.close()
            browser.close()
        shutil.move(video, OUT / "app.webm")
        (OUT / "marks.json").write_text(json.dumps(d.marks, indent=2) + "\n")
        print(json.dumps(d.marks, indent=2))
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
