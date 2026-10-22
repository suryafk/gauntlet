"""Manage the example run the dashboard opens on when the database is empty.

  python scripts/seed_run.py export RUN_ID     # save a finished run (+ its patch and re-run) as the example
  python scripts/seed_run.py generate          # build a mock example: 30-call v1 run, rules patch, re-run
  python scripts/seed_run.py import            # load the example into the database now

Default file: seed/example_run.json. On demo day, export your best real run over the mock one.
"""

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import seed  # noqa: E402
from backend.bolna_adapter import MockBolnaAdapter  # noqa: E402
from backend.config import ROOT, Settings  # noqa: E402
from backend.events import EventBus  # noqa: E402
from backend.models import Store  # noqa: E402
from backend.orchestrator import Orchestrator  # noqa: E402
from backend.patcher import Patcher, TemplatePatcher  # noqa: E402
from backend.personas import load_personas  # noqa: E402
from backend.scorer import HeuristicJudge, Scorer  # noqa: E402

DEFAULT_PATH = ROOT / "seed" / "example_run.json"


async def wait_completed(store: Store, run_id: str) -> None:
    while store.get_run(run_id)["status"] != "completed":
        await asyncio.sleep(0.1)


async def generate(out: Path, per_persona: int, seed_value: int) -> None:
    tmp = Path(tempfile.mkdtemp())
    settings = Settings()
    settings.bolna_mode = "mock"
    settings.database_url = str(tmp / "seed.db")
    settings.target_prompts_dir = tmp / "prompts"
    settings.target_prompts_dir.mkdir()
    shutil.copy(ROOT / "agents" / "target_prompts" / "v1.md", settings.target_prompts_dir / "v1.md")
    settings.mock_call_min_s, settings.mock_call_max_s = 0.1, 0.4

    store = Store(settings.database_url)
    personas = load_personas(settings.personas_dir)
    for p in personas.values():
        store.upsert_persona(p)
    orch = Orchestrator(store, MockBolnaAdapter(settings, seed=seed_value), settings, EventBus(), personas)
    orch.on_call_finished.append(Scorer(store, EventBus(), settings, personas, HeuristicJudge()).score_call)
    patcher = Patcher(store, settings, personas, TemplatePatcher())

    base = orch.start_run(list(personas), per_persona, "v1")
    await wait_completed(store, base["id"])
    patch = await patcher.generate(store.get_run(base["id"]))
    version = patcher.write_version(patch)
    rerun = orch.start_run(list(personas), per_persona, version, base["id"])
    store.mark_patch_applied(patch["id"], rerun["id"], version)
    await wait_completed(store, rerun["id"])
    await orch.shutdown()

    seed.write_bundle(seed.export_bundle(store, base["id"]), out)
    print(f"Wrote mock example ({base['id']} -> {rerun['id']}) to {out}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("run_id")
    gen = sub.add_parser("generate")
    gen.add_argument("--per-persona", type=int, default=5)
    gen.add_argument("--seed", type=int, default=11)
    sub.add_parser("import")
    for p in (exp, gen, sub.choices["import"]):
        p.add_argument("--file", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args()

    if args.cmd == "generate":
        asyncio.run(generate(args.file, args.per_persona, args.seed))
        return
    store = Store(Settings().database_url)
    if args.cmd == "export":
        seed.write_bundle(seed.export_bundle(store, args.run_id), args.file)
        print(f"Saved run {args.run_id} and its re-runs to {args.file}")
    else:
        print(f"Imported {seed.import_bundle(store, args.file)} run(s) from {args.file}")


if __name__ == "__main__":
    main()
