from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from mcp import ClientSession
try:
    from mcp.client.streamable_http import (
        create_mcp_http_client,
        streamable_http_client,
    )
    _MCP_V2 = True
except ImportError:
    from mcp.client.streamable_http import (
        streamablehttp_client as streamable_http_client,
    )
    create_mcp_http_client = None
    _MCP_V2 = False

logger = logging.getLogger(__name__)

SEPARATOR = "__"
_LIVE_CONTEXT_TOOL = "GetLiveContext"
_SEARCH_ENTITIES_TOOL = "SearchEntities"
_GET_ENTITY_STATE_TOOL = "GetEntityState"
_WORD_RE = re.compile(r"[a-z0-9]+")
_ENTITY_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "homerseklet": ("temperature",),
    "hofok": ("temperature",),
    "fok": ("temperature",),
    "paratartalom": ("humidity",),
    "haloszoba": ("bedroom",),
    "nappali": ("living", "room"),
    "konyha": ("kitchen",),
    "furdoszoba": ("bathroom",),
    "folyoso": ("corridor",),
    "eloszoba": ("lobby",),
    "garazs": ("garage",),
    "terasz": ("terrace",),
    "udvar": ("courtyard",),
    "kert": ("backyard",),
    "kapu": ("gate",),
    "kazan": ("boiler",),
    "szoba": ("room",),
    "villany": ("light",),
    "feny": ("light",),
    "lampa": ("lamp",),
    "suni": ("hedgehog",),
    "doboz": ("box",),
}


@asynccontextmanager
async def _open_streamable_http(
    url: str, headers: dict[str, str]
) -> AsyncIterator[tuple[Any, Any, Any]]:
    if _MCP_V2:
        async with create_mcp_http_client(headers=headers) as http_client:
            async with streamable_http_client(
                url, http_client=http_client
            ) as streams:
                yield streams[0], streams[1], None
        return

    async with streamable_http_client(url, headers=headers) as streams:
        yield streams


def _tool_input_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "input_schema", None)
    return schema if schema is not None else tool.inputSchema


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return " ".join(_WORD_RE.findall(ascii_text))


def _tokens(text: str) -> list[str]:
    tokens = [
        token for token in _normalize(text).split() if len(token) > 1
    ]
    expanded = list(tokens)
    for token in tokens:
        for source, replacements in _ENTITY_TOKEN_ALIASES.items():
            if token == source or (
                min(len(token), len(source)) >= 4
                and (token.startswith(source) or source.startswith(token))
            ):
                expanded.extend(replacements)
    return list(dict.fromkeys(expanded))


def _token_matches(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) >= 4 and (
        left.startswith(right) or right.startswith(left)
    ):
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= 0.82


@dataclass(frozen=True)
class EntitySnapshot:
    name: str
    domain: str
    state: str = ""
    area: str = ""
    details: str = ""

    def search_text(self) -> str:
        return " ".join(part for part in (self.name, self.area) if part)

    def summary(self, *, include_state: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "domain": self.domain,
        }
        if self.area:
            result["area"] = self.area
        if include_state:
            result["state"] = self.state
            if self.details:
                result["details"] = self.details
        return result


@dataclass
class MCPServer:
    name: str
    url: str
    token: str
    keywords: list[str] = field(default_factory=list)
    session: ClientSession | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    entities: list[EntitySnapshot] = field(default_factory=list)
    has_live_context: bool = False
    entity_refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _connected: bool = False


