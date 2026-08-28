from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from .mcp_manager import MCPManager
from .models import (
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    Usage,
)
from .remote_logging import RemoteLogger

logger = logging.getLogger(__name__)

_LEGACY_SITE_RESPONSE_RULE = (
    "Válaszban mindig annak a helyszínnek a nevét használd, amelyik helyszín "
    "eszközétől az adat érkezett (a tool prefix alapján), NEM az origin "
    "helyszínt."
)
_LOCAL_SITE_RESPONSE_RULE = (
    "Ha a használt tool helyszíne megegyezik az origin helyszínnel, a "
    "válaszban NE nevezd meg a helyszínt; csak az eredményt vagy a végrehajtott "
    "műveletet mondd. A helyszínt csak cross-site kérésnél vagy több helyszín "
    "eredményének összehasonlításakor nevezd meg, mindig a tool prefix szerinti "
    "magyar névvel."
)


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
    def __init__(self, mcp_manager: MCPManager, remote_logger: RemoteLogger | None = None) -> None:
        self._mcp = mcp_manager
        self._remote_logger = remote_logger
        endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
        api_key = os.environ["AZURE_OPENAI_API_KEY"]
        self._client = AsyncOpenAI(
            base_url=f"{endpoint}/openai/v1/",
            api_key=api_key,
        )
        self._deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
        logger.info(
            "Azure OpenAI deployment: %s",
            self._deployment,
        )

        # Validate and complete the native Azure OpenAI request options.
        raw_extra = os.environ.get("AZURE_OPENAI_EXTRA", "").strip() or "{}"
        try:
            extra: Any = json.loads(raw_extra)
        except json.JSONDecodeError as exc:
            raise ValueError("AZURE_OPENAI_EXTRA must be valid JSON") from exc
        if not isinstance(extra, dict):
            raise ValueError("AZURE_OPENAI_EXTRA must contain a JSON object")
        self._request_kwargs = self._prepare_request_kwargs(extra)

        self._system_prompt = os.environ.get("SYSTEM_PROMPT", "")
        self._max_iterations = int(os.environ.get("MAX_TOOL_ITERATIONS", "10"))
        self._entity_context_max_results = int(
            os.environ.get("ENTITY_CONTEXT_MAX_RESULTS", "5")
        )
        configured_logging_mode = os.environ.get(
            "REMOTE_LOGGING_MODE", "missed"
        ).lower()
        self._remote_logging_mode = (
            configured_logging_mode if configured_logging_mode == "all" else "missed"
        )

        # Global keywords — if any appear in the user message, send all tools
        gk_raw = os.environ.get("GLOBAL_KEYWORDS", "").strip()
        self._global_keywords = [_normalize(k.strip()) for k in gk_raw.split(",") if k.strip()] if gk_raw else []

        self.stats = StatsTracker()

    @staticmethod
    def _prepare_request_kwargs(extra: dict[str, Any]) -> dict[str, Any]:
        kwargs = dict(extra)

        include = kwargs.get("include", [])
        if not isinstance(include, list):
            raise ValueError("AZURE_OPENAI_EXTRA 'include' must be a list")
        if "reasoning.encrypted_content" not in include:
            kwargs["include"] = [*include, "reasoning.encrypted_content"]

        store = kwargs.pop("store", False)
        if store is not False and store is not None:
            raise ValueError(
                "Requests are always sent with store=false for privacy"
            )
        if kwargs.get("background"):
            raise ValueError(
                "Background mode requires storage and is unsupported"
            )
        if "previous_response_id" in kwargs or "conversation" in kwargs:
            raise ValueError(
                "Server-managed conversation state is unsupported; the "
                "orchestrator carries input items locally"
            )

        kwargs.pop("stream", None)
        kwargs.pop("stream_options", None)
        kwargs.pop("model", None)
        kwargs.pop("messages", None)
        kwargs.pop("input", None)
        kwargs.pop("tools", None)
        return kwargs

    @staticmethod
    def _format_tools(
        tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "parameters": tool["function"].get(
                    "parameters",
                    {"type": "object", "properties": {}},
                ),
            }
            for tool in tools
        ]

    @staticmethod
    def _response_output_items(response: Any) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json", exclude_none=True)
            for item in response.output
        ]

    @staticmethod
    def _response_tool_calls(response: Any) -> list[Any]:
        return [
            item
            for item in response.output
            if item.type == "function_call"
        ]

    @staticmethod
    def _response_text(response: Any) -> str:
        if response.output_text:
            return response.output_text
        refusals: list[str] = []
        for item in response.output:
            for content in getattr(item, "content", []):
                if getattr(content, "type", "") == "refusal":
                    refusals.append(content.refusal)
        return "".join(refusals)

    @staticmethod
    def _ensure_completed_response(response: Any) -> None:
        if response.status != "completed":
            details = (
                response.error
                or response.incomplete_details
                or "no error details"
            )
            raise RuntimeError(
                f"Azure OpenAI returned status {response.status}: {details}"
            )

    @staticmethod
    def _ollama_stream_chunk(content: str, *, done: bool) -> str:
        return json.dumps({
            "model": "ha-orchestrator",
            "created_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S.000000Z",
                time.gmtime(),
            ),
            "message": {"role": "assistant", "content": content},
            "done": done,
        }) + "\n"

    async def close(self) -> None:
        await self._client.close()

    @staticmethod
    def _last_user_text(messages: list[ChatMessage]) -> str:
        for message in reversed(messages):
            if message.role == "user" and message.content:
                return message.content
        return ""

    @staticmethod
    def _strip_origin_marker(text: str, origin: str | None) -> str:
        if not origin:
            return text
        site = re.escape(origin)
        patterns = (
            rf"This request originates from (?:the )?{site}(?: site)?\.?",
            (
                rf"Ez a kérés a[z]?\s+{site}\s+"
                rf"(?:telephelyről|helyszínről)\s+érkezett\.?"
            ),
        )
        cleaned = text
        for pattern in patterns:
            cleaned = re.sub(
                pattern,
                "",
                cleaned,
                flags=re.IGNORECASE,
            )
        return re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    def _emit_interaction(
        self,
        incoming: list[ChatMessage],
        request_messages: list[dict[str, Any]],
        response_text: str,
        origin: str | None,
        sites: list[str] | None,
        available_tools: list[str],
        tool_calls: list[dict[str, Any]],
        tool_calls_made: int,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        iterations: int,
        duration_ms: int,
        outcome: str,
    ) -> None:
        """Log a completed interaction if remote logging is enabled."""
        if not self._remote_logger:
            return
        user_msg = ""
        for msg in reversed(incoming):
            if msg.role == "user" and msg.content:
                user_msg = msg.content
                break
        if self._remote_logging_mode != "all":
            if outcome != "no_tool_calls":
                return
            self._remote_logger.log_event_bg({
                "deployment": self._deployment,
                "origin": origin,
                "routed_sites": sites or self._mcp.connected_sites,
                "user_message": user_msg,
                "assistant_response": response_text,
                "tools_available": len(available_tools),
                "tool_calls_made": 0,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            })
            return
        self._remote_logger.log_event_bg({
            "schema_version": 2,
            "event_type": "interaction",
            "deployment": self._deployment,
            "outcome": outcome,
            "origin": origin,
            "routed_sites": sites or self._mcp.connected_sites,
            "user_message": user_msg,
            "request_messages": request_messages,
            "assistant_response": response_text,
            "available_tools": available_tools,
            "tools_available": len(available_tools),
            "tool_calls": tool_calls,
            "tool_calls_made": tool_calls_made,
            "iterations": iterations,
            "duration_ms": duration_ms,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        })

    @staticmethod
    def _interaction_outcome(
        available_tools: list[str], tool_calls_made: int, had_tool_error: bool
    ) -> str:
        if had_tool_error:
            return "tool_error"
        if tool_calls_made:
            return "tools_used"
        if available_tools:
            return "no_tool_calls"
        return "no_tools_available"

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
        user_text = _normalize(self._last_user_text(incoming))

        # Check global keywords first — if matched, send all tools
        if user_text and self._global_keywords:
            if any(kw in user_text for kw in self._global_keywords):
                logger.debug("Global keyword matched — sending all tools")
                return None

        # Level 1: Check site-specific keywords in user message only
        if user_text and site_keywords:
            matched = self._match_site_keywords(user_text, site_keywords)
            if matched:
                logger.debug("Site keyword matched in user message: %s", matched)
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
                logger.debug("Site keyword matched in system prompt: %s", matched)
                return matched

        # No keyword match — send all tools
        return None

    def _build_messages(
        self, incoming: list[ChatMessage], sites: list[str] | None = None
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        origin = self._detect_origin_site(incoming)

        # Master system prompt always first.
        # If the prompt contains an {entities} placeholder, substitute it with
        # the cached entity context (opt-in inline injection).  When the
        # placeholder is absent, entities are not injected at all.
        if self._system_prompt:
            master = self._system_prompt.replace(
                _LEGACY_SITE_RESPONSE_RULE,
                _LOCAL_SITE_RESPONSE_RULE,
            )
            if "{entities}" in master:
                entity_ctx = self._mcp.get_entity_context(
                    sites,
                    query=self._last_user_text(incoming),
                    limit=self._entity_context_max_results,
                )
                lookup_policy = (
                    "This is a non-exhaustive candidate list. If the exact "
                    "entity is present, use it directly with the action tool or "
                    "GetEntityState. If absent, call the site's SearchEntities "
                    "tool with an English query that omits the site name, and "
                    "do not report it missing before that search returns no "
                    "match. When the tool site equals the request's origin "
                    "site, omit the site name from the response; mention sites "
                    "only for cross-site or multi-site results."
                )
                master = master.replace(
                    "{entities}",
                    (
                        entity_ctx
                        or "(no relevant entities were preselected)"
                    )
                    + "\n"
                    + lookup_policy,
                )
            messages.append({"role": "system", "content": master})

        # Append incoming messages; additional system messages from
        # the Ollama integration are preserved after the master prompt
        for msg in incoming:
            m: dict[str, Any] = {"role": msg.role}
            if msg.content is not None:
                content = msg.content
                if msg.role == "system":
                    content = self._strip_origin_marker(content, origin)
                    if not content:
                        continue
                m["content"] = content
            if msg.tool_call_id is not None:
                m["tool_call_id"] = msg.tool_call_id
            if msg.name is not None:
                m["name"] = msg.name
            if msg.tool_calls is not None:
                m["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
            messages.append(m)

        return messages

    async def chat(
        self, incoming: list[ChatMessage]
    ) -> ChatCompletionResponse:
        sites = self._select_sites(incoming)
        messages = self._build_messages(incoming, sites)
        response_input: list[Any] = copy.deepcopy(messages)
        capture_full_trace = bool(
            self._remote_logger and self._remote_logging_mode == "all"
        )
        request_messages = copy.deepcopy(messages) if capture_full_trace else []
        mcp_tools = self._mcp.get_all_tools_openai(sites)
        tools = self._format_tools(mcp_tools)
        available_tools = [tool["name"] for tool in tools]
        origin = self._detect_origin_site(incoming)
        tool_trace: list[dict[str, Any]] = []
        total_tool_calls = 0
        had_tool_error = False
        prompt_tokens = completion_tokens = total_tokens = 0
        iterations = 0
        started_at = time.monotonic()

        for iteration in range(self._max_iterations):
            iterations = iteration + 1
            kwargs: dict[str, Any] = {
                **self._request_kwargs,
                "model": self._deployment,
                "input": response_input,
                "store": False,
            }
            if tools:
                kwargs["tools"] = tools

            response = await self._client.responses.create(**kwargs)
            self._ensure_completed_response(response)
            if response.usage:
                prompt_tokens += response.usage.input_tokens
                completion_tokens += response.usage.output_tokens
                total_tokens += response.usage.total_tokens

            tool_calls = self._response_tool_calls(response)
            if tool_calls:
                response_input.extend(
                    self._response_output_items(response)
                )

                async def _exec_tool(call: Any):
                    try:
                        args = json.loads(call.arguments)
                        result = await self._mcp.call_tool(call.name, args)
                        return call.call_id, result, None
                    except Exception as exc:
                        logger.exception("Tool call %s failed", call.name)
                        return call.call_id, f"Error: {exc}", str(exc)

                for call in tool_calls:
                    logger.debug(
                        "Tool call %s: %s(%s)",
                        call.call_id,
                        call.name,
                        call.arguments[:500],
                    )
                results = await asyncio.gather(
                    *[_exec_tool(call) for call in tool_calls]
                )
                total_tool_calls += len(results)
                for call, (call_id, result, error) in zip(
                    tool_calls, results
                ):
                    logger.debug(
                        "Tool result for %s: %s",
                        call_id,
                        str(result)[:500],
                    )
                    had_tool_error = had_tool_error or error is not None
                    if capture_full_trace:
                        try:
                            arguments: Any = json.loads(call.arguments)
                        except json.JSONDecodeError:
                            arguments = call.arguments
                        tool_trace.append({
                            "iteration": iterations,
                            "id": call_id,
                            "name": call.name,
                            "arguments": arguments,
                            "result": str(result),
                            "error": error,
                            "assistant_content": self._response_text(response),
                        })
                    response_input.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(result),
                    })
                continue

            response_text = self._response_text(response)
            self.stats.record(
                origin,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                total_tool_calls,
            )
            self._emit_interaction(
                incoming=incoming,
                request_messages=request_messages,
                response_text=response_text,
                origin=origin,
                sites=sites,
                available_tools=available_tools,
                tool_calls=tool_trace,
                tool_calls_made=total_tool_calls,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                iterations=iterations,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                outcome=self._interaction_outcome(
                    available_tools, total_tool_calls, had_tool_error
                ),
            )
            return ChatCompletionResponse(
                model=self._deployment,
                choices=[
                    Choice(
                        message=ChoiceMessage(
                            role="assistant",
                            content=response_text,
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                ),
            )

        response_text = (
            "I reached the maximum number of tool-calling iterations. "
            "Please try again with a simpler request."
        )
        self.stats.record(
            origin,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            total_tool_calls,
        )
        self._emit_interaction(
            incoming=incoming,
            request_messages=request_messages,
            response_text=response_text,
            origin=origin,
            sites=sites,
            available_tools=available_tools,
            tool_calls=tool_trace,
            tool_calls_made=total_tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            iterations=iterations,
            duration_ms=round((time.monotonic() - started_at) * 1000),
            outcome="max_iterations",
        )
        return ChatCompletionResponse(
            model=self._deployment,
            choices=[
                Choice(
                    message=ChoiceMessage(
                        role="assistant",
                        content=response_text,
                    ),
                    finish_reason="stop",
                )
            ],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            ),
        )

    async def chat_stream_ollama(
        self, incoming: list[ChatMessage]
    ) -> AsyncIterator[str]:
        sites = self._select_sites(incoming)
        messages = self._build_messages(incoming, sites)
        response_input: list[Any] = copy.deepcopy(messages)
        capture_full_trace = bool(
            self._remote_logger and self._remote_logging_mode == "all"
        )
        request_messages = copy.deepcopy(messages) if capture_full_trace else []
        mcp_tools = self._mcp.get_all_tools_openai(sites)
        tools = self._format_tools(mcp_tools)
        available_tools = [tool["name"] for tool in tools]
        origin = self._detect_origin_site(incoming)
        tool_trace: list[dict[str, Any]] = []
        total_tool_calls = 0
        had_tool_error = False
        prompt_tokens = completion_tokens = total_tokens = 0
        iterations = 0
        started_at = time.monotonic()

        for iteration in range(self._max_iterations):
            iterations = iteration + 1
            kwargs: dict[str, Any] = {
                **self._request_kwargs,
                "model": self._deployment,
                "input": response_input,
                "store": False,
                "stream": True,
            }
            if tools:
                kwargs["tools"] = tools

            completed_response = None
            content_parts: list[str] = []
            try:
                stream = await self._client.responses.create(**kwargs)
                async for event in stream:
                    if event.type in {
                        "response.output_text.delta",
                        "response.refusal.delta",
                    }:
                        content_parts.append(event.delta)
                        yield self._ollama_stream_chunk(
                            event.delta, done=False
                        )
                    elif event.type == "response.completed":
                        completed_response = event.response
                    elif event.type in {
                        "error",
                        "response.failed",
                        "response.incomplete",
                    }:
                        raise RuntimeError(
                            f"Azure OpenAI stream ended with {event.type}: "
                            f"{event.model_dump(mode='json', exclude_none=True)}"
                        )
            except Exception:
                logger.exception("Azure OpenAI stream failed")
                response_text = (
                    "I couldn't complete the request because the model "
                    "response failed. Please try again."
                )
                self.stats.record(
                    origin,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    total_tool_calls,
                )
                self._emit_interaction(
                    incoming=incoming,
                    request_messages=request_messages,
                    response_text=response_text,
                    origin=origin,
                    sites=sites,
                    available_tools=available_tools,
                    tool_calls=tool_trace,
                    tool_calls_made=total_tool_calls,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    iterations=iterations,
                    duration_ms=round(
                        (time.monotonic() - started_at) * 1000
                    ),
                    outcome="model_error",
                )
                yield self._ollama_stream_chunk(
                    response_text, done=True
                )
                return

            if completed_response is None:
                logger.error(
                    "Azure OpenAI stream ended without a completed response"
                )
                response_text = (
                    "I couldn't complete the request because the model "
                    "stream ended unexpectedly. Please try again."
                )
                self.stats.record(
                    origin,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    total_tool_calls,
                )
                self._emit_interaction(
                    incoming=incoming,
                    request_messages=request_messages,
                    response_text=response_text,
                    origin=origin,
                    sites=sites,
                    available_tools=available_tools,
                    tool_calls=tool_trace,
                    tool_calls_made=total_tool_calls,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    iterations=iterations,
                    duration_ms=round(
                        (time.monotonic() - started_at) * 1000
                    ),
                    outcome="model_error",
                )
                yield self._ollama_stream_chunk(
                    response_text, done=True
                )
                return
            self._ensure_completed_response(completed_response)
            if completed_response.usage:
                prompt_tokens += completed_response.usage.input_tokens
                completion_tokens += completed_response.usage.output_tokens
                total_tokens += completed_response.usage.total_tokens

            tool_calls = self._response_tool_calls(completed_response)
            if tool_calls:
                response_input.extend(
                    self._response_output_items(completed_response)
                )

                async def _exec_tool(call: Any):
                    try:
                        args = json.loads(call.arguments)
                        result = await self._mcp.call_tool(call.name, args)
                        return call.call_id, result, None
                    except Exception as exc:
                        logger.exception("Tool call %s failed", call.name)
                        return call.call_id, f"Error: {exc}", str(exc)

                for call in tool_calls:
                    logger.debug(
                        "Tool call %s: %s(%s)",
                        call.call_id,
                        call.name,
                        call.arguments[:500],
                    )
                results = await asyncio.gather(
                    *[_exec_tool(call) for call in tool_calls]
                )
                total_tool_calls += len(results)
                for call, (call_id, result, error) in zip(
                    tool_calls, results
                ):
                    logger.debug(
                        "Tool result for %s: %s",
                        call_id,
                        str(result)[:500],
                    )
                    had_tool_error = had_tool_error or error is not None
                    if capture_full_trace:
                        try:
                            arguments: Any = json.loads(call.arguments)
                        except json.JSONDecodeError:
                            arguments = call.arguments
                        tool_trace.append({
                            "iteration": iterations,
                            "id": call_id,
                            "name": call.name,
                            "arguments": arguments,
                            "result": str(result),
                            "error": error,
                            "assistant_content": self._response_text(
                                completed_response
                            ),
                        })
                    response_input.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(result),
                    })
                continue

            response_text = (
                self._response_text(completed_response)
                or "".join(content_parts)
            )
            self.stats.record(
                origin,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                total_tool_calls,
            )
            self._emit_interaction(
                incoming=incoming,
                request_messages=request_messages,
                response_text=response_text,
                origin=origin,
                sites=sites,
                available_tools=available_tools,
                tool_calls=tool_trace,
                tool_calls_made=total_tool_calls,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                iterations=iterations,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                outcome=self._interaction_outcome(
                    available_tools, total_tool_calls, had_tool_error
                ),
            )
            yield self._ollama_stream_chunk("", done=True)
            return

        response_text = (
            "I reached the maximum number of tool-calling iterations."
        )
        self.stats.record(
            origin,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            total_tool_calls,
        )
        self._emit_interaction(
            incoming=incoming,
            request_messages=request_messages,
            response_text=response_text,
            origin=origin,
            sites=sites,
            available_tools=available_tools,
            tool_calls=tool_trace,
            tool_calls_made=total_tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            iterations=iterations,
            duration_ms=round((time.monotonic() - started_at) * 1000),
            outcome="max_iterations",
        )
        yield self._ollama_stream_chunk(response_text, done=True)
