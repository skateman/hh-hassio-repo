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
from contextlib import aclosing
from typing import Any, AsyncIterator, Iterable

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
from .request_budget import RequestBudget, RequestTimeoutError, TimeoutSettings
from .skills import SkillContext, SkillRegistry, SkillSession

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
_FOLLOWUP_START_WORDS = {"es", "akkor", "then"}
_SHORT_FOLLOWUP_QUESTION_WORDS = {"miert", "why"}
_FOLLOWUP_START_PHRASES = (
    ("and", "then"),
    ("try", "again"),
    ("please", "try", "again"),
    ("do", "it"),
    ("what", "happened"),
    ("what", "was"),
    ("probald", "ujra"),
    ("probald", "megint"),
    ("csinald", "meg"),
    ("hajtsd", "vegre"),
    ("most", "mar"),
)
_FOLLOWUP_WORDS = {
    "azt",
    "ezt",
    "ugyanazt",
    "elozo",
    "amit",
    "arra",
    "ott",
    "again",
    "previous",
    "ujra",
    "megint",
}
_FOLLOWUP_ACTION_WORDS = {
    "check",
    "do",
    "execute",
    "fix",
    "perform",
    "repeat",
    "retry",
    "run",
    "switch",
    "try",
    "turn",
}
_FOLLOWUP_PRONOUNS = {"it", "that"}
_EXISTENTIAL_THERE_VERBS = {"is", "are", "was", "were"}


def _normalize(text: str) -> str:
    """Lowercase and strip diacritics (ě→e, š→s, ö→o, etc.)."""
    text = text.lower()
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