class MCPManager:
    def __init__(self) -> None:
        self._servers: dict[str, MCPServer] = {}
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._connection_tasks: list[asyncio.Task[None]] = []

    def _build_server_list(self) -> list[MCPServer]:
        servers: list[MCPServer] = []

        # Local connection via Supervisor API
        local_name = os.environ.get("LOCAL_SITE_NAME", "local")
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
        local_kw_raw = os.environ.get("LOCAL_SITE_KEYWORDS", "").strip()
        local_keywords = [k.strip().lower() for k in local_kw_raw.split(",") if k.strip()] if local_kw_raw else []
        if supervisor_token:
            servers.append(MCPServer(
                name=local_name,
                url="http://supervisor/core/api/mcp",
                token=supervisor_token,
                keywords=local_keywords,
            ))
        else:
            logger.warning("SUPERVISOR_TOKEN not set — skipping local MCP")

        # Remote connections from config
        # bashio outputs list items as newline-delimited JSON objects, not a JSON array
        raw = os.environ.get("REMOTE_SITES", "").strip()
        logger.debug("REMOTE_SITES env length: %d", len(raw))
        if raw:
            remote_servers: list[dict[str, str]] = []
            try:
                parsed = json.loads(raw)
                # If bashio returned a proper JSON array
                if isinstance(parsed, list):
                    remote_servers = parsed
                else:
                    remote_servers = [parsed]
            except json.JSONDecodeError:
                # Newline-delimited JSON objects
                for line in raw.splitlines():
                    line = line.strip()
                    if line:
                        try:
                            remote_servers.append(json.loads(line))
                        except json.JSONDecodeError:
                            logger.exception("Failed to parse remote server line: %r", line)
            logger.debug("Parsed %d remote server(s)", len(remote_servers))
            for entry in remote_servers:
                kw_raw = entry.get("keywords", "")
                keywords = [k.strip().lower() for k in kw_raw.split(",") if k.strip()] if kw_raw else []
                servers.append(MCPServer(
                    name=entry["name"],
                    url=entry["url"],
                    token=entry["token"],
                    keywords=keywords,
                ))
        else:
            logger.debug("No remote MCP servers configured")

        return servers

    async def connect_all(self) -> None:
        servers = self._build_server_list()
        logger.info("Will attempt to connect to %d MCP server(s): %s",
                     len(servers), [s.name for s in servers])
        ready_events: list[asyncio.Event] = []
        for server in servers:
            ready = asyncio.Event()
            ready_events.append(ready)
            task = asyncio.create_task(self._connection_loop(server, ready))
            self._connection_tasks.append(task)
        await asyncio.gather(*[e.wait() for e in ready_events])

    _RETRY_DELAYS = [5, 10, 30, 60, 120]  # seconds, then repeat last

    async def _connection_loop(self, server: MCPServer, ready: asyncio.Event) -> None:
        """Manage a single MCP connection with automatic reconnection on failure."""
        attempt = 0
        site_type = "local" if server.url.startswith("http://supervisor") else "remote"
        site_label = f" at {server.url}" if site_type == "remote" else " via Supervisor API"

        while not self._shutdown_event.is_set():
            try:
                headers = {"Authorization": f"Bearer {server.token}"}
                async with _open_streamable_http(
                    server.url, headers
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        server.session = session
                        server._connected = True
                        attempt = 0  # reset on success

                        # Discover tools
                        result = await session.list_tools()
                        discovered_tools = [
                            {
                                "name": tool.name,
                                "description": tool.description or "",
                                "inputSchema": _tool_input_schema(tool),
                            }
                            for tool in result.tools
                        ]
                        server.has_live_context = any(
                            tool["name"] == _LIVE_CONTEXT_TOOL
                            for tool in discovered_tools
                        )
                        server.tools = [
                            tool
                            for tool in discovered_tools
                            if tool["name"] != _LIVE_CONTEXT_TOOL
                        ]
                        if server.has_live_context:
                            server.tools.extend(self._entity_tools())

                        self._servers[server.name] = server

                        # Cache exposed entities for relevant prompt injection
                        # and targeted virtual lookup tools.
                        if server.has_live_context:
                            try:
                                server.entities = await self._fetch_entities(
                                    session
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to fetch entities for %s",
                                    server.name,
                                    exc_info=True,
                                )
                                server.entities = []
                        else:
                            server.entities = []

                        logger.info(
                            "Connected to %s MCP server (%s)%s — %d tools, "
                            "%d exposed entities",
                            site_type, server.name, site_label,
                            len(server.tools), len(server.entities),
                        )
                        ready.set()
                        await self._shutdown_event.wait()
                        return  # clean shutdown

            except BaseException as exc:
                # Clean up stale state so the site isn't listed while disconnected
                server.session = None
                server._connected = False
                server.tools = []
                server.entities = []
                server.has_live_context = False
                self._servers.pop(server.name, None)

                delay = self._RETRY_DELAYS[min(attempt, len(self._RETRY_DELAYS) - 1)]
                exc_name = type(exc).__name__
                if attempt == 0:
                    logger.warning(
                        "Failed to connect to %s MCP server (%s)%s: %s — retrying in %ds",
                        site_type, server.name, site_label, exc_name, delay,
                    )
                else:
                    logger.warning(
                        "Reconnect attempt %d for %s MCP server (%s) failed: %s — retrying in %ds",
                        attempt, site_type, server.name, exc_name, delay,
                    )

                # Signal ready after first failure so startup isn't blocked forever
                ready.set()
                attempt += 1

                # Wait for retry delay or shutdown, whichever comes first
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(), timeout=delay
                    )
                    return  # shutdown requested during wait
                except asyncio.TimeoutError:
                    pass  # retry delay elapsed, try again

    @staticmethod
    def _entity_tools() -> list[dict[str, Any]]:
        return [
            {
                "name": _LIVE_CONTEXT_TOOL,
                "description": (
                    "Compatibility tool for targeted current context. Unlike "
                    "the underlying Home Assistant tool, this requires a "
                    "search query and returns current state for at most 10 "
                    "matching exposed entities, never the complete home. New "
                    "prompts should prefer SearchEntities followed by "
                    "GetEntityState."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Entity, area, or sensor phrase in the English "
                                "entity naming language. Omit the site name."
                            ),
                        },
                        "domain": {"type": "string"},
                        "area": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": _SEARCH_ENTITIES_TOOL,
                "description": (
                    "Search exposed Home Assistant entities at this site by "
                    "translated name, area, or type. Use this before a control "
                    "call when the exact entity name is not in the prompt. "
                    "Returns at most 10 candidates and never returns all states."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Entity or area phrase in the English entity "
                                "naming language. Translate the user's wording "
                                "and omit the site name."
                            ),
                        },
                        "domain": {
                            "type": "string",
                            "description": (
                                "Optional Home Assistant domain filter, for "
                                "example light, switch, sensor, or climate."
                            ),
                        },
                        "area": {
                            "type": "string",
                            "description": "Optional area/room filter.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": _GET_ENTITY_STATE_TOOL,
                "description": (
                    "Get the current state for one exact exposed entity name "
                    "at this site. Duplicate names across areas return at most "
                    "10 matches and can be narrowed with domain or area. "
                    "The orchestrator refreshes the complete Home Assistant "
                    "snapshot internally but returns only exact name matches. "
                    "Use SearchEntities first when the exact name is unknown."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Exact entity name returned by SearchEntities "
                                "or shown in the relevant entity context."
                            ),
                        },
                        "domain": {
                            "type": "string",
                            "description": "Optional exact domain filter.",
                        },
                        "area": {
                            "type": "string",
                            "description": "Optional exact area/room filter.",
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        ]

    @staticmethod
    def _result_text(result: Any) -> str:
        text = "\n".join(
            item.text for item in result.content if hasattr(item, "text")
        )
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        if isinstance(parsed, dict) and isinstance(parsed.get("result"), str):
            return parsed["result"]
        return text

    @staticmethod
    def _clean_scalar(value: str) -> str:
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            return value[1:-1]
        return value

    @classmethod
    def _parse_entities(cls, text: str) -> list[EntitySnapshot]:
        blocks: list[list[str]] = []
        current: list[str] = []
        for line in text.splitlines():
            if line.startswith("- names:"):
                if current:
                    blocks.append(current)
                current = [line]
            elif current:
                current.append(line)
        if current:
            blocks.append(current)

        entities: list[EntitySnapshot] = []
        seen: set[tuple[str, str, str]] = set()
        for block in blocks:
            name = cls._clean_scalar(block[0].split(":", 1)[1])
            domain = state = area = ""
            for line in block[1:]:
                if line.startswith("  domain:"):
                    domain = cls._clean_scalar(line.split(":", 1)[1])
                elif line.startswith("  state:"):
                    state = cls._clean_scalar(line.split(":", 1)[1])
                elif line.startswith("  areas:"):
                    area = cls._clean_scalar(line.split(":", 1)[1])

            if not name or not domain:
                continue
            key = (name, domain, area)
            if key in seen:
                continue
            seen.add(key)
            entities.append(EntitySnapshot(
                name=name,
                domain=domain,
                state=state,
                area=area,
                details="\n".join(block),
            ))
        return entities

    async def _fetch_entities(
        self, session: ClientSession
    ) -> list[EntitySnapshot]:
        result = await session.call_tool(_LIVE_CONTEXT_TOOL, {})
        return self._parse_entities(self._result_text(result))

    async def _refresh_entities(
        self, server: MCPServer
    ) -> list[EntitySnapshot]:
        if not server.session or not server.has_live_context:
            raise RuntimeError(
                f"Live entity context is unavailable for site '{server.name}'"
            )
        async with server.entity_refresh_lock:
            entities = await self._fetch_entities(server.session)
            server.entities = entities
            return entities

    @staticmethod
    def _entity_score(entity: EntitySnapshot, query: str) -> int:
        normalized_query = _normalize(query)
        normalized_name = _normalize(entity.name)
        normalized_area = _normalize(entity.area)
        if not normalized_query or not normalized_name:
            return 0
        if normalized_query == normalized_name:
            return 100
        name_tokens = _tokens(entity.name)
        if normalized_query in normalized_name or (
            normalized_name in normalized_query and len(name_tokens) > 1
        ):
            return 95

        query_tokens = _tokens(query)
        area_tokens = _tokens(entity.area)
        matched_name = sum(
            any(_token_matches(query_token, name_token)
                for query_token in query_tokens)
            for name_token in name_tokens
        )
        matched_area = sum(
            any(_token_matches(query_token, area_token)
                for query_token in query_tokens)
            for area_token in area_tokens
        )
        name_coverage = matched_name / max(len(name_tokens), 1)
        query_coverage = matched_name / max(len(query_tokens), 1)
        token_score = round(name_coverage * 75 + query_coverage * 25)
        area_score = round(
            matched_area / max(len(area_tokens), 1) * 55
        )
        sequence_score = round(
            difflib.SequenceMatcher(
                None, normalized_query, normalized_name
            ).ratio() * 70
        )
        if normalized_area and (
            normalized_query in normalized_area
            or normalized_area in normalized_query
        ):
            area_score = max(area_score, 65)
        combined_score = min(
            100, token_score + round(area_score * 0.3)
        )
        return max(combined_score, area_score, sequence_score)

    @classmethod
    def _search_entity_records(
        cls,
        entities: list[EntitySnapshot],
        query: str,
        *,
        domain: str = "",
        area: str = "",
        limit: int = 5,
    ) -> list[tuple[int, EntitySnapshot]]:
        normalized_domain = _normalize(domain)
        normalized_area = _normalize(area)
        scored: list[tuple[int, EntitySnapshot]] = []
        for entity in entities:
            filter_score = 0
            area_mismatch = False
            if normalized_domain and _normalize(entity.domain) != normalized_domain:
                continue
            if normalized_domain:
                filter_score = 35
            if normalized_area:
                entity_area = _normalize(entity.area)
                area_mismatch = (
                    normalized_area not in entity_area
                    and entity_area not in normalized_area
                    and difflib.SequenceMatcher(
                        None, normalized_area, entity_area
                    ).ratio() < 0.72
                )
                if not area_mismatch:
                    filter_score = max(filter_score, 70)
            score = max(cls._entity_score(entity, query), filter_score)
            if normalized_area and area_mismatch:
                score = max(0, score - 10)
            if score >= 30:
                scored.append((score, entity))
        scored.sort(key=lambda item: (-item[0], item[1].name, item[1].domain))
        return scored[:limit]

    def get_entity_context(
        self,
        sites: list[str] | None = None,
        *,
        query: str,
        limit: int = 5,
    ) -> str:
        """Return only cached entities relevant to the current utterance."""
        parts = []
        for server in self._servers.values():
            if sites is not None and server.name not in sites:
                continue
            matches = self._search_entity_records(
                server.entities,
                query,
                limit=min(len(server.entities), limit * 3),
            )
            if not matches:
                continue
            entries: list[str] = []
            seen: set[tuple[str, str]] = set()
            for _, entity in matches:
                key = (entity.name, entity.domain)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(f"{entity.name} [{entity.domain}]")
                if len(entries) == limit:
                    break
            parts.append(f"{server.name.upper()}: {', '.join(entries)}")
        return "\n".join(parts)

    @classmethod
    def _search_arguments(
        cls, arguments: dict[str, Any]
    ) -> tuple[str, str, str, int]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("A non-empty 'query' is required")
        domain = arguments.get("domain") or ""
        area = arguments.get("area") or ""
        limit = arguments.get("limit", 5)
        if limit is None:
            limit = 5
        if not isinstance(domain, str) or not isinstance(area, str):
            raise ValueError("'domain' and 'area' must be strings")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise ValueError("'limit' must be an integer between 1 and 10")
        return query, domain, area, limit

    @classmethod
    def _search_response(
        cls, server: MCPServer, arguments: dict[str, Any]
    ) -> str:
        query, domain, area, limit = cls._search_arguments(arguments)
        matches = cls._search_entity_records(
            server.entities,
            query,
            domain=domain,
            area=area,
            limit=limit,
        )
        return json.dumps({
            "success": True,
            "site": server.name,
            "query": query,
            "count": len(matches),
            "matches": [
                {
                    **entity.summary(),
                    "confidence": score,
                }
                for score, entity in matches
            ],
        }, ensure_ascii=False)

    async def _targeted_context_response(
        self, server: MCPServer, arguments: dict[str, Any]
    ) -> str:
        query, domain, area, limit = self._search_arguments(arguments)
        entities = await self._refresh_entities(server)
        matches = self._search_entity_records(
            entities,
            query,
            domain=domain,
            area=area,
            limit=limit,
        )
        return json.dumps({
            "success": True,
            "site": server.name,
            "query": query,
            "count": len(matches),
            "entities": [
                {
                    **entity.summary(include_state=True),
                    "confidence": score,
                }
                for score, entity in matches
            ],
        }, ensure_ascii=False)

    async def _state_response(
        self, server: MCPServer, arguments: dict[str, Any]
    ) -> str:
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("GetEntityState requires a non-empty 'name'")
        domain = arguments.get("domain") or ""
        area = arguments.get("area") or ""
        if not isinstance(domain, str) or not isinstance(area, str):
            raise ValueError("'domain' and 'area' must be strings")

        entities = await self._refresh_entities(server)
        normalized_name = _normalize(name)
        normalized_domain = _normalize(domain)
        normalized_area = _normalize(area)
        all_matches = [
            entity
            for entity in entities
            if _normalize(entity.name) == normalized_name
            and (
                not normalized_domain
                or _normalize(entity.domain) == normalized_domain
            )
            and (
                not normalized_area
                or _normalize(entity.area) == normalized_area
            )
        ]
        matches = all_matches[:10]
        if matches:
            return json.dumps({
                "success": True,
                "site": server.name,
                "name": name,
                "count": len(matches),
                "truncated": len(all_matches) > len(matches),
                "entities": [
                    entity.summary(include_state=True)
                    for entity in matches
                ],
            }, ensure_ascii=False)

        candidates = self._search_entity_records(entities, name, limit=5)
        return json.dumps({
            "success": False,
            "site": server.name,
            "name": name,
            "error": "No exact exposed entity name matched",
            "candidates": [
                {
                    **entity.summary(),
                    "confidence": score,
                }
                for score, entity in candidates
            ],
        }, ensure_ascii=False)

    def get_all_tools_openai(self, sites: list[str] | None = None) -> list[dict[str, Any]]:
        """Return tools in OpenAI function-calling format, optionally filtered by site."""
        tools = []
        for server in self._servers.values():
            if sites is not None and server.name not in sites:
                continue
            for tool in server.tools:
                namespaced_name = f"{server.name}{SEPARATOR}{tool['name']}"
                tools.append({
                    "type": "function",
                    "function": {
                        "name": namespaced_name,
                        "description": f"[{server.name}] {tool['description']}",
                        "parameters": tool.get("inputSchema", {}),
                    },
                })
        return tools

    async def call_tool(self, namespaced_name: str, arguments: dict[str, Any]) -> Any:
        """Parse site prefix and route call to the correct MCP server."""
        if SEPARATOR not in namespaced_name:
            raise ValueError(f"Tool name must be namespaced: {namespaced_name}")

        site, tool_name = namespaced_name.split(SEPARATOR, 1)
        server = self._servers.get(site)
        if not server or not server.session:
            raise ValueError(f"MCP server '{site}' is not connected")

        available_tools = {tool["name"] for tool in server.tools}
        if tool_name not in available_tools:
            raise ValueError(
                f"Tool '{namespaced_name}' is not available to the model"
            )
        if tool_name == _LIVE_CONTEXT_TOOL:
            return await self._targeted_context_response(server, arguments)
        if tool_name == _SEARCH_ENTITIES_TOOL:
            await self._refresh_entities(server)
            return self._search_response(server, arguments)
        if tool_name == _GET_ENTITY_STATE_TOOL:
            return await self._state_response(server, arguments)

        result = await server.session.call_tool(tool_name, arguments)
        # Extract text content from the result
        parts = []
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(item.text)
            else:
                parts.append(str(item))
        return "\n".join(parts)

    @property
    def connected_sites(self) -> list[str]:
        return list(self._servers.keys())

    @property
    def site_keywords(self) -> dict[str, list[str]]:
        return {s.name: s.keywords for s in self._servers.values() if s.keywords}

    async def close(self) -> None:
        self._shutdown_event.set()
        if self._connection_tasks:
            await asyncio.gather(*self._connection_tasks, return_exceptions=True)
            self._connection_tasks.clear()
        self._servers.clear()
