"""Gemini calls for the judge and the patcher, via the google-genai Interactions API
(google-genai >= 2.3), with the reply constrained to a JSON schema."""

import json
from typing import Any

from google import genai

from .config import Settings


class GeminiError(Exception):
    pass


class GeminiJSON:
    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self.client = client or genai.Client(api_key=settings.gemini_api_key or None)

    async def ask(self, *, model: str, system: str, prompt: str, schema: dict[str, Any],
                  thinking_level: str = "medium") -> dict[str, Any]:
        interaction = await self.client.aio.interactions.create(
            model=model,
            system_instruction=system,
            input=prompt,
            response_mime_type="application/json",
            response_format={"type": "text", "mime_type": "application/json", "schema": schema},
            generation_config={"thinking_level": thinking_level},
            store=False,  # don't keep call transcripts on Google's side
            timeout=120,
        )
        if interaction.status != "completed":
            raise GeminiError(f"interaction ended with status {interaction.status}")
        if not interaction.output_text:
            raise GeminiError("empty response")
        return json.loads(interaction.output_text)
