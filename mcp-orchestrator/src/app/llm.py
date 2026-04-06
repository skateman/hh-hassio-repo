from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import unicodedata
from dataclasses import dataclass
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


def _normalize(text: str) -> str:
    """Lowercase and strip diacritics (ě→e, š→s, ö→o, etc.)."""
    text = text.lower()
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


@dataclass
class SiteStats:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tool_calls: int = 0


class StatsTracker:
    def __init__(self) -> None:
        self._by_site: dict[str, SiteStats] = {}
        self._started_at = time.time()

    def record(self, origin: str | None, prompt_tokens: int, completion_tokens: int,
               total_tokens: int, tool_calls: int) -> None:
        site = origin or "unknown"
        if site not in self._by_site:
            self._by_site[site] = SiteStats()
        s = self._by_site[site]
        s.requests += 1
        s.prompt_tokens += prompt_tokens
        s.completion_tokens += completion_tokens
        s.total_tokens += total_tokens
        s.tool_calls += tool_calls

    def snapshot(self) -> dict[str, Any]:
        totals = SiteStats()
        sites = {}
        for site, s in self._by_site.items():
            sites[site] = {
                "requests": s.requests,
                "prompt_tokens": s.prompt_tokens,
                "completion_tokens": s.completion_tokens,
                "total_tokens": s.total_tokens,
                "tool_calls": s.tool_calls,
            }
            totals.requests += s.requests
            totals.prompt_tokens += s.prompt_tokens
            totals.completion_tokens += s.completion_tokens
            totals.total_tokens += s.total_tokens
            totals.tool_calls += s.tool_calls
        return {
            "uptime_seconds": int(time.time() - self._started_at),
            "totals": {
                "requests": totals.requests,
                "prompt_tokens": totals.prompt_tokens,
                "completion_tokens": totals.completion_tokens,
                "total_tokens": totals.total_tokens,
                "tool_calls": totals.tool_calls,
            },
            "by_site": sites,
        }


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

        # Global keywords — if any appear in the user message, send all tools
        gk_raw = os.environ.get("GLOBAL_KEYWORDS", "").strip()
        self._global_keywords = [_normalize(k.strip()) for k in gk_raw.split(",") if k.strip()] if gk_raw else []

        self.stats = StatsTracker()

    def _detect_origin_site(self, messages: list[ChatMessage]) -> str | None:
        """Detect the origin site from system messages (set via Ollama Instructions)."""
        all_sites = self._mcp.connected_sites
        for msg in messages:
            if msg.role == "system" and msg.content:
                text = _normalize(msg.content)
                for site in all_sites:
                    if _normalize(site) in text:
                        return site
        return None

    def _match_site_keywords(self, text: str, site_keywords: dict[str, list[str]]) -> list[str]:
        """Match site keywords against a normalized text. Also checks site names."""
        matched = []
        for site in self._mcp.connected_sites:
            keywords = site_keywords.get(site, [])
            if any(_normalize(kw) in text for kw in keywords) or _normalize(site) in text:
                matched.append(site)
        return matched

    def _select_sites(self, incoming: list[ChatMessage]) -> list[str] | None:
        """Determine which sites' tools to include. Returns None for all tools.

        Two-level keyword detection:
        1. Check the user message for site keywords — if found, use only those sites.
        2. If no match, check the combined system prompt (L1 master + L2 incoming) for
           site keywords — if found, use those sites.
        3. No match anywhere — send all tools.
        """
        site_keywords = self._mcp.site_keywords

        # Find the last user message
        user_text = ""
        for msg in reversed(incoming):
            if msg.role == "user" and msg.content:
                user_text = _normalize(msg.content)
                break

        # Check global keywords first — if matched, send all tools
        if user_text and self._global_keywords:
            if any(kw in user_text for kw in self._global_keywords):
                logger.info("Global keyword matched — sending all tools")
                return None

        # Level 1: Check site-specific keywords in user message only
        if user_text and site_keywords:
            matched = self._match_site_keywords(user_text, site_keywords)
            if matched:
                logger.info("Site keyword matched in user message: %s", matched)
                return matched

        # Level 2: Check site-specific keywords in L2 system prompt only
        # (incoming system messages, excluding the L1 master prompt from config)
        system_parts: list[str] = []
        for msg in incoming:
            if msg.role == "system" and msg.content:
                system_parts.append(_normalize(msg.content))
        system_text = " ".join(system_parts)

        if system_text and site_keywords:
            matched = self._match_site_keywords(system_text, site_keywords)
            if matched:
                logger.info("Site keyword matched in system prompt: %s", matched)
                return matched

        # No keyword match — send all tools
        return None

    def _build_messages(
        self, incoming: list[ChatMessage]
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []

        # Master system prompt always first
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})

        # Append incoming messages; additional system messages from
        # the Ollama integration are preserved after the master prompt
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
        sites = self._select_sites(incoming)
        tools = self._mcp.get_all_tools_openai(sites)
        origin = self._detect_origin_site(incoming)
        total_tool_calls = 0

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

                # Execute tool calls in parallel
                async def _exec_tool(tc):
                    try:
                        args = json.loads(tc.function.arguments)
                        return tc.id, await self._mcp.call_tool(tc.function.name, args)
                    except Exception as e:
                        logger.exception("Tool call %s failed", tc.function.name)
                        return tc.id, f"Error: {e}"

                results = await asyncio.gather(
                    *[_exec_tool(tc) for tc in choice.message.tool_calls]
                )
                total_tool_calls += len(choice.message.tool_calls)
                for tool_call_id, result in results:
                    logger.info("Tool result for %s: %s", tool_call_id, str(result)[:200])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": str(result),
                    })
            else:
                # Final text response
                pt = response.usage.prompt_tokens if response.usage else 0
                ct = response.usage.completion_tokens if response.usage else 0
                tt = response.usage.total_tokens if response.usage else 0
                self.stats.record(origin, pt, ct, tt, total_tool_calls)
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
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        total_tokens=tt,
                    ),
                )

        # Max iterations reached — return whatever we have
        self.stats.record(origin, 0, 0, 0, total_tool_calls)
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
        sites = self._select_sites(incoming)
        tools = self._mcp.get_all_tools_openai(sites)
        origin = self._detect_origin_site(incoming)
        total_tool_calls = 0
        last_usage = None

        for iteration in range(self._max_iterations):
            kwargs: dict[str, Any] = {
                **self._chat_kwargs,
                "model": self._deployment,
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if tools:
                kwargs["tools"] = tools

            tool_calls_acc: dict[int, dict[str, Any]] = {}
            content_parts: list[str] = []

            async for chunk in await self._client.chat.completions.create(**kwargs):
                if chunk.usage:
                    last_usage = chunk.usage
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
                total_tool_calls += len(sorted_tcs)
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": sorted_tcs,
                }
                if content_parts:
                    assistant_msg["content"] = "".join(content_parts)
                messages.append(assistant_msg)

                # Execute tool calls in parallel
                async def _exec_tool_s(tc):
                    try:
                        args = json.loads(tc["function"]["arguments"])
                        return tc["id"], await self._mcp.call_tool(tc["function"]["name"], args)
                    except Exception as e:
                        logger.exception("Tool call %s failed", tc["function"]["name"])
                        return tc["id"], f"Error: {e}"

                results = await asyncio.gather(
                    *[_exec_tool_s(tc) for tc in sorted_tcs]
                )
                for tool_call_id, result in results:
                    logger.info("Tool result for %s: %s", tool_call_id, str(result)[:200])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": str(result),
                    })
                continue

            # Done — send final chunk
            self.stats.record(
                origin,
                last_usage.prompt_tokens if last_usage else 0,
                last_usage.completion_tokens if last_usage else 0,
                last_usage.total_tokens if last_usage else 0,
                total_tool_calls,
            )
            yield json.dumps({
                "model": "ha-orchestrator",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()),
                "message": {"role": "assistant", "content": ""},
                "done": True,
            }) + "\n"
            return

        # Max iterations
        self.stats.record(
            origin,
            last_usage.prompt_tokens if last_usage else 0,
            last_usage.completion_tokens if last_usage else 0,
            last_usage.total_tokens if last_usage else 0,
            total_tool_calls,
        )
        yield json.dumps({
            "model": "ha-orchestrator",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()),
            "message": {"role": "assistant", "content": "I reached the maximum number of tool-calling iterations."},
            "done": True,
        }) + "\n"
