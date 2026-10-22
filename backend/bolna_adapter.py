"""The only module that talks to Bolna: start a call, parse a webhook.

RealBolnaAdapter follows Bolna's docs (POST /call, execution-shaped webhooks).
MockBolnaAdapter produces the same execution-shaped payloads, so both modes go
through the same webhook parsing path.
"""

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import mock_data
from .bolna_agents import BolnaAgents
from .config import Settings

# Bolna execution status -> our call status
STATUS_MAP = {
    "scheduled": "queued",
    "queued": "queued",
    "rescheduled": "queued",
    "initiated": "dialing",
    "ringing": "dialing",
    "in-progress": "in_progress",
    "call-disconnected": "in_progress",  # cost/transcript only arrive with "completed"
    "completed": "completed",
    "balance-low": "failed",
    "busy": "failed",
    "no-answer": "failed",
    "canceled": "failed",
    "failed": "failed",
    "stopped": "failed",
    "error": "failed",
}

Deliver = Callable[[dict[str, Any]], Awaitable[Any]]


class DialError(Exception):
    pass


@dataclass
class CallResult:
    bolna_call_id: str
    status: str
    gauntlet_call_id: str | None = None
    transcript: list[dict[str, str]] = field(default_factory=list)
    audio_url: str | None = None
    first_response_ms: float | None = None
    duration_s: float | None = None
    tool_payload: dict[str, Any] | None = None
    error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in ("completed", "failed")


def parse_transcript(raw: str | None, assistant: str = "caller") -> list[dict[str, str]]:
    """Bolna transcripts are "assistant: ...\\nuser: ..." from one agent's side.

    On the caller's execution assistant = caller and user = target agent; on the
    target's own execution it is the other way round (pass assistant="agent").
    """
    roles = {"assistant": assistant, "user": "agent" if assistant == "caller" else "caller"}
    turns: list[dict[str, str]] = []
    for line in (raw or "").splitlines():
        speaker, sep, text = line.partition(":")
        if not sep or speaker.strip().lower() not in roles:
            if turns and line.strip():
                turns[-1]["text"] += " " + line.strip()
            continue
        role = roles[speaker.strip().lower()]
        turns.append({"speaker": role, "text": text.strip()})
    return turns


def _float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_execution(payload: dict[str, Any]) -> CallResult:
    status = STATUS_MAP.get(str(payload.get("status", "")).lower(), "in_progress")
    context = payload.get("context_details") or {}
    user_data = context.get("recipient_data") or context.get("user_data") or {}
    telephony = payload.get("telephony_data") or {}
    latency = payload.get("latency_data") or {}
    extracted = payload.get("extracted_data") or {}
    error = payload.get("error_message") or None
    if status == "failed" and not error:
        error = f"bolna status {payload.get('status')}"
    return CallResult(
        bolna_call_id=str(payload.get("id") or payload.get("execution_id") or ""),
        status=status,
        gauntlet_call_id=user_data.get("gauntlet_call_id"),
        transcript=parse_transcript(payload.get("transcript")),
        audio_url=telephony.get("recording_url"),
        # [verify] this is the caller agent's own latency; the target's latency
        # will need the target agent's execution in stage 2.
        first_response_ms=_float(latency.get("time_to_first_audio")),
        duration_s=_float(payload.get("conversation_duration") or telephony.get("duration")),
        tool_payload=extracted.get("tool_payload") if isinstance(extracted, dict) else None,
        error=error,
    )


class BolnaAdapter:
    # Real calls need the target agent's side joined in afterwards (see target_link.py).
    needs_target_link = False

    def __init__(self, settings: Settings):
        self.settings = settings
        self._deliver: Deliver | None = None

    def bind(self, deliver: Deliver) -> None:
        """Mock mode pushes its fake webhooks through this callback."""
        self._deliver = deliver

    def _guard_target(self, to_number: str) -> None:
        if to_number != self.settings.target_number:
            raise DialError(f"refusing to dial {to_number}: only TARGET_NUMBER may be called")

    async def prepare_run(self, prompt_version: str) -> None:
        """Make sure the target agent runs the given prompt version before dialing."""

    async def start_call(self, *, call_id: str, persona: dict[str, Any], to_number: str,
                         prompt_version: str) -> str:
        raise NotImplementedError

    def parse_webhook(self, payload: dict[str, Any]) -> CallResult:
        return parse_execution(payload)

    async def aclose(self) -> None:
        pass


