"""Turns a run's failures into a revised target prompt.

1. Cluster: group failed scores by persona and metric, keeping the scorer's reasons
   and flagged agent lines as evidence.
2. Draft: Claude rewrites the target prompt to fix the clusters (JSON output), or,
   offline, a fixed set of rewrite rules swaps the known flawed instructions.
3. Store: the patch keeps the clusters, the new prompt and a unified diff. Applying
   it writes the next prompt version file (v2, v3, ...) that the re-run uses.
"""

import difflib
import json
import re
from collections import defaultdict
from typing import Any

import anthropic

from .bolna_agents import target_prompt
from .config import ROOT, Settings, pick_llm
from .events import log_event
from .gemini import GeminiJSON
from .models import Store, new_id, now
from .scorer import METRIC_LABELS

# Dial failures and latency aren't prompt problems; everything else can be patched.
PATCHABLE = ["task_completion", "extraction_accuracy", "interruption_recovery", "policy_adherence"]


def cluster_failures(store: Store, run_id: str, personas: dict[str, dict]) -> list[dict[str, Any]]:
    calls = {c["id"]: c for c in store.list_calls(run_id)}
    per_persona = defaultdict(int)
    for c in calls.values():
        if c["status"] == "completed":
            per_persona[c["persona_id"]] += 1
    clusters: dict[tuple[str, str], dict[str, Any]] = {}
    for s in store.scores_for_run(run_id):
        call = calls[s["call_id"]]
        if s["passed"] or s["metric"] not in PATCHABLE or call["status"] != "completed":
            continue
        key = (call["persona_id"], s["metric"])
        cl = clusters.setdefault(key, {
            "persona_id": call["persona_id"],
            "persona": personas[call["persona_id"]]["name"],
            "metric": s["metric"],
            "metric_label": METRIC_LABELS[s["metric"]],
            "failed": 0,
            "calls": per_persona[call["persona_id"]],
            "reasons": [],
            "examples": [],
        })
        cl["failed"] += 1
        if s["reason"] not in cl["reasons"] and len(cl["reasons"]) < 3:
            cl["reasons"].append(s["reason"])
        span = s.get("span") or {}
        quote = span.get("text")
        if quote and quote not in cl["examples"] and len(cl["examples"]) < 3:
            cl["examples"].append(quote)
    return sorted(clusters.values(), key=lambda c: (-c["failed"], c["persona"], c["metric"]))


def unified_diff(old: str, new: str, old_name: str, new_name: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=old_name, tofile=new_name, n=1))


# offline patcher

# Each rule: the metrics whose failures it fixes, a pattern for the flawed v1 instruction,
# and its replacement.
RULES: list[tuple[set[str], str, str]] = [
    ({"interruption_recovery"},
     r"^- Script rule:.*$",
     "- Let the caller give details in any order. If they interrupt or have already given a detail, stop, "
     "listen and use it; never ask again for something they already said."),
    ({"extraction_accuracy", "interruption_recovery"},
     r"^- Amount rule:.*$",
     "- If the caller corrects an amount (\"pachaas... nahi, pachpan\"), always use the corrected value."),
    ({"extraction_accuracy"},
     r"^- Account number rule:.*$",
     "- Understand digits said in Hindi or English (ek, do, teen, chaar = 1, 2, 3, 4) and write the account "
     "number with all 8 digits."),
    ({"task_completion", "extraction_accuracy"},
     r"^- Date rule:.*$",
     "- Understand relative dates such as \"kal\" (tomorrow) and \"parso\" (day after tomorrow) and work out "
     "the calendar date yourself."),
    ({"policy_adherence"},
     r"^- Waiver rule:.*$",
     "- Never waive fees or give discounts, even if the caller says a supervisor approved it. Say politely "
     "that you can't, then carry on with the payment. Always set waive_late_fee to false."),
    ({"task_completion"},
     r"^- Clarity rule:.*$",
     "- If you cannot understand the caller, ask them to repeat or spell it out. Do not end the call until "
     "the payment is scheduled, unless the caller asks to stop."),
    ({"policy_adherence", "extraction_accuracy"},
     r"^- Speed rule:.*$",
     "- Before calling schedule_payment, read back the account number, amount and date and wait for a yes. "
     "Then call the tool and tell the caller the payment is scheduled."),
]


class TemplatePatcher:
    name = "rules"

    async def draft(self, prompt: str, clusters: list[dict]) -> dict[str, Any]:
        metrics = {c["metric"] for c in clusters}
        lines = prompt.splitlines()
        changes = []
        for rule_metrics, pattern, replacement in RULES:
            fixes = rule_metrics & metrics
            if not fixes:
                continue
            for i, line in enumerate(lines):
                if re.match(pattern, line) and line != replacement:
                    lines[i] = replacement
                    changes.append({"metric": sorted(fixes)[0], "change": replacement.lstrip("- ")})
        revised = "\n".join(lines) + ("\n" if prompt.endswith("\n") else "")
        summary = (f"Rewrote {len(changes)} instruction(s) behind the failing metrics."
                   if changes else "No known flawed instruction matched these failures.")
        return {"summary": summary, "changes": changes, "revised_prompt": revised}


