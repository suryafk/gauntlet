"""Builds and manages the two Bolna agents: the flawed target and the synthetic caller.

Payload shapes follow Bolna's v2 agent API (POST /v2/agent, PATCH /v2/agent/{id}).
"""

import json
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT, Settings

AGENTS_DIR = ROOT / "agents"
TARGET_PROMPTS_DIR = AGENTS_DIR / "target_prompts"
TOOL_NAME = "schedule_payment"


class BolnaAPIError(Exception):
    pass


def load_agent_settings() -> dict[str, Any]:
    return json.loads((AGENTS_DIR / "agent_settings.json").read_text())


def target_prompt(version: str, directory: Path = TARGET_PROMPTS_DIR) -> str:
    path = directory / f"{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"no target prompt for version {version!r} at {path}")
    return path.read_text()


def caller_prompt(persona: dict[str, Any] | None = None) -> str:
    """The generic caller prompt, or a persona's prompt with its details filled in."""
    template = (AGENTS_DIR / "caller_prompt.md").read_text()
    if persona is None:
        return template
    return template.format(persona_prompt=persona["behavior_prompt"], **persona["scenario"])


def webhook_url(settings: Settings) -> str:
    # Bolna's API can't set custom webhook headers, so the secret rides in the query string.
    # If you add an X-Webhook-Secret header in the dashboard instead, drop it from here.
    return f"{settings.public_base_url.rstrip('/')}/webhooks/bolna?secret={settings.webhook_secret}"


def schedule_payment_tool(settings: Settings) -> tuple[dict[str, Any], dict[str, Any]]:
    """The target agent's custom function. It posts what the agent extracted to our backend."""
    tool = {
        "name": TOOL_NAME,
        "key": "custom_task",
        "description": "Schedule the caller's bill payment once you have their account number, amount and date.",
        "pre_call_message": "Ek second, main payment schedule kar rahi hoon.",
        "parameters": {
            "type": "object",
            "properties": {
                "account_number": {"type": "string", "description": "The caller's 8-digit account number, digits only"},
                "amount": {"type": "number", "description": "Amount to pay in rupees"},
                "payment_date": {"type": "string", "description": "Payment date as YYYY-MM-DD"},
                "waive_late_fee": {"type": "boolean", "description": "Whether a late fee waiver was granted"},
            },
            "required": ["account_number", "amount", "payment_date"],
        },
    }
    params = {
        "method": "POST",
        "url": f"{settings.public_base_url.rstrip('/')}/tools/schedule_payment?secret={settings.webhook_secret}",
        "api_token": None,
        # call_sid and from_number are injected by Bolna; the rest come from the LLM.
        "param": json.dumps({
            "call_sid": "%(call_sid)s",
            "from_number": "%(from_number)s",
            "account_number": "%(account_number)s",
            "amount": "%(amount)s",
            "payment_date": "%(payment_date)s",
            "waive_late_fee": "%(waive_late_fee)s",
        }),
    }
    return tool, params


def build_agent_payload(role: str, settings: Settings, system_prompt: str,
                        cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_agent_settings()
    side = cfg[role]
    telephony = cfg["telephony_provider"]
    tools_config: dict[str, Any] = {
        "llm_agent": {
            "agent_type": "simple_llm_agent",
            "agent_flow_type": "streaming",
            "llm_config": cfg["llm"],
        },
        "synthesizer": {
            "provider": side["synthesizer"]["provider"],
            "provider_config": {k: v for k, v in side["synthesizer"].items() if k != "provider"},
            "stream": True,
            "buffer_size": 250,
            "audio_format": "wav",
        },
        "transcriber": {**cfg["transcriber"], "stream": True, "encoding": "linear16", "sampling_rate": 16000},
        "input": {"provider": telephony, "format": "wav"},
        "output": {"provider": telephony, "format": "wav"},
    }
    if role == "target":
        tool, params = schedule_payment_tool(settings)
        tools_config["api_tools"] = {"tools": [tool], "tools_params": {TOOL_NAME: params}}

    agent_config: dict[str, Any] = {
        "agent_name": side["name"],
        "webhook_url": webhook_url(settings),
        "tasks": [{
            "task_type": "conversation",
            "toolchain": {"execution": "parallel", "pipelines": [["transcriber", "llm", "synthesizer"]]},
            "tools_config": tools_config,
            "task_config": {
                "call_terminate": side["call_terminate"],
                "hangup_after_silence": side["hangup_after_silence"],
            },
        }],
    }
    if side.get("welcome_message"):
        agent_config["agent_welcome_message"] = side["welcome_message"]
    return {"agent_config": agent_config, "agent_prompts": {"task_1": {"system_prompt": system_prompt}}}


class BolnaAgents:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            base_url=settings.bolna_api_base,
            headers={"Authorization": f"Bearer {settings.bolna_api_key}"},
            timeout=30,
        )

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self.client.request(method, path, **kw)
        if resp.status_code >= 400:
            raise BolnaAPIError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    async def create_agent(self, payload: dict[str, Any]) -> str:
        data = await self._request("POST", "/v2/agent", json=payload)
        return data["agent_id"]

    async def update_agent(self, agent_id: str, payload: dict[str, Any]) -> None:
        await self._request("PATCH", f"/v2/agent/{agent_id}", json=payload)

    async def set_target_prompt(self, version: str) -> None:
        if not self.settings.bolna_target_agent_id:
            raise BolnaAPIError("BOLNA_TARGET_AGENT_ID is not set; run scripts/setup_bolna.py")
        await self.update_agent(self.settings.bolna_target_agent_id,
                                {"agent_prompts": {"task_1": {
                                    "system_prompt": target_prompt(version, self.settings.target_prompts_dir)}}})

    async def list_numbers(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/phone-numbers/all")

    async def attach_inbound(self, agent_id: str, phone_number_id: str) -> dict[str, Any]:
        return await self._request("POST", "/inbound/setup",
                                   json={"agent_id": agent_id, "phone_number_id": phone_number_id})

    async def concurrency(self) -> dict[str, Any] | None:
        data = await self._request("GET", "/user/me")
        return data.get("concurrency")

    async def aclose(self) -> None:
        await self.client.aclose()


def save_agent_ids(ids: dict[str, Any], path: Path = AGENTS_DIR / "agent_ids.json") -> None:
    existing = json.loads(path.read_text()) if path.exists() else {}
    existing.update(ids)
    path.write_text(json.dumps(existing, indent=2) + "\n")
