"""Model access for the agent loop: Bedrock Converse with tool calling.
Tests use a fake ``AgentLLM`` instead, so they need no AWS.
"""

import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src import config

__all__ = [
    "AgentLLM",
    "AgentTurn",
    "BedrockAgentLLM",
    "LLMClientError",
    "LLMUsage",
    "SpendCapReached",
    "SpendGuard",
    "ToolCall",
    "ToolDefinition",
    "create_agent_llm",
]


class LLMClientError(RuntimeError):
    """The model provider call itself failed."""


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
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class AgentTurn:
    """One reply from the model: its text, tool calls and token use."""

    content: list[dict]
    text: str
    tool_calls: tuple[ToolCall, ...]
    usage: LLMUsage
    latency_ms: int
    stop_reason: str | None


class AgentLLM(Protocol):
    model_id: str

    def converse(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: Sequence[ToolDefinition],
        force_tool: str | None,
        max_tokens: int,
        temperature: float,
    ) -> AgentTurn: ...


class BedrockAgentLLM:
    """Sends the conversation to Bedrock and returns the model's reply."""

    def __init__(self, client, *, model_id: str = config.AGENT_MODEL_ID):
        self._client = client
        self.model_id = model_id

    def converse(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: Sequence[ToolDefinition],
        force_tool: str | None,
        max_tokens: int,
        temperature: float,
    ) -> AgentTurn:
        tool_choice = {"tool": {"name": force_tool}} if force_tool else {"auto": {}}
        started = time.monotonic()
        try:
            response = self._client.converse(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=messages,
                toolConfig={
                    "tools": [
                        {
                            "toolSpec": {
                                "name": tool.name,
                                "description": tool.description,
                                "inputSchema": {"json": dict(tool.input_schema)},
                            }
                        }
                        for tool in tools
                    ],
                    "toolChoice": tool_choice,
                },
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            )
        except (BotoCoreError, ClientError) as error:
            raise LLMClientError(
                f"Bedrock call to {self.model_id} failed: {error}"
            ) from error

        content = list(response["output"]["message"]["content"])
        usage = response.get("usage", {})
        return AgentTurn(
            content=content,
            text="".join(block["text"] for block in content if "text" in block),
            tool_calls=tuple(
                ToolCall(
                    id=block["toolUse"]["toolUseId"],
                    name=block["toolUse"]["name"],
                    input=block["toolUse"]["input"]
                    if isinstance(block["toolUse"]["input"], dict)
                    else {},
                )
                for block in content
                if "toolUse" in block
            ),
            usage=LLMUsage(
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
            ),
            latency_ms=response.get("metrics", {}).get("latencyMs")
            or int((time.monotonic() - started) * 1000),
            stop_reason=response.get("stopReason"),
        )


def create_agent_llm(
    model_id: str = config.AGENT_MODEL_ID, *, read_timeout_s: int = 90
) -> BedrockAgentLLM:
    """Makes a Bedrock client that times out and retries, so one slow call can't hang a run."""

    client = boto3.client(
        "bedrock-runtime",
        region_name=config.AWS_REGION,
        config=Config(
            read_timeout=read_timeout_s,
            retries={"max_attempts": 3, "mode": "adaptive"},
        ),
    )
    return BedrockAgentLLM(client, model_id=model_id)


class SpendCapReached(RuntimeError):
    """Raised when estimated LLM spend reaches the cap."""


class SpendGuard:
    """Adds up estimated LLM spend and blocks new calls once the cap is reached.
    Runs in parallel can overshoot it slightly.
    """

    def __init__(
        self,
        cap_usd: float = config.AGENT_SPEND_CAP_USD,
        price_per_m_tokens: tuple[float, float] = config.AGENT_PRICE_USD_PER_M_TOKENS,
    ):
        self.cap_usd = cap_usd
        self._price_in, self._price_out = price_per_m_tokens
        self._spent = 0.0
        self._lock = threading.Lock()

    @property
    def spent_usd(self) -> float:
        with self._lock:
            return self._spent

    def check(self) -> None:
        if self.spent_usd >= self.cap_usd:
            raise SpendCapReached(
                f"Estimated LLM spend ${self.spent_usd:.2f} reached the cap of ${self.cap_usd:.2f}."
            )

    def record(self, usage: LLMUsage) -> None:
        cost = (
            usage.input_tokens * self._price_in + usage.output_tokens * self._price_out
        ) / 1_000_000
        with self._lock:
            self._spent += cost