@dataclass(frozen=True)
class BroadcastPolicy:
    voice: bool
    origin_site: str | None

    @classmethod
    def for_request(
        cls, incoming: Iterable[ChatMessage], site_names: Iterable[str]
    ) -> BroadcastPolicy:
        instructions = [
            message.content or "" for message in incoming if message.role == "system"
        ]
        channel = SkillContext.for_request(
            "", instructions, has_previous_turn=False,
            multi_site_request=False, entity_unresolved=False,
        ).channel
        if channel == "telegram":
            return cls(voice=False, origin_site=None)

        # Only explicit caller declarations establish the voice origin.
        # Site mentions in examples or in the user's request must not change it.
        patterns = (
            r"\b(?:this )?request originates from (?:the )?([a-z0-9_-]+)\b",
            r"\bez a keres az? ([a-z0-9_-]+) (?:telephelyrol|helyszinrol)\b",
            r"\bhasznald az? ([a-z0-9_-]+) mcp szervert\b",
            r"\buse (?:the )?([a-z0-9_-]+) mcp server\b",
        )
        declared = {
            match.group(1)
            for text in instructions
            for pattern in patterns
            for match in re.finditer(pattern, " ".join(_normalize(text).split()))
        }
        known_sites = {_normalize(site): site for site in site_names}
        origin = known_sites.get(next(iter(declared))) if len(declared) == 1 else None
        return cls(voice=True, origin_site=origin)

    def allows(self, name: str) -> bool:
        if not self.voice or name.rsplit("__", 1)[-1] != "HassBroadcast":
            return True
        site, separator, _ = name.partition("__")
        return bool(
            separator and self.origin_site is not None and site != self.origin_site
        )

    def validate_call(self, name: str) -> None:
        if not self.allows(name):
            raise ValueError(
                f"Broadcast '{name}' is unavailable during this voice conversation: "
                "the calling satellite cannot be excluded safely. Explain the "
                "limitation: a normal spoken reply is not a broadcast. "
                "Do not claim delivery or choose another site."
            )

    def filter_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [tool for tool in tools if self.allows(tool["function"]["name"])]

    def guidance(self) -> str:
        scope = (
            f"to the voice-origin site '{self.origin_site}'"
            if self.origin_site is not None
            else "because the voice-origin site is unknown"
        )
        return (
            f"HassBroadcast is unavailable {scope}: it could cancel this conversation. "
            "For an explicit broadcast or all-speakers request at a blocked site, "
            "first say you cannot broadcast there; do not silently replace it with "
            "local speech. Otherwise, for an ordinary local 'say/tell' request, "
            "speak the message as your normal reply, using the message speaker's "
            "perspective and addressing a named recipient directly. Do not claim "
            "delivery or broadcasting without a successful tool result. "
            "Never substitute another site or infer a person's location."
        )


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
    def __init__(
        self,
        mcp_manager: MCPManager,
        remote_logger: RemoteLogger | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self._mcp = mcp_manager
        self._remote_logger = remote_logger
        self._skill_registry = (
            skill_registry
            if skill_registry is not None
            else SkillRegistry.from_environment()
        )
        timeouts = TimeoutSettings.from_environment()
        self._llm_timeout_seconds = timeouts.llm_seconds
        self._request_timeout_seconds = timeouts.request_seconds
        self._timeout_reply = timeouts.reply
        endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
        api_key = os.environ["AZURE_OPENAI_API_KEY"]
        self._client = AsyncOpenAI(
            base_url=f"{endpoint}/openai/v1/",
            api_key=api_key,
            timeout=self._llm_timeout_seconds,
            max_retries=0,
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
                # Responses otherwise makes optional HA parameters required.
                "strict": False,
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
        skills: SkillSession | None = None,
        budget: RequestBudget | None = None,
        timeout_stage: str | None = None,
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
                "skills": skills.records() if skills else [],
                "origin": origin,
                "routed_sites": sites or self._mcp.connected_sites,
                "user_message": user_msg,
                "assistant_response": response_text,
                "tools_available": len(available_tools),
                "tool_calls_made": 0,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "model_attempts": budget.attempts if budget else [],
            })
            return
        self._remote_logger.log_event_bg({
            "schema_version": 2,
            "event_type": "interaction",
            "deployment": self._deployment,
            "skills": skills.records() if skills else [],
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
            "model_attempts": budget.attempts if budget else [],
            "timeout_stage": timeout_stage,
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

    @staticmethod
    def _is_referential_followup(text: str) -> bool:
        normalized = " ".join(
            re.findall(r"[a-z0-9]+", _normalize(text))
        )
        words = normalized.split()
        if not normalized or len(words) > 15:
            return False
        if words[0] in _FOLLOWUP_START_WORDS:
            return True
        if (
            words[0] in _SHORT_FOLLOWUP_QUESTION_WORDS
            and len(words) <= 4
        ):
            return True
        if any(word in _FOLLOWUP_WORDS for word in words):
            return True
        if (
            any(word in _FOLLOWUP_PRONOUNS for word in words)
            and any(word in _FOLLOWUP_ACTION_WORDS for word in words)
        ):
            return True
        if "there" in words and any(
            word in _FOLLOWUP_ACTION_WORDS for word in words
        ):
            existential_there = any(
                words[index] == "there"
                and index + 1 < len(words)
                and words[index + 1] in _EXISTENTIAL_THERE_VERBS
                for index in range(len(words))
            )
            if not existential_there:
                return True
        return any(
            tuple(words[:len(phrase)]) == phrase
            for phrase in _FOLLOWUP_START_PHRASES
        )

    def _followup_sites(
        self,
        incoming: list[ChatMessage],
        site_keywords: dict[str, list[str]],
    ) -> tuple[bool, list[str] | None]:
        skipped_current_user = False
        for message in reversed(incoming):
            if message.role != "user" or not message.content:
                continue
            if not skipped_current_user:
                skipped_current_user = True
                continue
            text = _normalize(message.content)
            if self._global_keywords and any(
                keyword in text for keyword in self._global_keywords
            ):
                return True, None
            matched = self._match_site_keywords(
                text,
                site_keywords,
            )
            if matched:
                return True, matched
            if not self._is_referential_followup(text):
                return False, None
        return False, None

    def _select_sites(self, incoming: list[ChatMessage]) -> list[str] | None:
        """Determine which sites' tools to include. Returns None for all tools.

        Site selection order:
        1. Check the user message for site keywords — if found, use only those sites.
        2. For a referential follow-up, reuse the latest site explicitly named
           by the user earlier in the conversation.
        3. If no match, check the combined system prompt (L1 master + L2 incoming) for
           site keywords — if found, use those sites.
        4. No match anywhere — send all tools.
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

        # Level 2: Keep the most recently named site for referential follow-ups.
        if self._is_referential_followup(user_text):
            inherited, matched = self._followup_sites(
                incoming,
                site_keywords,
            )
            if inherited:
                logger.debug(
                    "Site routing inherited from conversation follow-up: %s",
                    matched or "all sites",
                )
                return matched

        # Level 3: Check site-specific keywords in L2 system prompt only
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
        self,
        incoming: list[ChatMessage],
        sites: list[str] | None = None,
        *,
        skills: SkillSession | None = None,
        tool_guidance: str | None = None,
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
                    "match. With a unique exact entity name, send only the name "
                    "and action-specific values. Preserve action data such as "
                    "the cleaning area for HassVacuumCleanArea; if an exact "
                    "name is duplicated, include only the area or domain needed "
                    "to disambiguate it. When the tool site equals the request's "
                    "origin site, omit the site name from the response; mention "
                    "sites only for cross-site or multi-site results."
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

        if skills is not None:
            skills.inject(messages, offset=len(messages))

        if tool_guidance:
            messages.append({"role": "system", "content": tool_guidance})

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

    def _start_skills(
        self,
        incoming: list[ChatMessage],
        sites: list[str] | None,
        tools: list[dict[str, Any]],
    ) -> tuple[SkillContext, SkillSession]:
        user_message = self._last_user_text(incoming)
        session = SkillSession(self._skill_registry)
        if not self._skill_registry.skills:
            return SkillContext(user_message), session
        multi_site = sites is not None and len(sites) > 1
        if sites is None:
            multi_site = any(
                keyword in _normalize(user_message)
                for keyword in self._global_keywords
            )
            if not multi_site and self._is_referential_followup(user_message):
                inherited, previous_sites = self._followup_sites(
                    incoming, self._mcp.site_keywords
                )
                multi_site = inherited and previous_sites is None
        has_lookup = any(
            tool["function"]["name"].rsplit("__", 1)[-1] == "SearchEntities"
            for tool in tools
        )
        context = SkillContext.for_request(
            user_message,
            (message.content or "" for message in incoming if message.role == "system"),
            has_previous_turn=sum(message.role == "user" for message in incoming) > 1,
            multi_site_request=multi_site,
            entity_unresolved=(
                has_lookup and self._mcp.needs_entity_discovery(user_message, sites)
            ),
        )
        session.activate(context)
        return context, session

    def _update_skills(
        self,
        session: SkillSession,
        context: SkillContext,
        response_input: list[dict[str, Any]],
        iterations: int,
    ) -> None:
        if iterations < self._max_iterations and session.activate(
            context, iteration=iterations + 1
        ):
            session.inject(response_input, offset=int(bool(self._system_prompt)))

    def _prepare_request_tool_arguments(
        self,
        name: str,
        arguments: str,
        available_tools: list[str],
        broadcast_policy: BroadcastPolicy,
    ) -> dict[str, Any]:
        broadcast_policy.validate_call(name)
        if name not in available_tools:
            raise ValueError(
                f"Tool '{name}' is not available for this request; "
                "do not substitute another site or tool."
            )
        return self._mcp.prepare_tool_arguments(name, json.loads(arguments))

    async def _execute_tool_calls(
        self,
        calls: list[Any],
        available_tools: list[str],
        broadcast_policy: BroadcastPolicy,
        budget: RequestBudget,
        iteration: int,
        assistant_content: str,
        tool_trace: list[dict[str, Any]] | None,
    ) -> list[tuple[str, str, str | None, Any]]:
        budget.check("tools")
        records = [{
            "iteration": iteration,
            "id": call.call_id,
            "name": call.name,
            "arguments": call.arguments,
            "result": "",
            "error": None,
            "assistant_content": assistant_content,
            "status": "not_started",
            "reused": False,
        } for call in calls]
        if tool_trace is not None:
            tool_trace.extend(records)

        async def execute(call: Any, record: dict[str, Any]):
            started = time.monotonic()
            arguments: Any = call.arguments
            key: tuple[str, str] | None = None
            read_only = True
            try:
                budget.check("tools")
                arguments = self._prepare_request_tool_arguments(
                    call.name, call.arguments, available_tools, broadcast_policy
                )
                record["arguments"] = arguments
                key = (call.name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
                read_only = call.name.rsplit("__", 1)[-1] in {
                    "GetLiveContext", "SearchEntities", "GetEntityState", "GetDateTime",
                }
                error = None
                if budget.retry_used and not read_only and key in budget.action_results:
                    record["reused"] = True
                    previous_result, error = budget.action_results[key]
                    result = (
                        (
                            "This identical action already completed earlier in this request"
                            if error is None
                            else "This identical action was already attempted; its prior error still applies"
                        )
                        + " and was NOT executed again. Previous result:\n"
                        + previous_result
                    )
                    logger.info("Reusing prior action result after LLM retry: %s", call.name)
                else:
                    record["status"] = "running"
                    logger.debug("Tool call %s: %s(%s)", call.call_id, call.name, arguments)
                    result = str(await self._mcp.call_tool(call.name, arguments))
                    if not read_only:
                        budget.action_results[key] = (result, None)
                record.update(
                    status="completed" if error is None else "error", result=result, error=error
                )
                return call.call_id, result, error, arguments
            except asyncio.CancelledError:
                record.update(
                    status="cancelled",
                    error="Request cancelled; an already-dispatched operation may still complete",
                )
                raise
            except RequestTimeoutError:
                record.update(status="not_started", error="Request deadline reached before dispatch")
                raise
            except Exception as exc:
                logger.exception("Tool call %s failed", call.name)
                if key is not None and not read_only and record["status"] == "running":
                    budget.action_results[key] = (f"Error: {exc}", str(exc))
                record.update(status="error", result=f"Error: {exc}", error=str(exc))
                return call.call_id, f"Error: {exc}", str(exc), arguments
            finally:
                record["duration_ms"] = round((time.monotonic() - started) * 1000)

        tasks = [
            asyncio.create_task(execute(call, record))
            for call, record in zip(calls, records)
        ]
        try:
            async with asyncio.timeout_at(budget.deadline):
                return await asyncio.gather(*tasks)
        except TimeoutError as exc:
            raise RequestTimeoutError("tools") from exc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def chat(
        self, incoming: list[ChatMessage]
    ) -> ChatCompletionResponse:
        sites = self._select_sites(incoming)
        catalog = self._mcp.get_all_tools_openai(sites)
        broadcast_policy = BroadcastPolicy.for_request(
            incoming, self._mcp.connected_sites
        )
        mcp_tools = broadcast_policy.filter_tools(catalog)
        skill_context, skills = self._start_skills(incoming, sites, mcp_tools)
        messages = self._build_messages(
            incoming, sites, skills=skills,
            tool_guidance=(
                broadcast_policy.guidance() if len(mcp_tools) < len(catalog) else None
            ),
        )
        response_input: list[Any] = copy.deepcopy(messages)
        capture_full_trace = bool(
            self._remote_logger and self._remote_logging_mode == "all"
        )
        request_messages = copy.deepcopy(messages) if capture_full_trace else []
        tools = self._format_tools(mcp_tools)
        available_tools = [tool["name"] for tool in tools]
        origin = self._detect_origin_site(incoming)
        tool_trace: list[dict[str, Any]] = []
        total_tool_calls = 0
        had_tool_error = False
        prompt_tokens = completion_tokens = total_tokens = 0
        iterations = 0
        budget = RequestBudget(self._llm_timeout_seconds, self._request_timeout_seconds)
        started_at = budget.started_at

        def timed_out(error: RequestTimeoutError) -> ChatCompletionResponse:
            logger.warning("Request timed out during %s; returning configured fallback", error.stage)
            self.stats.record(
                origin, prompt_tokens, completion_tokens, total_tokens, total_tool_calls
            )
            self._emit_interaction(
                incoming=incoming, request_messages=request_messages,
                response_text=self._timeout_reply, origin=origin, sites=sites,
                available_tools=available_tools, tool_calls=tool_trace,
                tool_calls_made=total_tool_calls, prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens, total_tokens=total_tokens,
                iterations=iterations,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                outcome="timeout", skills=skills, budget=budget,
                timeout_stage=error.stage,
            )
            return ChatCompletionResponse(
                model=self._deployment,
                choices=[Choice(
                    message=ChoiceMessage(role="assistant", content=self._timeout_reply),
                    finish_reason="stop",
                )],
                usage=Usage(
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                ),
            )

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

            try:
                response = await budget.response(
                    self._client.responses.create, kwargs, iterations
                )
            except RequestTimeoutError as exc:
                return timed_out(exc)
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

                total_tool_calls += len(tool_calls)
                try:
                    results = await self._execute_tool_calls(
                        tool_calls, available_tools, broadcast_policy, budget,
                        iterations, self._response_text(response),
                        tool_trace if capture_full_trace else None,
                    )
                except RequestTimeoutError as exc:
                    return timed_out(exc)
                for call, (
                    call_id,
                    result,
                    error,
                    prepared_arguments,
                ) in zip(
                    tool_calls, results
                ):
                    logger.debug(
                        "Tool result for %s: %s",
                        call_id,
                        str(result)[:500],
                    )
                    had_tool_error = had_tool_error or error is not None
                    skill_context = skill_context.after_tool(
                        call.name, str(result), failed=error is not None
                    )
                    response_input.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(result),
                    })
                self._update_skills(skills, skill_context, response_input, iterations)
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
                skills=skills,
                budget=budget,
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
            skills=skills,
            budget=budget,
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
        catalog = self._mcp.get_all_tools_openai(sites)
        broadcast_policy = BroadcastPolicy.for_request(
            incoming, self._mcp.connected_sites
        )
        mcp_tools = broadcast_policy.filter_tools(catalog)
        skill_context, skills = self._start_skills(incoming, sites, mcp_tools)
        messages = self._build_messages(
            incoming, sites, skills=skills,
            tool_guidance=(
                broadcast_policy.guidance() if len(mcp_tools) < len(catalog) else None
            ),
        )
        response_input: list[Any] = copy.deepcopy(messages)
        capture_full_trace = bool(
            self._remote_logger and self._remote_logging_mode == "all"
        )
        request_messages = copy.deepcopy(messages) if capture_full_trace else []
        tools = self._format_tools(mcp_tools)
        available_tools = [tool["name"] for tool in tools]
        origin = self._detect_origin_site(incoming)
        tool_trace: list[dict[str, Any]] = []
        total_tool_calls = 0
        had_tool_error = False
        prompt_tokens = completion_tokens = total_tokens = 0
        iterations = 0
        budget = RequestBudget(self._llm_timeout_seconds, self._request_timeout_seconds)
        started_at = budget.started_at
        delivered_text: list[str] = []

        def timed_out(error: RequestTimeoutError) -> str:
            logger.warning("Request timed out during %s; returning configured fallback", error.stage)
            previous = "".join(delivered_text)
            suffix = (" " if previous and not previous[-1].isspace() else "") + self._timeout_reply
            self.stats.record(
                origin, prompt_tokens, completion_tokens, total_tokens, total_tool_calls
            )
            self._emit_interaction(
                incoming=incoming, request_messages=request_messages,
                response_text=previous + suffix, origin=origin, sites=sites,
                available_tools=available_tools, tool_calls=tool_trace,
                tool_calls_made=total_tool_calls, prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens, total_tokens=total_tokens,
                iterations=iterations,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                outcome="timeout", skills=skills, budget=budget,
                timeout_stage=error.stage,
            )
            return self._ollama_stream_chunk(suffix, done=True)

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
                async with aclosing(budget.events(
                    self._client.responses.create, kwargs, iterations
                )) as events:
                    async for event in events:
                        if event.type in {
                            "response.output_text.delta",
                            "response.refusal.delta",
                        }:
                            content_parts.append(event.delta)
                            delivered_text.append(event.delta)
                            if event.delta:
                                budget.speech_started = True
                            yield self._ollama_stream_chunk(
                                event.delta, done=False
                            )
                        elif event.type == "response.completed":
                            completed_response = event.response
            except RequestTimeoutError as exc:
                yield timed_out(exc)
                return
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
                    skills=skills,
                    budget=budget,
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
                    skills=skills,
                    budget=budget,
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

                total_tool_calls += len(tool_calls)
                try:
                    results = await self._execute_tool_calls(
                        tool_calls, available_tools, broadcast_policy, budget,
                        iterations, self._response_text(completed_response),
                        tool_trace if capture_full_trace else None,
                    )
                except RequestTimeoutError as exc:
                    yield timed_out(exc)
                    return
                for call, (
                    call_id,
                    result,
                    error,
                    prepared_arguments,
                ) in zip(
                    tool_calls, results
                ):
                    logger.debug(
                        "Tool result for %s: %s",
                        call_id,
                        str(result)[:500],
                    )
                    had_tool_error = had_tool_error or error is not None
                    skill_context = skill_context.after_tool(
                        call.name, str(result), failed=error is not None
                    )
                    response_input.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(result),
                    })
                self._update_skills(skills, skill_context, response_input, iterations)
                continue

            response_text = (
                self._response_text(completed_response)
                or "".join(content_parts)
            )
            if response_text and not content_parts:
                yield self._ollama_stream_chunk(response_text, done=False)
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
                skills=skills,
                budget=budget,
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
            skills=skills,
            budget=budget,
        )
        yield self._ollama_stream_chunk(response_text, done=True)