class RealBolnaAdapter(BolnaAdapter):
    needs_target_link = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        super().__init__(settings)
        self.client = client or httpx.AsyncClient(
            base_url=settings.bolna_api_base,
            headers={"Authorization": f"Bearer {settings.bolna_api_key}"},
            timeout=20,
        )
        self.agents = BolnaAgents(settings, self.client)
        self._applied_prompt: str | None = None
        self._prompt_lock = asyncio.Lock()

    async def prepare_run(self, prompt_version: str) -> None:
        # Runs share one target agent, so its prompt version is global: a run started while
        # another is in flight switches the target for both. Fine for a one-user demo.
        async with self._prompt_lock:
            if self._applied_prompt != prompt_version:
                await self.agents.set_target_prompt(prompt_version)
                self._applied_prompt = prompt_version

    async def start_call(self, *, call_id: str, persona: dict[str, Any], to_number: str,
                         prompt_version: str) -> str:
        self._guard_target(to_number)
        agent_id = (self.settings.persona_caller_agent_ids.get(persona["id"])
                    or persona.get("caller_agent_id")
                    or self.settings.bolna_caller_agent_id)
        if not agent_id:
            raise DialError("no caller agent id: run scripts/setup_bolna.py or set BOLNA_CALLER_AGENT_ID")
        if self.settings.bolna_from_number == to_number:
            raise DialError("BOLNA_FROM_NUMBER equals TARGET_NUMBER; the caller needs its own number")
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "recipient_phone_number": to_number,
            # Fills {placeholders} in the caller agent's prompt.
            "user_data": {
                "gauntlet_call_id": call_id,
                "persona_prompt": persona["behavior_prompt"],
                **{k: str(v) for k, v in persona["scenario"].items()},
            },
        }
        if self.settings.bolna_from_number:
            body["from_phone_number"] = self.settings.bolna_from_number

        for attempt in range(4):
            resp = await self.client.post("/call", json=body)
            if resp.status_code != 429:
                break
            await asyncio.sleep(2**attempt)  # rate limited: back off
        if resp.status_code >= 400:
            raise DialError(f"bolna /call {resp.status_code}: {resp.text[:200]}")
        execution_id = resp.json().get("execution_id")
        if not execution_id:
            raise DialError(f"bolna /call returned no execution_id: {resp.text[:200]}")
        return execution_id

    async def aclose(self) -> None:
        await self.client.aclose()


class MockBolnaAdapter(BolnaAdapter):
    """Fakes calls: waits a bit, then delivers Bolna-shaped webhooks with canned transcripts.

    The target's flaws fire often on the baseline prompt (v1) and rarely once patched,
    so a mock before/after comparison shows an improvement.
    """

    FAIL_PROB = {"v1": 0.7}
    PATCHED_FAIL_PROB = 0.15

    def __init__(self, settings: Settings, seed: int | None = None):
        super().__init__(settings)
        self.rng = random.Random(seed)
        self._tasks: set[asyncio.Task] = set()

    async def start_call(self, *, call_id: str, persona: dict[str, Any], to_number: str,
                         prompt_version: str) -> str:
        self._guard_target(to_number)
        if self.rng.random() < self.settings.mock_dial_fail_rate:
            raise DialError("mock: carrier rejected the call")
        execution_id = str(uuid.uuid4())
        fail_prob = self.FAIL_PROB.get(prompt_version, self.PATCHED_FAIL_PROB)
        outcome = "fail" if self.rng.random() < fail_prob else "pass"
        duration = self.rng.uniform(self.settings.mock_call_min_s, self.settings.mock_call_max_s)
        task = asyncio.create_task(self._simulate(execution_id, call_id, persona, outcome, duration))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return execution_id

    async def _simulate(self, execution_id: str, call_id: str, persona: dict[str, Any],
                        outcome: str, duration: float) -> None:
        assert self._deliver is not None, "MockBolnaAdapter.bind() was not called"
        context = {"recipient_data": {"gauntlet_call_id": call_id}}
        await asyncio.sleep(duration * 0.2)
        await self._deliver({"id": execution_id, "status": "in-progress", "context_details": context})
        await asyncio.sleep(duration * 0.8)
        script = mock_data.script_for(persona["id"], outcome)
        transcript = "\n".join(
            f"{'assistant' if speaker == 'caller' else 'user'}: {text}" for speaker, text in script
        )
        slow = outcome == "fail" and self.rng.random() < 0.3
        await self._deliver({
            "id": execution_id,
            "status": "completed",
            "conversation_duration": round(20 + len(script) * 6 + self.rng.uniform(0, 10), 1),
            "transcript": transcript,
            "context_details": context,
            "telephony_data": {"recording_url": None, "call_type": "outbound", "provider": "mock"},
            "latency_data": {"time_to_first_audio": round(self.rng.uniform(1800, 2600) if slow
                                                          else self.rng.uniform(600, 1200), 1)},
            "extracted_data": {"tool_payload": mock_data.payload_for(persona, outcome)},
        })

    async def aclose(self) -> None:
        for t in list(self._tasks):
            t.cancel()


def make_adapter(settings: Settings) -> BolnaAdapter:
    if settings.bolna_mode == "real":
        return RealBolnaAdapter(settings)
    return MockBolnaAdapter(settings)
