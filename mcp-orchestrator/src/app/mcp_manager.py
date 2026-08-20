from __future__ import annotations

import asyncio
import json
import logging
import os
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


@dataclass
class MCPServer:
    name: str
    url: str
    token: str
    keywords: list[str] = field(default_factory=list)
    session: ClientSession | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    entities: str = ""
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
                        server.tools = [
                            {
                                "name": tool.name,
                                "description": tool.description or "",
                                "inputSchema": _tool_input_schema(tool),
                            }
                            for tool in result.tools
                        ]

                        self._servers[server.name] = server

                        # Cache controllable entity names for prompt injection
                        server.entities = await self._cache_entities(session, server.name)

                        logger.info(
                            "Connected to %s MCP server (%s)%s — %d tools, %d entity chars",
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
                server.entities = ""
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

    _CONTROLLABLE_DOMAINS = frozenset({
        "switch", "light", "climate", "media_player", "vacuum", "cover", "fan", "lock",
    })

    async def _cache_entities(self, session: ClientSession, site_name: str) -> str:
        """Call GetLiveContext and extract controllable entity names in compact format.

        Returns a flat string like: "3D Printer [switch], Botond's Room Lamp [light], ..."
        """
        try:
            result = await session.call_tool("GetLiveContext", {})
            text = "\n".join(
                item.text for item in result.content if hasattr(item, "text")
            )
        except Exception:
            logger.warning("Failed to fetch entities for %s", site_name, exc_info=True)
            return ""

        # HA MCP server wraps responses in a JSON envelope:
        # {"success": true, "result": "- names: ...\n  domain: ..."}
        # Unwrap to get the actual multi-line context text.
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and "result" in parsed:
                text = parsed["result"]
        except (json.JSONDecodeError, TypeError):
            pass

        entries: list[str] = []
        current_name = current_domain = None

        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- names:"):
                current_name = stripped.split(":", 1)[1].strip()
                current_domain = None
            elif stripped.startswith("domain:"):
                current_domain = stripped.split(":", 1)[1].strip()
                if (current_name and current_domain
                        and current_domain in self._CONTROLLABLE_DOMAINS):
                    entries.append(f"{current_name} [{current_domain}]")
                current_name = current_domain = None
            elif stripped.startswith("areas:"):
                # Skip area info — flat list only
                pass

        if not entries:
            return ""

        # Deduplicate (same entity may appear under multiple areas)
        return ", ".join(dict.fromkeys(entries))

    def get_entity_context(self, sites: list[str] | None = None) -> str:
        """Return cached entity context for the selected sites.

        Format: ``SITE: entity [type], entity [type], ...`` — one line per site.
        The UPPERCASE prefix helps the model map entities to the correct site's
        MCP tools and visually separates site labels from ``[type]`` brackets.
        """
        parts = []
        for server in self._servers.values():
            if sites is not None and server.name not in sites:
                continue
            if server.entities:
                parts.append(f"{server.name.upper()}: {server.entities}")
        return "\n".join(parts)

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
