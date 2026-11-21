"""Structured LLM drafting schema, provider contract, and cost estimation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field


class StructuredDraftPayload(BaseModel):
    subject: str = Field(min_length=3, max_length=300)
    body_paragraphs: list[str] = Field(min_length=1, max_length=6)
    cited_fact_ids: list[str] = Field(default_factory=list)
    language: str = "en"


@dataclass
class LlmDraftResult:
    payload: StructuredDraftPayload
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    error: str = ""


class LLMProvider(Protocol):
    name: str
    model_name: str

    def generate_structured_draft(self, system_prompt: str, user_prompt: str) -> LlmDraftResult:
        ...
