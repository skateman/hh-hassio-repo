from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, AsyncIterator

from openai import AsyncAzureOpenAI

from .mcp_manager import MCPManager
from .models import (
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    Usage,
)

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, mcp_manager: MCPManager) -> None:
        self._mcp = mcp_manager
        self._client = AsyncAzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
        self._deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]

        # Extra kwargs passed through to chat completion calls
        raw_extra = os.environ.get("AZURE_OPENAI_EXTRA", "{}")
        try:
            self._chat_kwargs: dict[str, Any] = json.loads(raw_extra)
        except json.JSONDecodeError:
            self._chat_kwargs = {}

        self._system_prompt = os.environ.get("SYSTEM_PROMPT", "")
        self._max_iterations = int(os.environ.get("MAX_TOOL_ITERATIONS", "10"))

    def _build_messages(
        self, incoming: list[ChatMessage]
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []

        # Master system prompt always first
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})

        # Append incoming messages; additional system messages from
        # Extended OpenAI Conversation are preserved after the master prompt
        for msg in incoming:
            m: dict[str, Any] = {"role": msg.role}
            if msg.content is not None:
                m["content"] = msg.content
            if msg.tool_call_id is not None:
                m["tool_call_id"] = msg.tool_call_id
            if msg.name is not None:
                m["name"] = msg.name
            if msg.tool_calls is not None:
                m["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
            messages.append(m)

        return messages

    async def chat(self, incoming: list[ChatMessage]) -> ChatCompletionResponse:
        messages = self._build_messages(incoming)
        tools = self._mcp.get_all_tools_openai()

        for iteration in range(self._max_iterations):
            kwargs: dict[str, Any] = {
                **self._chat_kwargs,
                "model": self._deployment,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools

            response = await self._client.chat.completions.create(**kwargs)
            choice = response.choices[0]

            if choice.finish_reason == "tool_calls" or (
                choice.message.tool_calls and len(choice.message.tool_calls) > 0
            ):
                # Append assistant message with tool calls
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in choice.message.tool_calls
                    ],
                }
                if choice.message.content:
                    assistant_msg["content"] = choice.message.content
                messages.append(assistant_msg)

                # Execute each tool call
                for tc in choice.message.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                        result = await self._mcp.call_tool(tc.function.name, args)
                        logger.info("Tool %s returned: %s", tc.function.name, result[:200] if result else "")
                    except Exception as e:
                        logger.exception("Tool call %s failed", tc.function.name)
                        result = f"Error: {e}"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(result),
                    })
            else:
                # Final text response
                return ChatCompletionResponse(
                    model=self._deployment,
                    choices=[
                        Choice(
                            message=ChoiceMessage(
                                role="assistant",
                                content=choice.message.content or "",
                            ),
                            finish_reason=choice.finish_reason or "stop",
                        )
                    ],
                    usage=Usage(
                        prompt_tokens=response.usage.prompt_tokens if response.usage else 0,
                        completion_tokens=response.usage.completion_tokens if response.usage else 0,
                        total_tokens=response.usage.total_tokens if response.usage else 0,
                    ),
                )

        # Max iterations reached — return whatever we have
        return ChatCompletionResponse(
            model=self._deployment,
            choices=[
                Choice(
                    message=ChoiceMessage(
                        role="assistant",
                        content="I reached the maximum number of tool-calling iterations. Please try again with a simpler request.",
                    ),
                    finish_reason="stop",
                )
            ],
        )

    async def chat_stream_ollama(
        self, incoming: list[ChatMessage]
    ) -> AsyncIterator[str]:
        messages = self._build_messages(incoming)
        tools = self._mcp.get_all_tools_openai()

        for iteration in range(self._max_iterations):
            kwargs: dict[str, Any] = {
                **self._chat_kwargs,
                "model": self._deployment,
                "messages": messages,
                "stream": True,
            }
            if tools:
                kwargs["tools"] = tools

            tool_calls_acc: dict[int, dict[str, Any]] = {}
            content_parts: list[str] = []

            async for chunk in await self._client.chat.completions.create(**kwargs):
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta.content:
                    content_parts.append(delta.content)
                    yield json.dumps({
                        "model": "ha-orchestrator",
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()),
                        "message": {"role": "assistant", "content": delta.content},
                        "done": False,
                    }) + "\n"

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc_delta.id or "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc_delta.id:
                            tool_calls_acc[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tool_calls_acc[idx]["function"]["name"] += tc_delta.function.name
                            if tc_delta.function.arguments:
                                tool_calls_acc[idx]["function"]["arguments"] += tc_delta.function.arguments

            if tool_calls_acc:
                sorted_tcs = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": sorted_tcs,
                }
                if content_parts:
                    assistant_msg["content"] = "".join(content_parts)
                messages.append(assistant_msg)

                for tc in sorted_tcs:
                    try:
                        args = json.loads(tc["function"]["arguments"])
                        result = await self._mcp.call_tool(tc["function"]["name"], args)
                        logger.info("Tool %s returned: %s", tc["function"]["name"], result[:200] if result else "")
                    except Exception as e:
                        logger.exception("Tool call %s failed", tc["function"]["name"])
                        result = f"Error: {e}"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result),
                    })
                continue

            # Done — send final chunk
            yield json.dumps({
                "model": "ha-orchestrator",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()),
                "message": {"role": "assistant", "content": ""},
                "done": True,
            }) + "\n"
            return

        # Max iterations
        yield json.dumps({
            "model": "ha-orchestrator",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()),
            "message": {"role": "assistant", "content": "I reached the maximum number of tool-calling iterations."},
            "done": True,
        }) + "\n"
