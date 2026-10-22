"""Loads persona definitions from personas/*.json."""

import json
from pathlib import Path
from typing import Any

REQUIRED = {"id", "name", "language_mix", "behavior_prompt", "success_criteria", "scenario"}


def load_personas(directory: Path) -> dict[str, dict[str, Any]]:
    personas: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        p = json.loads(path.read_text())
        missing = REQUIRED - p.keys()
        if missing:
            raise ValueError(f"{path.name} is missing {sorted(missing)}")
        personas[p["id"]] = p
    return personas
