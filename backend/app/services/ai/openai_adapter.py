"""OpenAI structured outputs adapter using official openai Python SDK."""

from __future__ import annotations

from openai import OpenAI

from app.core.config import get_settings
from app.services.ai.base import LlmDraftResult, StructuredDraftPayload


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.model_name = model or settings.openai_model
        self.in_rate = settings.cost_rate_openai_input_per_1k
        self.out_rate = settings.cost_rate_openai_output_per_1k

    def generate_structured_draft(self, system_prompt: str, user_prompt: str) -> LlmDraftResult:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        with OpenAI(api_key=self.api_key, timeout=get_settings().model_timeout_seconds, max_retries=0) as client:
            completion = client.chat.completions.parse(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=StructuredDraftPayload,
                temperature=0.2,
                max_completion_tokens=1024,
            )
        msg = completion.choices[0].message
        if msg.refusal or msg.parsed is None:
            raise RuntimeError(f"OpenAI structured draft refused or empty: {msg.refusal}")
        usage = completion.usage
        in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
        est_cost = (in_tok / 1000.0) * self.in_rate + (out_tok / 1000.0) * self.out_rate
        return LlmDraftResult(
            payload=msg.parsed,
            provider=self.name,
            model=self.model_name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            estimated_cost_usd=round(est_cost, 6),
        )
