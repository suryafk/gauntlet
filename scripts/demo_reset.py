"""Reset the dashboard for a clean demo take.

Deletes the database named by DATABASE_URL (the next server start loads the saved example
run) and any patched prompt versions (v2.md, v3.md, ...) that demo runs created and git
doesn't track. Stop the server first.

  python scripts/demo_reset.py           # asks before deleting
  python scripts/demo_reset.py --yes
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import ROOT, Settings  # noqa: E402


def server_running(port: int) -> bool:
    try:
        return httpx.get(f"http://localhost:{port}/health", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def untracked_prompt_versions(directory: Path) -> list[Path]:
    """Patched versions (v2+) that git doesn't track, i.e. made by demo runs."""
    found = []
    for path in sorted(directory.glob("v*.md")):
        m = re.fullmatch(r"v(\d+)", path.stem)
        if not m or int(m.group(1)) < 2:
            continue
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", str(path)], cwd=ROOT,
                                 capture_output=True).returncode == 0
        if not tracked:
            found.append(path)
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    parser.add_argument("--port", type=int, default=8000, help="port to check for a running server")
    args = parser.parse_args()

    if server_running(args.port):
        sys.exit(f"The server on port {args.port} is running. Stop it (Ctrl-C), then run this again.")

    settings = Settings()
    db = Path(settings.database_url)
    if not db.is_absolute():
        db = ROOT / db
    targets = [p for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm")) if p.exists()]
    targets += untracked_prompt_versions(settings.target_prompts_dir)
    if not targets:
        print("Nothing to reset.")
        return
    print("Will delete:\n" + "\n".join(f"  {p}" for p in targets))
    if not args.yes and input("Go ahead? [y/N] ").strip().lower() != "y":
        print("Nothing deleted.")
        return
    for p in targets:
        p.unlink()
    print("Reset. Start the server and the dashboard opens on the saved example run.")


if __name__ == "__main__":
    main()
