"""Provider-neutral structured LLM calls, with an Amazon Bedrock implementation."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.config import BEDROCK_MODEL_ID


class LLMClientError(RuntimeError):
    """Raised when the model provider call itself fails."""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class StructuredCompletion:
    """One model call. payload is None when the model returned no tool call."""

    payload: Mapping[str, Any] | None
    usage: LLMUsage
    latency_ms: int | None
    model_id: str
    stop_reason: str | None


class StructuredLLMClient(Protocol):
    model_id: str

    def complete_structured(
        self,
        *,
        system: str,
        user_message: str,
        tool: ToolDefinition,
        max_tokens: int,
        temperature: float,
    ) -> StructuredCompletion: ...


def create_bedrock_runtime_client(region_name: str):
    """Create a Bedrock Runtime client in the configured AWS region."""

    return boto3.client(
        "bedrock-runtime",
        region_name=region_name,
    )


class BedrockConverseClient:
    """Forces one tool call through the Bedrock Converse API."""

    def __init__(self, client, *, model_id: str = BEDROCK_MODEL_ID):
        self._client = client
        self.model_id = model_id

    def complete_structured(
        self,
        *,
        system: str,
        user_message: str,
        tool: ToolDefinition,
        max_tokens: int,
        temperature: float,
    ) -> StructuredCompletion:
        try:
            response = self._client.converse(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=[
                    {"role": "user", "content": [{"text": user_message}]}
                ],
                toolConfig={
                    "tools": [
                        {
                            "toolSpec": {
                                "name": tool.name,
                                "description": tool.description,
                                "inputSchema": {"json": dict(tool.input_schema)},
                            }
                        }
                    ],
                    "toolChoice": {"tool": {"name": tool.name}},
                },
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                },
            )
        except (BotoCoreError, ClientError) as error:
            raise LLMClientError(
                f"Bedrock call to {self.model_id} failed: {error}"
            ) from error

        tool_inputs = [
            block["toolUse"]["input"]
            for block in response["output"]["message"]["content"]
            if "toolUse" in block and block["toolUse"]["name"] == tool.name
        ]
        usage = response.get("usage", {})
        return StructuredCompletion(
            payload=tool_inputs[0] if len(tool_inputs) == 1 else None,
            usage=LLMUsage(
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
            ),
            latency_ms=response.get("metrics", {}).get("latencyMs"),
            model_id=self.model_id,
            stop_reason=response.get("stopReason"),
        )


def total_usage(completions: Sequence[StructuredCompletion]) -> LLMUsage:
    usage = LLMUsage(input_tokens=0, output_tokens=0)
    for completion in completions:
        usage = usage + completion.usage
    return usage
