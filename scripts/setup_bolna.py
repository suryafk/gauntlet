"""Create (or update) the Bolna agents Gauntlet needs and wire up the target's inbound number.

  python scripts/setup_bolna.py --dry-run        # print the agent payloads, call nothing
  python scripts/setup_bolna.py                  # create/update target + generic caller
  python scripts/setup_bolna.py --per-persona    # also one caller agent per persona (prompt baked in)

Re-running updates the agents recorded in agents/agent_ids.json instead of creating new ones.
Needs BOLNA_API_KEY, PUBLIC_BASE_URL, TARGET_NUMBER and WEBHOOK_SECRET in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import bolna_agents as ba  # noqa: E402
from backend.config import ROOT, Settings, _agent_ids  # noqa: E402
from backend.personas import load_personas  # noqa: E402


def payloads(settings: Settings, per_persona: bool) -> dict[str, dict]:
    cfg = ba.load_agent_settings()
    out = {
        "target": ba.build_agent_payload("target", settings, ba.target_prompt("v1"), cfg),
        "caller": ba.build_agent_payload("caller", settings, ba.caller_prompt(), cfg),
    }
    if per_persona:
        for pid, persona in load_personas(settings.personas_dir).items():
            payload = ba.build_agent_payload("caller", settings, ba.caller_prompt(persona), cfg)
            payload["agent_config"]["agent_name"] = f"Gauntlet Caller: {persona['name']}"
            out[f"persona:{pid}"] = payload
    return out


async def upsert(agents: ba.BolnaAgents, existing_id: str | None, payload: dict, label: str) -> str:
    if existing_id:
        await agents.update_agent(existing_id, payload)
        print(f"updated {label}: {existing_id}")
        return existing_id
    agent_id = await agents.create_agent(payload)
    print(f"created {label}: {agent_id}")
    return agent_id


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--per-persona", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    all_payloads = payloads(settings, args.per_persona)
    if args.dry_run:
        print(json.dumps(all_payloads, indent=2, ensure_ascii=False))
        return

    problems = []
    if not settings.bolna_api_key:
        problems.append("BOLNA_API_KEY is empty")
    if not settings.public_base_url.startswith("https://") or "your-tunnel" in settings.public_base_url:
        problems.append("PUBLIC_BASE_URL must be a public https URL (ngrok or your host)")
    if not settings.webhook_secret or settings.webhook_secret == "change-me":
        problems.append("set WEBHOOK_SECRET to a random value")
    if problems:
        sys.exit("fix .env first:\n  - " + "\n  - ".join(problems))

    ids = _agent_ids()
    agents = ba.BolnaAgents(settings)
    try:
        conc = await agents.concurrency()
        print(f"account concurrency: {conc}")

        ids["target"] = await upsert(agents, ids.get("target"), all_payloads["target"], "target agent")
        ids["caller"] = await upsert(agents, ids.get("caller"), all_payloads["caller"], "caller agent")
        personas = ids.setdefault("personas", {})
        for key, payload in all_payloads.items():
            if key.startswith("persona:"):
                pid = key.split(":", 1)[1]
                personas[pid] = await upsert(agents, personas.get(pid), payload, f"caller agent ({pid})")
        ids["target_prompt_version"] = "v1"
        ba.save_agent_ids(ids)
        print(f"saved ids to {ROOT / 'agents' / 'agent_ids.json'}")

        numbers = await agents.list_numbers()
        match = next((n for n in numbers if n.get("phone_number") == settings.target_number), None)
        if match is None:
            owned = ", ".join(n.get("phone_number", "?") for n in numbers) or "none"
            print(f"\nTARGET_NUMBER {settings.target_number} is not in your Bolna account (you own: {owned}).")
            print("Buy or connect a number in Bolna, set TARGET_NUMBER to it and re-run.")
        else:
            await agents.attach_inbound(ids["target"], match["id"])
            print(f"target agent now answers {settings.target_number}")
    finally:
        await agents.aclose()


if __name__ == "__main__":
    asyncio.run(main())
