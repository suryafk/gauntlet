"""Example runs: export a finished run (with its patches and patched re-runs) to a JSON
file, and load it into an empty database so the dashboard never opens blank.

The demo-day plan is to save the best real 30-call run this way (scripts/seed_run.py export).
"""

import json
from pathlib import Path
from typing import Any

from .events import log_event
from .models import Store, now

FORMAT = "gauntlet-seed/1"


def _with_descendants(store: Store, run_id: str) -> list[str]:
    ids, queue = [], [run_id]
    while queue:
        rid = queue.pop(0)
        ids.append(rid)
        queue += [r["id"] for r in store.child_runs(rid)]
    return ids


def export_bundle(store: Store, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    if run["status"] != "completed":
        raise ValueError(f"run {run_id} is still {run['status']}")
    ids = _with_descendants(store, run_id)
    tables = store.export_runs(ids)
    for row in tables["runs"]:
        row["seeded"] = 1
    return {"format": FORMAT, "exported_at": now(), "root_run_id": run_id, "mode": run.get("mode") or "mock",
            "tables": tables}


def write_bundle(bundle: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, indent=1, ensure_ascii=False) + "\n")


def import_bundle(store: Store, path: Path) -> int:
    bundle = json.loads(path.read_text())
    if bundle.get("format") != FORMAT:
        raise ValueError(f"{path} is not a {FORMAT} file")
    added = store.import_rows(bundle["tables"])
    log_event("seed_imported", path=str(path), runs=added, mode=bundle.get("mode"))
    return added


def seed_if_empty(store: Store, path: Path) -> int:
    if store.list_runs() or not path.exists():
        return 0
    try:
        return import_bundle(store, path)
    except Exception as exc:  # noqa: BLE001 - a bad seed file must not stop the server
        log_event("seed_failed", path=str(path), error=repr(exc))
        return 0
