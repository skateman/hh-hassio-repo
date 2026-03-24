from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class FunctionCall(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: FunctionCall


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    model: str = "ha-orchestrator"
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)


class ErrorDetail(BaseModel):
    message: str
    type: str = "error"
    code: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# --- Ollama-compatible models ---

class OllamaMessage(BaseModel):
    role: str
    content: str


class OllamaChatRequest(BaseModel):
    model: str = "ha-orchestrator"
    messages: list[OllamaMessage]
    stream: bool = True
    options: dict[str, Any] | None = None
    tools: list[Any] | None = None  # accepted but ignored


class OllamaChatResponse(BaseModel):
    model: str = "ha-orchestrator"
    created_at: str
    message: OllamaMessage
    done: bool
    total_duration: int | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None


class OllamaModel(BaseModel):
    name: str
    model: str
    modified_at: str
    size: int = 0
    digest: str = ""
    details: dict[str, Any] = Field(default_factory=lambda: {
        "family": "custom",
        "parameter_size": "unknown",
        "quantization_level": "unknown",
    })


class OllamaModelList(BaseModel):
    models: list[OllamaModel]
