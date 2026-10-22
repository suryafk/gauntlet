"""Hand spot-check: a person labels the judged metrics on a sample of calls, and we
report how often the judge agrees. The sample mixes calls the judge passed and failed,
spread across personas, and is the same every time for a given run.
"""

import hashlib
from collections import defaultdict
from typing import Any

from .models import Store
from .scorer import JUDGED, METRIC_LABELS

DEFAULT_SAMPLE = 10


def _order(run_id: str, call_id: str) -> str:
    return hashlib.sha256(f"{run_id}:{call_id}".encode()).hexdigest()


def sample_calls(store: Store, run_id: str, n: int = DEFAULT_SAMPLE) -> list[dict]:
    scores: dict[str, dict[str, bool]] = defaultdict(dict)
    for s in store.scores_for_run(run_id):
        if s["metric"] in JUDGED:
            scores[s["call_id"]][s["metric"]] = bool(s["passed"])
    calls = [c for c in store.list_calls(run_id)
             if c["status"] == "completed" and len(scores.get(c["id"], {})) == len(JUDGED)]

    def by_persona(group: list[dict]) -> list[dict]:
        """Round-robin across personas so one persona can't fill the sample."""
        buckets: dict[str, list[dict]] = defaultdict(list)
        for c in sorted(group, key=lambda c: _order(run_id, c["id"])):
            buckets[c["persona_id"]].append(c)
        out, keys = [], sorted(buckets)
        while any(buckets.values()):
            for k in keys:
                if buckets[k]:
                    out.append(buckets[k].pop(0))
        return out

    failed = by_persona([c for c in calls if not all(scores[c["id"]].values())])
    passed = by_persona([c for c in calls if all(scores[c["id"]].values())])
    picked: list[dict] = []
    while len(picked) < n and (failed or passed):
        for group in (failed, passed):
            if group and len(picked) < n:
                picked.append(group.pop(0))
    return picked


def agreement(store: Store, run_id: str) -> dict[str, Any]:
    judged = {(s["call_id"], s["metric"]): s for s in store.scores_for_run(run_id) if s["metric"] in JUDGED}
    per_metric = {m: {"agree": 0, "total": 0} for m in JUDGED}
    disagreements, reviewed_calls, judges = [], set(), set()
    for r in store.reviews_for_run(run_id):
        s = judged.get((r["call_id"], r["metric"]))
        if s is None:
            continue
        reviewed_calls.add(r["call_id"])
        judges.add(s.get("judge") or "unknown")
        same = bool(r["human_passed"]) == bool(s["passed"])
        per_metric[r["metric"]]["agree"] += same
        per_metric[r["metric"]]["total"] += 1
        if not same:
            disagreements.append({"call_id": r["call_id"], "metric": r["metric"], "label": METRIC_LABELS[r["metric"]],
                                  "judge_passed": bool(s["passed"]), "human_passed": bool(r["human_passed"]),
                                  "judge_reason": s["reason"], "note": r.get("note") or ""})
    agree = sum(v["agree"] for v in per_metric.values())
    total = sum(v["total"] for v in per_metric.values())
    return {
        "reviewed_calls": len(reviewed_calls),
        "labels": total,
        "rate": agree / total if total else None,
        "per_metric": {m: {**v, "label": METRIC_LABELS[m], "rate": v["agree"] / v["total"] if v["total"] else None}
                       for m, v in per_metric.items()},
        "judges": sorted(judges),
        "disagreements": disagreements,
    }


def spotcheck(store: Store, run_id: str, personas: dict[str, dict], n: int = DEFAULT_SAMPLE) -> dict[str, Any]:
    reviews: dict[str, dict[str, Any]] = defaultdict(dict)
    notes: dict[str, str] = {}
    for r in store.reviews_for_run(run_id):
        reviews[r["call_id"]][r["metric"]] = bool(r["human_passed"])
        if r.get("note"):
            notes[r["call_id"]] = r["note"]
    scores: dict[str, dict[str, dict]] = defaultdict(dict)
    for s in store.scores_for_run(run_id):
        scores[s["call_id"]][s["metric"]] = {"passed": bool(s["passed"]), "reason": s["reason"],
                                             "judge": s.get("judge"), "span": s.get("span")}
    items = []
    for c in sample_calls(store, run_id, n):
        persona = personas[c["persona_id"]]
        items.append({
            "call_id": c["id"],
            "persona_id": c["persona_id"],
            "persona": persona["name"],
            "scenario": persona["scenario"],
            "transcript": c["transcript"],
            "tool_payload": c["tool_payload"],
            "scores": scores[c["id"]],
            "review": reviews.get(c["id"]) or None,
            "note": notes.get(c["id"], ""),
        })
    return {"run_id": run_id, "metrics": JUDGED, "metric_labels": {m: METRIC_LABELS[m] for m in JUDGED},
            "items": items, "agreement": agreement(store, run_id)}
