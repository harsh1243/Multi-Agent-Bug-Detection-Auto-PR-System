"""Anthropic Claude LLM client with retry logic and tiered model selection."""

import asyncio
import json
import random
import re
from typing import Any, Optional

import anthropic

from config import settings


class LLMError(RuntimeError):
    """Raised when an LLM call fails after all retries."""


def extract_json(text: str) -> Any:
    """Best-effort parse of JSON from an LLM response.

    Tolerates ```json fences, leading prose, and trailing commentary by locating
    the first balanced JSON array/object. Returns the parsed value, or raises
    ValueError if nothing parseable is found.
    """
    if not text:
        raise ValueError("empty response")
    # Strip code fences.
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1).strip() if fenced else text.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced [...] or {...} span.
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = candidate.find(open_ch)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(candidate)):
            c = candidate[i]
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError("no parseable JSON found in response")


class LLMClient:
    """Tiered LLM client: Haiku for triage, Sonnet for reasoning."""

    def __init__(self):
        kwargs: dict[str, Any] = {"api_key": settings.anthropic_api_key}
        # Route through a custom Anthropic-compatible endpoint (e.g. Lightning AI
        # credits proxy) when ANTHROPIC_BASE_URL is configured.
        if settings.anthropic_base_url:
            kwargs["base_url"] = settings.anthropic_base_url
        self.client = anthropic.AsyncAnthropic(**kwargs)

    async def call(
        self,
        prompt: str,
        system: str = "",
        model_tier: str = "sonnet",
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> str:
        """Call Claude with exponential backoff retry."""
        model = (
            settings.claude_model_primary
            if model_tier == "sonnet"
            else settings.claude_model_triage
        )
        max_tokens = max_tokens or settings.llm_token_budget_routing

        create_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Some newer models deprecate `temperature` and 400 if it is present.
        if settings.llm_use_temperature:
            create_kwargs["temperature"] = temperature

        for attempt in range(settings.llm_max_retries):
            try:
                response = await asyncio.wait_for(
                    self.client.messages.create(**create_kwargs),
                    timeout=settings.llm_timeout,
                )
                return response.content[0].text if response.content else ""

            except Exception as e:
                delay = settings.llm_retry_base_delay * (2**attempt) + random.uniform(
                    0, 1
                )
                if attempt < settings.llm_max_retries - 1:
                    await asyncio.sleep(delay)
                else:
                    raise LLMError(f"LLM call failed after {settings.llm_max_retries} retries: {e}")

        return ""

    async def triage(self, prompt: str, max_tokens: int = 1024) -> str:
        """Quick triage call using Haiku."""
        return await self.call(prompt, model_tier="haiku", max_tokens=max_tokens, temperature=0.1)

    async def reason(self, prompt: str, system: str = "", max_tokens: int = 4096) -> str:
        """Deep reasoning call using Sonnet."""
        return await self.call(
            prompt, system=system, model_tier="sonnet", max_tokens=max_tokens, temperature=0.2
        )

    async def generate_code(self, prompt: str, system: str = "", max_tokens: int = 8192) -> str:
        """Code generation call using Sonnet."""
        code_system = (
            system
            or "You are an expert software engineer. Generate clean, correct, production-ready code. "
               "Only output the code changes in a clear diff format. Do not include explanations outside the diff."
        )
        return await self.call(
            prompt, system=code_system, model_tier="sonnet", max_tokens=max_tokens, temperature=0.1
        )

    async def call_json(
        self,
        prompt: str,
        system: str = "",
        model_tier: str = "haiku",
        max_tokens: int = 2048,
    ) -> Any:
        """Call Claude and parse a JSON value from the response.

        Returns the parsed JSON (list/dict). Raises ValueError if the model did
        not return parseable JSON, or LLMError if the API call itself failed.
        """
        json_system = (system + "\n\n" if system else "") + (
            "Respond with ONLY valid JSON. No prose, no markdown fences, no explanation."
        )
        raw = await self.call(
            prompt, system=json_system, model_tier=model_tier,
            max_tokens=max_tokens, temperature=0.0,
        )
        return extract_json(raw)


# Global instance
llm = LLMClient()
