"""Joins the target agent's side of a call to the caller-side call we placed.

The caller's webhook only shows the caller's view. What the target *did* (its
schedule_payment tool call, its own response latency, what it heard) arrives
separately: the tool posts to /tools/schedule_payment with the telephony call_sid,
and the target's own execution webhook carries the same id as provider_call_id.

All synthetic calls dial from the same number, so we pair a target leg with a
caller call by time overlap plus transcript similarity (both sides transcribe the
same conversation, so the right pair reads alike).
"""

import asyncio
import re
import uuid
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from .bolna_adapter import STATUS_MAP, parse_transcript
from .config import Settings
from .events import log_event
from .models import Store

MIN_SIMILARITY = 0.3
WINDOW = timedelta(seconds=20)


def _unfilled(v: Any) -> bool:
    return v is None or (isinstance(v, str) and (v.strip() == "" or "%(" in v))


def normalize_tool_params(params: dict[str, Any]) -> dict[str, Any]:
    """Bolna sends every templated value as a string; turn them back into typed fields."""
    out: dict[str, Any] = {}
    acct = params.get("account_number")
    if not _unfilled(acct):
        out["account_number"] = re.sub(r"\D", "", str(acct))
    amount = params.get("amount")
    if not _unfilled(amount):
        try:
            num = float(str(amount).replace(",", "").replace("₹", "").strip())
            out["amount"] = int(num) if num.is_integer() else num
        except ValueError:
            out["amount"] = amount
    date = params.get("payment_date")
    if not _unfilled(date):
        out["payment_date"] = str(date).strip()
    waive = params.get("waive_late_fee")
    if not _unfilled(waive):
        out["waive_late_fee"] = str(waive).strip().lower() in ("true", "1", "yes")
    return out


def _words(turns: list[dict[str, str]] | None) -> list[str]:
    text = " ".join(t.get("text", "") for t in turns or [])
    return re.findall(r"\w+", text.lower())[:400]


def similarity(a: list[dict[str, str]] | None, b: list[dict[str, str]] | None) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)


class TargetLinker:
    def __init__(self, store: Store, settings: Settings, poll_s: float = 1.0):
        self.store = store
        self.settings = settings
        self.poll_s = poll_s

    def is_target_execution(self, payload: dict[str, Any]) -> bool:
        target = self.settings.bolna_target_agent_id
        return bool(target) and str(payload.get("agent_id", "")) == target

    def record_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        call_sid = params.get("call_sid")
        if _unfilled(call_sid):
            call_sid = f"unknown-{uuid.uuid4().hex[:8]}"
            log_event("tool_call_without_sid", params=params)
        payload = normalize_tool_params(params)
        self.store.upsert_target_leg(call_sid, tool_payload=payload)
        log_event("target_tool_call", call_sid=call_sid, payload=payload)
        return {"status": "scheduled", "confirmation_id": f"PE{uuid.uuid4().hex[:6].upper()}"}

    def record_execution(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = STATUS_MAP.get(str(payload.get("status", "")).lower(), "in_progress")
        if status not in ("completed", "failed"):
            return {"ok": True, "target": True, "stored": False}
        telephony = payload.get("telephony_data") or {}
        call_sid = telephony.get("provider_call_id") or f"exec-{payload.get('id')}"
        latency = (payload.get("latency_data") or {}).get("time_to_first_audio")
        self.store.upsert_target_leg(
            call_sid,
            execution_id=payload.get("id"),
            created_at=payload.get("created_at"),
            status=status,
            transcript=parse_transcript(payload.get("transcript"), assistant="agent"),
            first_response_ms=float(latency) if latency is not None else None,
        )
        log_event("target_execution", call_sid=call_sid, status=status)
        return {"ok": True, "target": True, "stored": True}

    def try_link(self, call_id: str) -> bool:
        call = self.store.get_call(call_id)
        if call is None or call.get("target_call_sid"):
            return bool(call)
        start, end = _ts(call.get("started_at")), _ts(call.get("ended_at"))
        best, best_sim, in_window = None, -1.0, 0
        for leg in self.store.unlinked_target_legs():
            created = _ts(leg.get("created_at"))
            if start and created and not (start - WINDOW <= created <= (end or start) + WINDOW):
                continue
            in_window += 1
            sim = similarity(call.get("transcript"), leg.get("transcript"))
            if sim > best_sim:
                best, best_sim = leg, sim
        if best is None:
            return False
        no_text = not call.get("transcript")
        if best_sim < MIN_SIMILARITY and not (no_text and in_window == 1):
            return False
        if not self.store.link_target_leg(best["call_sid"], call_id):
            return False
        fields: dict[str, Any] = {
            "target_call_sid": best["call_sid"],
            "target_transcript": best.get("transcript"),
            "tool_payload": best.get("tool_payload"),
        }
        if best.get("first_response_ms") is not None:
            fields["first_response_ms"] = best["first_response_ms"]
        self.store.update_call(call_id, **fields)
        log_event("target_linked", call_id=call_id, call_sid=best["call_sid"], similarity=round(best_sim, 2))
        return True

    async def link(self, call_id: str, timeout: float | None = None) -> bool:
        deadline = asyncio.get_running_loop().time() + (timeout or self.settings.link_timeout_s)
        while True:
            if self.try_link(call_id):
                return True
            if asyncio.get_running_loop().time() >= deadline:
                log_event("target_link_timeout", call_id=call_id)
                return False
            await asyncio.sleep(self.poll_s)
