"""FastAPI app: runs, calls, live stream and the Bolna webhook."""

import asyncio
import hmac
import json
import logging
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .bolna_adapter import make_adapter
from .config import ROOT, Settings
from .events import EventBus, log_event
from .models import Store
from .orchestrator import Orchestrator, RunRejected
from .patcher import Patcher
from .personas import load_personas
from .scorer import Scorer, compare_runs, run_summary
from .seed import seed_if_empty
from .spotcheck import JUDGED, agreement, spotcheck
from .target_link import TargetLinker

logging.basicConfig(level=logging.INFO, format="%(message)s")


def create_app(settings: Settings | None = None, adapter=None, judge=None, patcher=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.database_url)
        personas = load_personas(settings.personas_dir)
        for p in personas.values():
            store.upsert_persona(p)
        if settings.seed_on_empty:
            seed_if_empty(store, settings.seed_path)
        bus = EventBus()
        linker = TargetLinker(store, settings)
        orch = Orchestrator(store, adapter or make_adapter(settings), settings, bus, personas, linker)
        app.state.store, app.state.bus, app.state.orch, app.state.personas = store, bus, orch, personas
        app.state.linker = linker
        scorer = Scorer(store, bus, settings, personas, judge)
        orch.on_call_finished.append(scorer.score_call)
        app.state.scorer = scorer
        app.state.patcher = Patcher(store, settings, personas, patcher)
        if settings.bolna_mode == "sim" and not settings.gemini_api_key:
            log_event("warning", message="BOLNA_MODE=sim needs GEMINI_API_KEY; every call will fail without it")
        if settings.bolna_mode == "real":
            if not settings.webhook_secret:
                log_event("warning", message="WEBHOOK_SECRET is empty; webhooks are not authenticated")
            if not settings.bolna_target_agent_id:
                log_event("warning", message="no target agent id; run scripts/setup_bolna.py")
        log_event("startup", mode=settings.bolna_mode, personas=len(personas), judge=scorer.judge.name)
        yield
        await orch.shutdown()
        store.close()

    # Every route is async: the Store's single SQLite connection must only be used from the
    # event loop thread, never from FastAPI's threadpool (where plain `def` routes run).
    app = FastAPI(title="Gauntlet", lifespan=lifespan)
    app.state.settings = settings

    class RunRequest(BaseModel):
        persona_ids: list[str] = Field(min_length=1)
        calls_per_persona: int = Field(ge=1)
        prompt_version: str = "v1"

    def _check_secret(request: Request) -> None:
        """Accept the shared secret as an X-Webhook-Secret header or a ?secret= query param."""
        if not settings.webhook_secret:
            return
        given = request.headers.get("x-webhook-secret") or request.query_params.get("secret") or ""
        if not hmac.compare_digest(given, settings.webhook_secret):
            raise HTTPException(401, "bad webhook secret")

    class RerunRequest(BaseModel):
        patch_id: str

    class ReviewRequest(BaseModel):
        labels: dict[str, bool]  # metric -> did the agent pass, in the reviewer's judgement
        note: str = ""

    def _run_or_404(request: Request, run_id: str) -> dict:
        run = request.app.state.store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return run

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "mode": settings.bolna_mode}

    @app.get("/personas")
    async def personas(request: Request) -> list[dict[str, Any]]:
        return [
            {k: p[k] for k in ("id", "name", "language_mix", "weakness") if k in p}
            for p in request.app.state.personas.values()
        ]

    @app.get("/config")
    async def public_config(request: Request) -> dict[str, Any]:
        # Safe to show in the UI: no keys.
        return {
            "mode": settings.bolna_mode,
            "target_number": settings.target_number,
            "run_call_cap": settings.run_call_cap,
            "max_concurrency": settings.max_concurrency,
            "judge": request.app.state.scorer.judge.name,
            "patcher": request.app.state.patcher.patcher.name,
            "target_agent_configured": bool(settings.bolna_target_agent_id),
            "caller_agent_configured": bool(settings.bolna_caller_agent_id or settings.persona_caller_agent_ids),
        }

    @app.post("/runs", status_code=201)
    async def create_run(body: RunRequest, request: Request) -> dict[str, Any]:
        try:
            return request.app.state.orch.start_run(body.persona_ids, body.calls_per_persona, body.prompt_version)
        except RunRejected as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/runs")
    async def list_runs(request: Request) -> list[dict[str, Any]]:
        return request.app.state.store.list_runs()

    @app.get("/overview")
    async def overview(request: Request) -> dict[str, Any]:
        """Home-screen numbers, all derived from stored runs."""
        store = request.app.state.store
        runs = []
        for r in store.list_runs():
            s = run_summary(store, r["id"])
            runs.append({k: r[k] for k in ("id", "created_at", "status", "target_prompt_version", "parent_run_id",
                                           "seeded", "mode")}
                        | {k: s[k] for k in ("pass_rate", "total_calls", "scored_calls", "failed_calls")})
        by_id = {r["id"]: r for r in runs}
        latest_fix = None
        for r in runs:  # newest first
            parent = by_id.get(r["parent_run_id"] or "")
            if parent and r["status"] == parent["status"] == "completed":
                latest_fix = {"before": parent, "after": r}
                break
        labels = agree = 0
        for r in runs:
            a = agreement(store, r["id"])
            labels += a["labels"]
            agree += round((a["rate"] or 0) * a["labels"])
        return {
            "runs": runs[:12],
            "total_runs": len(runs),
            "total_calls": sum(r["total_calls"] for r in runs),
            "personas": len(request.app.state.personas),
            "latest_fix": latest_fix,
            "review": {"labels": labels, "rate": agree / labels if labels else None},
        }

    @app.get("/runs/{run_id}/report")
    async def run_report(run_id: str, request: Request) -> Response:
        """Everything about one run as a downloadable JSON file."""
        run = _run_or_404(request, run_id)
        store = request.app.state.store
        scores: dict[str, list] = defaultdict(list)
        for sc in store.scores_for_run(run_id):
            scores[sc["call_id"]].append({k: sc[k] for k in ("metric", "passed", "reason", "judge", "value")})
        report = {
            "run": run,
            "summary": run_summary(store, run_id),
            "calls": [{**c, "scores": scores.get(c["id"], [])} for c in store.list_calls(run_id)],
            "patches": store.patches_for_run(run_id),
        }
        return Response(json.dumps(report, indent=2, ensure_ascii=False, default=str), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="gauntlet-run-{run_id}.json"'})

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        run = _run_or_404(request, run_id)
        store = request.app.state.store
        calls = store.list_calls(run_id)
        by_persona: dict[str, Counter] = defaultdict(Counter)
        for c in calls:
            by_persona[c["persona_id"]][c["status"]] += 1
        return {
            **run,
            **run_summary(store, run_id),
            "status_counts": dict(Counter(c["status"] for c in calls)),
            "by_persona": {pid: dict(counts) for pid, counts in by_persona.items()},
        }

    @app.get("/runs/{run_id}/calls")
    async def get_calls(run_id: str, request: Request) -> list[dict[str, Any]]:
        _run_or_404(request, run_id)
        store = request.app.state.store
        scores: dict[str, list] = defaultdict(list)
        for s in store.scores_for_run(run_id):
            scores[s["call_id"]].append(s)
        return [{**c, "scores": scores.get(c["id"], [])} for c in store.list_calls(run_id)]

    @app.get("/runs/{run_id}/spotcheck")
    async def get_spotcheck(run_id: str, request: Request, n: int = 10) -> dict[str, Any]:
        _run_or_404(request, run_id)
        return spotcheck(request.app.state.store, run_id, request.app.state.personas, max(1, min(n, 50)))

    @app.post("/calls/{call_id}/review")
    async def review_call(call_id: str, body: ReviewRequest, request: Request) -> dict[str, Any]:
        store = request.app.state.store
        call = store.get_call(call_id)
        if call is None:
            raise HTTPException(404, "call not found")
        unknown = set(body.labels) - set(JUDGED)
        if unknown or not body.labels:
            raise HTTPException(400, f"label these metrics: {JUDGED}")
        for metric, passed in body.labels.items():
            store.save_review(call_id, metric, passed, body.note.strip())
        return agreement(store, call["run_id"])

    @app.post("/calls/{call_id}/retry")
    async def retry_call(call_id: str, request: Request) -> dict[str, Any]:
        try:
            return request.app.state.orch.retry_call(call_id)
        except RunRejected as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/runs/{run_id}/retry-failed")
    async def retry_failed(run_id: str, request: Request) -> dict[str, Any]:
        _run_or_404(request, run_id)
        orch, retried, skipped = request.app.state.orch, [], []
        for c in request.app.state.store.list_calls(run_id):
            if c["status"] != "failed":
                continue
            try:
                retried.append(orch.retry_call(c["id"])["id"])
            except RunRejected as exc:
                skipped.append({"call_id": c["id"], "reason": str(exc)})
        return {"retried": retried, "skipped": skipped}

    @app.get("/runs/{run_id}/patches")
    async def list_patches(run_id: str, request: Request) -> list[dict[str, Any]]:
        _run_or_404(request, run_id)
        return request.app.state.store.patches_for_run(run_id)

    @app.post("/runs/{run_id}/patch", status_code=201)
    async def generate_patch(run_id: str, request: Request) -> dict[str, Any]:
        run = _run_or_404(request, run_id)
        if run["status"] != "completed":
            raise HTTPException(409, "wait for the run to finish before patching")
        try:
            return await request.app.state.patcher.generate(run)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/runs/{run_id}/rerun", status_code=201)
    async def rerun(run_id: str, body: RerunRequest, request: Request) -> dict[str, Any]:
        """Re-run the same personas and call counts on the patched prompt, linked by parent_run_id."""
        _run_or_404(request, run_id)
        store = request.app.state.store
        patch = store.get_patch(body.patch_id)
        if patch is None or patch["run_id"] != run_id:
            raise HTTPException(404, "patch not found for this run")
        if patch.get("applied_run_id"):
            return store.get_run(patch["applied_run_id"])
        counts = Counter(c["persona_id"] for c in store.list_calls(run_id))
        version = request.app.state.patcher.write_version(patch)
        try:
            new_run = request.app.state.orch.start_run(list(counts), max(counts.values()), version, run_id)
        except RunRejected as exc:
            raise HTTPException(400, str(exc)) from exc
        store.mark_patch_applied(patch["id"], new_run["id"], version)
        return new_run

    @app.get("/compare")
    async def compare(a: str, b: str, request: Request) -> dict[str, Any]:
        _run_or_404(request, a)
        _run_or_404(request, b)
        store = request.app.state.store
        return {**compare_runs(store, a, b), "patch": store.patch_applied_in(b)}

    @app.get("/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request) -> StreamingResponse:
        run = _run_or_404(request, run_id)
        bus: EventBus = request.app.state.bus

        async def events():
            q = bus.subscribe(run_id)
            try:
                yield f"data: {json.dumps({'type': 'run', 'run_id': run_id, 'status': run['status']})}\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                bus.unsubscribe(run_id, q)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/webhooks/bolna")
    async def bolna_webhook(request: Request) -> dict:
        _check_secret(request)
        payload = await request.json()
        return await request.app.state.orch.handle_webhook(payload)

    @app.post("/tools/schedule_payment")
    async def schedule_payment(request: Request) -> dict:
        """The target agent's custom function. Records what it extracted; always 'succeeds'."""
        _check_secret(request)
        params = await request.json()
        if isinstance(params, str):  # some tool callers send the param string JSON-encoded
            params = json.loads(params)
        return request.app.state.linker.record_tool_call(params)

    frontend = ROOT / "frontend"
    if (frontend / "index.html").exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")

    return app


app = create_app()
