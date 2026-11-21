"""Google Gemini structured outputs adapter using official google-genai Python SDK."""

from __future__ import annotations

from google import genai
from google.genai import types

from app.core.config import get_settings
from app.services.ai.base import LlmDraftResult, StructuredDraftPayload


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.gemini_api_key
        self.model_name = model or settings.gemini_model
        self.in_rate = settings.cost_rate_gemini_input_per_1k
        self.out_rate = settings.cost_rate_gemini_output_per_1k

    def generate_structured_draft(self, system_prompt: str, user_prompt: str) -> LlmDraftResult:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        options = types.HttpOptions(
            timeout=int(get_settings().model_timeout_seconds * 1000), retry_options=types.HttpRetryOptions(attempts=1),
        )
        with genai.Client(api_key=self.api_key, http_options=options) as client:
            response = client.models.generate_content(
                model=self.model_name,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=StructuredDraftPayload,
                    temperature=0.2,
                    max_output_tokens=1024,
                ),
            )
        parsed: StructuredDraftPayload | None = response.parsed
        if parsed is None:
            raise RuntimeError("Gemini structured output returned empty parsed payload")
        usage = getattr(response, "usage_metadata", None)
        in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
        out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
        est_cost = (in_tok / 1000.0) * self.in_rate + (out_tok / 1000.0) * self.out_rate
        return LlmDraftResult(
            payload=parsed,
            provider=self.name,
            model=self.model_name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            estimated_cost_usd=round(est_cost, 6),
        )
