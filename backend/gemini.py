"""Gemini JSON calls for the judge, the patcher and sim-mode conversations.

Uses the google-genai SDK's models.generate_content with a JSON-schema response. The SDK's
own retries are turned off: its Interactions API silently waits out 429s, honouring retry
hints of many hours on an exhausted free tier, which made calls look hung. Here a daily
quota error fails at once (so callers fall back to the offline judge or rules), and only
short per-minute limits and server errors are retried.
"""

import asyncio
import json
from typing import Any

from google import genai
from google.genai import types

from .config import Settings

REQUEST_TIMEOUT_S = 90
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_WAIT_S = 30  # longer server-suggested waits aren't worth blocking a demo for


class GeminiError(Exception):
    pass


class GeminiQuotaExhausted(GeminiError):
    """The project's daily request quota for this model is used up."""


def _status(exc: Exception) -> int | None:
    for attr in ("code", "status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _quota_details(exc: Exception) -> tuple[bool, float | None]:
    """(is a per-day quota, suggested retry delay in seconds) from a 429's error details."""
    details = getattr(exc, "details", None)
    err = details.get("error", details) if isinstance(details, dict) else {}
    per_day, delay = False, None
    for d in err.get("details", []) if isinstance(err, dict) else []:
        for v in d.get("violations", []):
            if "PerDay" in str(v.get("quotaId", "")):
                per_day = True
        if "retryDelay" in d:
            try:
                delay = float(str(d["retryDelay"]).rstrip("s"))
            except ValueError:
                pass
    return per_day, delay


class GeminiJSON:
    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self.client = client or genai.Client(
            api_key=settings.gemini_api_key or None,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_S * 1000,
                                           retry_options=types.HttpRetryOptions(attempts=1)),
        )

    async def ask(self, *, model: str, system: str, prompt: str, schema: dict[str, Any],
                  thinking_level: str = "medium", attempts: int = 4) -> dict[str, Any]:
        """One JSON reply. Retries per-minute rate limits and server errors (2, 4, 8 s, or the
        server's hint up to 30 s); a used-up daily quota raises GeminiQuotaExhausted at once."""
        for attempt in range(attempts):
            try:
                return await asyncio.wait_for(self._ask_once(model, system, prompt, schema, thinking_level),
                                              REQUEST_TIMEOUT_S + 5)
            except Exception as exc:
                status = _status(exc)
                if status == 429:
                    per_day, delay = _quota_details(exc)
                    if per_day or (delay is not None and delay > MAX_RETRY_WAIT_S):
                        raise GeminiQuotaExhausted(f"{model}: Gemini quota used up; try again later, "
                                                   "use another model or enable billing") from exc
                if status not in RETRY_STATUS or attempt == attempts - 1:
                    raise
                await asyncio.sleep(2 ** (attempt + 1))
        raise AssertionError("unreachable")

    async def _ask_once(self, model: str, system: str, prompt: str, schema: dict[str, Any],
                        thinking_level: str) -> dict[str, Any]:
        response = await self.client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=schema,
                thinking_config=types.ThinkingConfig(thinking_level=thinking_level.upper()),
            ),
        )
        if not response.text:
            raise GeminiError("empty response (the reply may have been blocked)")
        return json.loads(response.text)
