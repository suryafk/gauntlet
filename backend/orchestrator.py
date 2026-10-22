"""Fans out a run's calls under a concurrency cap and tracks each call to a terminal status.

A call holds its semaphore slot from dialing until its terminal webhook arrives (or it
times out). A dial error or missing webhook marks that call failed; the run always completes.
In real mode, a completed call is then joined to the target agent's side (TargetLinker)
before the finished hooks (scoring) see it.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from .bolna_adapter import BolnaAdapter
from .config import Settings
from .events import EventBus, log_event
from .models import Store, now
from .target_link import TargetLinker

CallFinished = Callable[[dict[str, Any]], Awaitable[None]]
MAX_ATTEMPTS = 3  # per call, counting the first dial; keeps retries from burning credits


class RunRejected(ValueError):
    pass


class Orchestrator:
    def __init__(self, store: Store, adapter: BolnaAdapter, settings: Settings, bus: EventBus,
                 personas: dict[str, dict[str, Any]], linker: TargetLinker | None = None):
        self.store = store
        self.adapter = adapter
        self.settings = settings
        self.bus = bus
        self.personas = personas
        self.linker = linker
        self.sem = asyncio.Semaphore(settings.max_concurrency)
        self.on_call_finished: list[CallFinished] = []  # the scorer hooks in here (stage 3)
        self._waiters: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task] = set()
        adapter.bind(self.handle_webhook)

    # runs
    def start_run(self, persona_ids: list[str], calls_per_persona: int, prompt_version: str = "v1",
                  parent_run_id: str | None = None) -> dict:
        unknown = [p for p in persona_ids if p not in self.personas]
        if unknown:
            raise RunRejected(f"unknown personas: {unknown}")
        if not persona_ids or calls_per_persona < 1:
            raise RunRejected("pick at least one persona and one call per persona")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", prompt_version) or not (
                self.settings.target_prompts_dir / f"{prompt_version}.md").exists():
            raise RunRejected(f"unknown target prompt version {prompt_version!r}")
        total = len(persona_ids) * calls_per_persona
        cap = self.settings.run_call_cap
        if total > cap:
            raise RunRejected(f"{total} calls exceeds the per-run cap of {cap}")

        run = self.store.create_run(cap, prompt_version, parent_run_id, mode=self.settings.bolna_mode)
        calls = [self.store.create_call(run["id"], pid) for pid in persona_ids for _ in range(calls_per_persona)]
        log_event("run_created", run_id=run["id"], calls=total, prompt_version=prompt_version)
        self._spawn(self._run(run["id"], calls, prompt_version))
        return run

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, run_id: str, calls: list[dict], prompt_version: str) -> None:
        self.store.set_run_status(run_id, "running")
        self.bus.publish(run_id, {"type": "run", "run_id": run_id, "status": "running"})
        try:
            await self.adapter.prepare_run(prompt_version)
        except Exception as exc:  # noqa: BLE001 - e.g. the target prompt could not be applied
            log_event("prepare_run_failed", run_id=run_id, error=repr(exc))
            for c in calls:
                self._fail(c, f"run setup failed: {exc}")
                await self._notify_finished(c["id"])
            calls = []
        await asyncio.gather(*(self._place_call(c, prompt_version) for c in calls), return_exceptions=True)
        self._maybe_complete(run_id)

    def _maybe_complete(self, run_id: str) -> None:
        """A run is completed once every call is terminal (and so scored); retries can reopen it."""
        if all(c["status"] in ("completed", "failed") for c in self.store.list_calls(run_id)):
            self.store.set_run_status(run_id, "completed")
            self.bus.publish(run_id, {"type": "run", "run_id": run_id, "status": "completed"})
            log_event("run_completed", run_id=run_id)

    def retry_call(self, call_id: str) -> dict:
        """Dial a failed call again inside its own run, so the run's pass rate updates in place."""
        call = self.store.get_call(call_id)
        if call is None:
            raise RunRejected("call not found")
        if call["status"] != "failed":
            raise RunRejected("only failed calls can be retried")
        if call["attempts"] >= MAX_ATTEMPTS:
            raise RunRejected(f"this call has already been tried {call['attempts']} times")
        run = self.store.get_run(call["run_id"])
        self.store.reset_call_for_retry(call_id)
        self.store.set_run_status(run["id"], "running")
        self.bus.publish(run["id"], {"type": "run", "run_id": run["id"], "status": "running"})
        self._publish_call(call_id)
        log_event("call_retry", run_id=run["id"], call_id=call_id, attempt=call["attempts"] + 1)
        self._spawn(self._retry(call_id, run["id"], run["target_prompt_version"]))
        return self.store.get_call(call_id)

    async def _retry(self, call_id: str, run_id: str, prompt_version: str) -> None:
        call = self.store.get_call(call_id)
        try:
            await self.adapter.prepare_run(prompt_version)
        except Exception as exc:  # noqa: BLE001
            self._fail(call, f"run setup failed: {exc}")
            await self._notify_finished(call_id)
        else:
            await self._place_call(call, prompt_version)
        self._maybe_complete(run_id)

    async def _place_call(self, call: dict, prompt_version: str) -> None:
        call_id = call["id"]
        persona = self.personas[call["persona_id"]]
        async with self.sem:
            done = asyncio.Event()
            self._waiters[call_id] = done  # registered before dialing so an early webhook is not lost
            try:
                self._set_status(call, "dialing", started_at=now())
                bolna_id = await self.adapter.start_call(
                    call_id=call_id, persona=persona, to_number=self.settings.target_number,
                    prompt_version=prompt_version,
                )
                self.store.update_call(call_id, bolna_call_id=bolna_id)
                await asyncio.wait_for(done.wait(), timeout=self.settings.call_timeout_s)
            except TimeoutError:
                self._fail(call, f"no terminal webhook within {self.settings.call_timeout_s:g}s")
            except Exception as exc:  # noqa: BLE001 - any dial problem fails this call only
                self._fail(call, f"{type(exc).__name__}: {exc}")
            finally:
                self._waiters.pop(call_id, None)
        # Slot released; now wait for the target's side without blocking other calls.
        if self.linker and self.adapter.needs_target_link and self.store.get_call(call_id)["status"] == "completed":
            if await self.linker.link(call_id):
                self._publish_call(call_id)
        await self._notify_finished(call_id)

    # call status
    def _set_status(self, call: dict, status: str, **fields: Any) -> None:
        self.store.update_call(call["id"], status=status, **fields)
        self._publish_call(call["id"])

    def _publish_call(self, call_id: str) -> None:
        call = self.store.get_call(call_id)
        self.bus.publish(call["run_id"], {
            "type": "call", "call_id": call_id, "persona_id": call["persona_id"], "status": call["status"],
        })
        log_event("call_status", run_id=call["run_id"], call_id=call_id, status=call["status"],
                  latency_ms=call.get("first_response_ms"), error=call.get("error"))

    def _fail(self, call: dict, error: str) -> None:
        if self.store.finish_call_if_open(call["id"], status="failed", error=error):
            self._publish_call(call["id"])

    async def _notify_finished(self, call_id: str) -> None:
        call = self.store.get_call(call_id)
        for hook in self.on_call_finished:
            try:
                await hook(call)
            except Exception as exc:  # noqa: BLE001 - a scoring bug must not break the run
                log_event("hook_error", call_id=call_id, error=repr(exc))

    # webhooks
    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Idempotent: a repeated terminal event for the same call is ignored."""
        if self.linker and self.linker.is_target_execution(payload):
            return self.linker.record_execution(payload)
        result = self.adapter.parse_webhook(payload)
        call = None
        if result.bolna_call_id:
            call = self.store.get_call_by_bolna_id(result.bolna_call_id)
        if call is None and result.gauntlet_call_id:
            call = self.store.get_call(result.gauntlet_call_id)
            if call is not None and not call.get("bolna_call_id") and result.bolna_call_id:
                self.store.update_call(call["id"], bolna_call_id=result.bolna_call_id)
        if call is None:
            log_event("webhook_unmatched", bolna_call_id=result.bolna_call_id, status=result.status)
            return {"ok": True, "matched": False}

        if call["status"] in ("completed", "failed"):
            return {"ok": True, "matched": True, "duplicate": True}

        if not result.terminal:
            if result.status != call["status"]:
                self._set_status(call, result.status)
            return {"ok": True, "matched": True, "duplicate": False}

        changed = self.store.finish_call_if_open(
            call["id"],
            status=result.status,
            transcript=result.transcript,
            audio_url=result.audio_url,
            first_response_ms=result.first_response_ms,
            duration_s=result.duration_s,
            tool_payload=result.tool_payload,
            error=result.error,
        )
        if changed:
            self._publish_call(call["id"])
            waiter = self._waiters.get(call["id"])
            if waiter:
                waiter.set()  # releases the call's semaphore slot
        return {"ok": True, "matched": True, "duplicate": not changed}

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await self.adapter.aclose()