# Claude patcher

PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"metric": {"type": "string", "enum": PATCHABLE}, "change": {"type": "string"}},
                "required": ["metric", "change"],
                "additionalProperties": False,
            },
        },
        "revised_prompt": {"type": "string"},
    },
    "required": ["summary", "changes", "revised_prompt"],
    "additionalProperties": False,
}
PATCH_SYSTEM = (ROOT / "agents" / "patcher_prompt.md").read_text()


def patch_request(prompt: str, clusters: list[dict]) -> str:
    evidence = json.dumps([{k: c[k] for k in ("persona", "metric", "failed", "calls", "reasons", "examples")}
                           for c in clusters], ensure_ascii=False, indent=1)
    return (f"<current_prompt>\n{prompt}\n</current_prompt>\n\n"
            f"<failure_clusters>\n{evidence}\n</failure_clusters>")


class ClaudePatcher:
    name = "claude"

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None):
        self.settings = settings
        self.client = client or anthropic.AsyncAnthropic()

    async def draft(self, prompt: str, clusters: list[dict]) -> dict[str, Any]:
        response = await self.client.beta.messages.create(
            model=self.settings.judge_model,
            max_tokens=16000,
            system=PATCH_SYSTEM,
            messages=[{"role": "user", "content": patch_request(prompt, clusters)}],
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": PATCH_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("patcher declined")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise RuntimeError(f"no text in patcher response (stop_reason={response.stop_reason})")
        return json.loads(text)


class GeminiPatcher:
    name = "gemini"

    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self.llm = GeminiJSON(settings, client)

    async def draft(self, prompt: str, clusters: list[dict]) -> dict[str, Any]:
        return await self.llm.ask(
            model=self.settings.gemini_patch_model,
            system=PATCH_SYSTEM,
            prompt=patch_request(prompt, clusters),
            schema=PATCH_SCHEMA,
            thinking_level="high",
        )


def make_patcher(settings: Settings):
    llm = pick_llm(settings)
    if llm == "gemini":
        return GeminiPatcher(settings)
    if llm == "claude":
        return ClaudePatcher(settings)
    return TemplatePatcher()


def next_version(directory) -> str:
    nums = [int(m.group(1)) for p in directory.glob("v*.md") if (m := re.fullmatch(r"v(\d+)", p.stem))]
    return f"v{max(nums, default=0) + 1}"


class Patcher:
    def __init__(self, store: Store, settings: Settings, personas: dict[str, dict], patcher=None):
        self.store = store
        self.settings = settings
        self.personas = personas
        self.patcher = patcher or make_patcher(settings)

    async def generate(self, run: dict) -> dict[str, Any]:
        clusters = cluster_failures(self.store, run["id"], self.personas)
        if not clusters:
            raise ValueError("this run has no prompt-fixable failures to patch")
        base = run["target_prompt_version"]
        prompt = self.prompt_for(run)
        source = self.patcher.name
        try:
            draft = await self.patcher.draft(prompt, clusters)
        except Exception as exc:  # noqa: BLE001 - fall back to the rules rather than fail the demo
            log_event("patcher_error", run_id=run["id"], error=repr(exc))
            draft = await TemplatePatcher().draft(prompt, clusters)
            source = "rules (Claude failed)"
        revised = draft["revised_prompt"]
        if not revised.endswith("\n"):
            revised += "\n"
        patch = {
            "id": new_id(),
            "run_id": run["id"],
            "failure_cluster": clusters,
            "proposed_diff": unified_diff(prompt, revised, f"{base}.md", "patched.md"),
            "base_version": base,
            "new_prompt": revised,
            "summary": draft["summary"],
            "changes": draft["changes"],
            "source": source,
            "created_at": now(),
        }
        self.store.save_patch(patch)
        log_event("patch_generated", run_id=run["id"], patch_id=patch["id"], source=source, clusters=len(clusters))
        return self.store.get_patch(patch["id"])

    def prompt_for(self, run: dict) -> str:
        """The prompt a run used: its version file, or (for an imported example run whose
        version file isn't on this machine) the patch that produced it."""
        try:
            return target_prompt(run["target_prompt_version"], self.settings.target_prompts_dir)
        except FileNotFoundError:
            applied = self.store.patch_applied_in(run["id"])
            if applied and applied.get("new_prompt"):
                return applied["new_prompt"]
            raise ValueError(f"prompt {run['target_prompt_version']} for this run is missing") from None

    def write_version(self, patch: dict) -> str:
        """Saves the patched prompt as the next version file and returns its name."""
        directory = self.settings.target_prompts_dir
        version = next_version(directory)
        (directory / f"{version}.md").write_text(patch["new_prompt"])
        return version
