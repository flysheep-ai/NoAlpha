from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import urllib.request
import urllib.error

from noalpha.llm.config import LLMConfig
from noalpha.llm.schemas import LLMMessage, LLMResponse, LLMUsage

logger = logging.getLogger(__name__)


class LLMStructuredOutputError(RuntimeError):
    """The model returned output that cannot be parsed against the schema
    even after a retry. Callers must treat this as LLM_SIGNAL_UNAVAILABLE,
    never as a crash of the trading loop."""


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, messages: list[LLMMessage], **kwargs: Any) -> LLMResponse:
        ...

    @abstractmethod
    def generate_structured(
        self, messages: list[LLMMessage], output_schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        ...


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._usage: LLMUsage | None = None

    @property
    def usage(self) -> LLMUsage | None:
        return self._usage

    def generate(self, messages: list[LLMMessage], **kwargs: Any) -> LLMResponse:
        temperature = kwargs.get("temperature", self.config.temperature)
        max_tokens = kwargs.get("max_tokens", self.config.max_tokens)
        timeout = kwargs.get("timeout", self.config.timeout)

        payload = {
            "model": self.config.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        retries = 0
        max_retries = kwargs.get("max_retries", 2)
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                started = time.monotonic()
                body = json.dumps(payload).encode("utf-8")
                url = f"{self.config.base_url.rstrip('/')}/chat/completions"
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.config.api_key}",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                latency = (time.monotonic() - started) * 1000

                choice = raw["choices"][0]
                content = choice["message"]["content"]
                usage_data = raw.get("usage", {})
                self._usage = LLMUsage(
                    input_tokens=usage_data.get("prompt_tokens", 0),
                    output_tokens=usage_data.get("completion_tokens", 0),
                    model=self.config.model,
                    latency_ms=latency,
                    retry_count=retries,
                )
                return LLMResponse(
                    content=content,
                    input_tokens=usage_data.get("prompt_tokens", 0),
                    output_tokens=usage_data.get("completion_tokens", 0),
                    model=self.config.model,
                    latency_ms=latency,
                )
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_error = exc
                retries = attempt
                if attempt < max_retries:
                    backoff = 2 ** attempt
                    logger.warning(
                        "LLM call failed (attempt %s/%s), retrying in %ss: %s",
                        attempt + 1, max_retries + 1, backoff, exc,
                    )
                    time.sleep(backoff)
        raise RuntimeError(
            f"LLM provider failed after {max_retries + 1} attempts: {last_error}"
        )

    def generate_structured(
        self, messages: list[LLMMessage], output_schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        schema_desc = json.dumps(output_schema, indent=2)
        instruction = (
            "You MUST respond with valid JSON that conforms to the following schema. "
            "Do NOT include any text outside the JSON object.\n\n"
            f"Schema:\n{schema_desc}"
        )
        sys_msg = messages[0] if messages and messages[0].role == "system" else None
        if sys_msg:
            updated_system = LLMMessage(role="system", content=f"{sys_msg.content}\n\n{instruction}")
            messages = [updated_system] + messages[1:]
        else:
            messages = [LLMMessage(role="system", content=instruction)] + messages

        gen_kwargs = {k: v for k, v in kwargs.items() if k != "temperature"}
        temperature = kwargs.get("temperature", 0.1)
        last_error: Exception | None = None
        # Attempt 1 with configured max_tokens; on parse failure (typically a
        # truncated response) retry once with double the budget and a reminder.
        for attempt, max_tokens in enumerate((self.config.max_tokens, self.config.max_tokens * 2)):
            resp = self.generate(messages, temperature=temperature,
                                 max_tokens=max_tokens, **gen_kwargs)
            raw = resp.content.strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = [line for line in lines if not line.startswith("```")]
                raw = "\n".join(lines).strip()
            try:
                parsed = json.loads(raw)
                self._validate_structured_output(parsed, output_schema)
                return parsed
            except json.JSONDecodeError as exc:
                last_error = exc
                logger.warning("structured output parse failed (attempt %s, %s output tokens): %s",
                               attempt + 1, resp.output_tokens, exc)
            except LLMStructuredOutputError as exc:
                last_error = exc
                logger.warning("structured output schema failed (attempt %s): %s", attempt + 1, exc)
            if last_error is not None:
                messages = messages + [
                    LLMMessage(role="assistant", content=raw[:2000]),
                    LLMMessage(role="user", content=(
                        "Your previous response did not satisfy the required JSON schema. "
                        "Respond again with ONLY the complete, valid JSON object. Be concise.")),
                ]
        raise LLMStructuredOutputError(
            f"structured output unparseable after retry: {last_error}")

    def _validate_structured_output(self, value: Any, schema: dict[str, Any]) -> None:
        if schema.get("type") == "object":
            if not isinstance(value, dict):
                raise LLMStructuredOutputError("expected JSON object")
            for key in schema.get("required", []):
                if key not in value:
                    raise LLMStructuredOutputError(f"missing required key: {key}")
            properties = schema.get("properties", {})
            for key, subschema in properties.items():
                if key in value:
                    self._validate_json_type(key, value[key], subschema)

    def _validate_json_type(self, key: str, value: Any, schema: dict[str, Any]) -> None:
        expected = schema.get("type")
        if expected == "string" and not isinstance(value, str):
            raise LLMStructuredOutputError(f"{key} must be a string")
        if expected == "number" and not isinstance(value, (int, float)):
            raise LLMStructuredOutputError(f"{key} must be a number")
        if expected == "integer" and not isinstance(value, int):
            raise LLMStructuredOutputError(f"{key} must be an integer")
        if expected == "boolean" and not isinstance(value, bool):
            raise LLMStructuredOutputError(f"{key} must be a boolean")
        if expected == "array" and not isinstance(value, list):
            raise LLMStructuredOutputError(f"{key} must be an array")
        if expected == "object" and not isinstance(value, dict):
            raise LLMStructuredOutputError(f"{key} must be an object")
