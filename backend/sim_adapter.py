"""Simulated calls with no telephony: Gemini plays both sides of the conversation in text.

The target agent speaks from the *actual* target prompt for the run's version (v1, or a
patched v2, v3, ...), and the caller speaks from the persona's caller prompt. So, unlike
mock mode, whether a call passes depends on what the prompt says, and a before/after
comparison measures the patch itself.

What it can't show: voice, accents, speech-to-text errors, barge-in timing and latency.
Interruptions are approximated in text (a caller cutting off with "—"), and latency is
not measured, so that metric is not counted in this mode.

Each finished conversation is delivered as a Bolna-shaped execution payload, so it goes
through the same webhook path as mock and real calls.
"""

import asyncio
import uuid
from datetime import date
from typing import Any

from .bolna_adapter import BolnaAdapter, DialError
from .bolna_agents import caller_prompt, load_agent_settings, target_prompt
from .config import Settings
from .events import log_event
from .gemini import GeminiJSON
from .target_link import normalize_tool_params

MAX_EXCHANGES = 7  # caller turn + agent turn; keeps each call to at most ~14 model requests
WORDS_PER_SECOND = 2.5  # rough speaking rate, for an estimated call duration

CALLER_SCHEMA = {
    "type": "object",
    "properties": {"say": {"type": "string"}, "hang_up": {"type": "boolean"}},
    "required": ["say", "hang_up"],
    "additionalProperties": False,
}
TARGET_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string"},
        "call_schedule_payment": {"type": "boolean"},
        "schedule_payment": {
            "type": "object",
            "properties": {
                "account_number": {"type": "string"},
                "amount": {"type": "number"},
                "payment_date": {"type": "string"},
                "waive_late_fee": {"type": "boolean"},
            },
            "required": ["account_number", "amount", "payment_date", "waive_late_fee"],
            "additionalProperties": False,
        },
        "hang_up": {"type": "boolean"},
    },
    "required": ["say", "call_schedule_payment", "schedule_payment", "hang_up"],
    "additionalProperties": False,
}

CALLER_RULES = """

This phone call is simulated in text. Reply with only the words you say aloud next.
Show interrupting or trailing off by cutting your sentence with "—". Set hang_up to true
when you say goodbye or give up."""

TARGET_RULES = """

Runtime notes for this call (not from the customer):
- This is a live phone call; you see the text of what has been said so far. Today is {today}.
- To use your schedule_payment tool, set call_schedule_payment to true and fill schedule_payment.
  The tool always succeeds. When you are not calling it, set call_schedule_payment to false and
  leave the schedule_payment fields empty or zero.
- Put only what you say aloud in "say". Set hang_up to true when the call should end."""


def _render(transcript: list[tuple[str, str]], me: str) -> str:
    lines = [f"{'You' if who == me else ('Caller' if who == 'caller' else 'Agent')}: {text}"
             for who, text in transcript]
    return "Conversation so far:\n" + "\n".join(lines) + "\n\nReply with your next turn."


class SimAdapter(BolnaAdapter):
    def __init__(self, settings: Settings, llm: GeminiJSON | None = None):
        super().__init__(settings)
        self.llm = llm or GeminiJSON(settings)
        self.welcome = load_agent_settings()["target"]["welcome_message"]
        self._prompts: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()

    async def prepare_run(self, prompt_version: str) -> None:
        if prompt_version not in self._prompts:
            self._prompts[prompt_version] = target_prompt(prompt_version, self.settings.target_prompts_dir)

    async def start_call(self, *, call_id: str, persona: dict[str, Any], to_number: str,
                         prompt_version: str) -> str:
        self._guard_target(to_number)
        if prompt_version not in self._prompts:
            raise DialError(f"target prompt {prompt_version} was not loaded")
        execution_id = f"sim-{uuid.uuid4()}"
        task = asyncio.create_task(self._call(execution_id, call_id, persona, self._prompts[prompt_version]))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return execution_id

    async def converse(self, persona: dict[str, Any], agent_prompt: str) -> tuple[list[tuple[str, str]], dict | None]:
        """Runs one text conversation. Returns the turns and what the agent sent to its tool."""
        model = self.settings.gemini_sim_model
        caller_system = caller_prompt(persona) + CALLER_RULES
        agent_system = agent_prompt + TARGET_RULES.format(today=date.today().isoformat())
        transcript: list[tuple[str, str]] = [("agent", self.welcome)]
        tool_payload = None
        for _ in range(MAX_EXCHANGES):
            caller = await self.llm.ask(model=model, system=caller_system, prompt=_render(transcript, "caller"),
                                        schema=CALLER_SCHEMA, thinking_level="low")
            if caller["say"].strip():
                transcript.append(("caller", caller["say"].strip()))
            if caller["hang_up"]:
                break
            agent = await self.llm.ask(model=model, system=agent_system, prompt=_render(transcript, "agent"),
                                       schema=TARGET_SCHEMA, thinking_level="low")
            if agent["call_schedule_payment"] and tool_payload is None:
                tool_payload = normalize_tool_params(agent["schedule_payment"])
            if agent["say"].strip():
                transcript.append(("agent", agent["say"].strip()))
            if agent["hang_up"]:
                break
        return transcript, tool_payload

    async def _call(self, execution_id: str, call_id: str, persona: dict[str, Any], agent_prompt: str) -> None:
        assert self._deliver is not None, "SimAdapter.bind() was not called"
        context = {"recipient_data": {"gauntlet_call_id": call_id}}
        await self._deliver({"id": execution_id, "status": "in-progress", "context_details": context})
        try:
            transcript, tool_payload = await self.converse(persona, agent_prompt)
        except Exception as exc:  # noqa: BLE001 - one broken conversation fails one call
            log_event("sim_call_failed", call_id=call_id, error=repr(exc))
            await self._deliver({"id": execution_id, "status": "failed", "context_details": context,
                                 "error_message": f"simulation failed: {type(exc).__name__}: {exc}"[:300]})
            return
        words = sum(len(text.split()) for _, text in transcript)
        await self._deliver({
            "id": execution_id,
            "status": "completed",
            "conversation_duration": round(words / WORDS_PER_SECOND, 1),
            "transcript": "\n".join(f"{'assistant' if who == 'caller' else 'user'}: {text}"
                                    for who, text in transcript),
            "context_details": context,
            "telephony_data": {"recording_url": None, "call_type": "outbound", "provider": "sim"},
            "latency_data": {},  # no audio, so no latency to measure
            "extracted_data": {"tool_payload": tool_payload},
        })

    async def aclose(self) -> None:
        for t in list(self._tasks):
            t.cancel()
