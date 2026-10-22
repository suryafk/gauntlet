"""Builds src/timeline.json for the Remotion video from the real inputs:
narration.json (script), public/audio/*.mp3 (ElevenLabs narration, durations via ffprobe)
and public/footage/marks.json (when each scene starts in the recorded app footage).

  python3 build_timeline.py
"""

import json
import math
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
FPS = 30
TAIL_FRAMES = 6  # a breath after each line

KIND = {"s1_problem": "intro", "s8_close": "outro"}
LABEL = {
    "s2_overview": "Overview · one-click flagship scenario",
    "s3_live": "Run · 30 calls in parallel, scored live",
    "s4_failure": "Drill-down · the exact line that failed",
    "s5_patch": "Patch · clustered failures → prompt diff",
    "s6_compare": "Compare · same callers, patched prompt",
    "s7_evidence": "Evidence · a real Gemini-simulated call",
}


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def captions(text: str, frames: int) -> list[dict]:
    """Split narration into short lines, timed by their share of the characters."""
    # split where a new sentence starts, never inside "pachaas... nahi"
    parts = [p.strip() for p in re.split(r"(?<=[.:?!])\s+(?=[A-Z])", text) if p.strip()]
    lines: list[str] = []
    for p in parts:
        while len(p) > 90:  # break long sentences at a comma near the middle
            cut = p.rfind(", ", 0, 90)
            if cut < 30:
                break
            lines.append(p[:cut + 1])
            p = p[cut + 2:]
        lines.append(p)
    total = sum(len(line) for line in lines)
    out, start = [], 0
    for line in lines:
        n = round(frames * len(line) / total)
        out.append({"text": line, "from": start, "frames": max(n, 1)})
        start += n
    return out


def main() -> None:
    narration = json.loads((HERE / "narration.json").read_text())
    marks = json.loads((HERE / "public" / "footage" / "marks.json").read_text())
    scenes = []
    for s in narration["scenes"]:
        sid = s["id"]
        audio_frames = math.ceil(duration(HERE / "public" / "audio" / f"{sid}.mp3") * FPS)
        scene = {
            "id": sid,
            "kind": KIND.get(sid, "footage"),
            "audio": f"audio/{sid}.mp3",
            "durationFrames": audio_frames + TAIL_FRAMES,
            "captions": captions(s["text"], audio_frames),
        }
        if scene["kind"] == "footage":
            scene["footageStartFrame"] = round(marks[sid] * FPS)
            scene["label"] = LABEL[sid]
            scene["real"] = sid == "s7_evidence"  # real Gemini output, until the switch below
        if sid == "s7_evidence":
            scene["switchFrame"] = round((marks["s7_review"] - marks[sid]) * FPS)
            scene["label2"] = "Review · a person spot-checks the judge"
        scenes.append(scene)
    timeline = {"fps": FPS, "width": 1920, "height": 1080, "scenes": scenes,
                "totalFrames": sum(s["durationFrames"] for s in scenes)}
    (HERE / "src").mkdir(exist_ok=True)
    (HERE / "src" / "timeline.json").write_text(json.dumps(timeline, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(scenes)} scenes, {timeline['totalFrames'] / FPS:.1f}s")


if __name__ == "__main__":
    main()
