from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)

SEPARATOR = "__"


@dataclass
class MCPServer:
    name: str
    url: str
    token: str
    keywords: list[str] = field(default_factory=list)
    session: ClientSession | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    _connected: bool = False


class MCPManager:
    def __init__(self) -> None:
        self._servers: dict[str, MCPServer] = {}
        self._contexts: list[Any] = []

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
        raw = os.environ.get("REMOTE_MCP_SERVERS", "").strip()
        logger.info("REMOTE_MCP_SERVERS env length: %d", len(raw))
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
            logger.info("Parsed %d remote server(s)", len(remote_servers))
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
            logger.info("No remote MCP servers configured")

        return servers

    async def connect_all(self) -> None:
        servers = self._build_server_list()
        logger.info("Will attempt to connect to %d MCP server(s): %s",
                     len(servers), [s.name for s in servers])
        await asyncio.gather(*[self._connect(server) for server in servers])

    async def _connect(self, server: MCPServer) -> None:
        ctx = None
        session = None
        try:
            headers = {"Authorization": f"Bearer {server.token}"}
            ctx = streamablehttp_client(server.url, headers=headers)
            read, write, _ = await ctx.__aenter__()

            session = ClientSession(read, write)
            await session.__aenter__()

            await session.initialize()
            server.session = session
            server._connected = True

            # Keep references for cleanup only on success
            self._contexts.append(session)
            self._contexts.append(ctx)

            # Discover tools
            result = await session.list_tools()
            server.tools = [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.inputSchema,
                }
                for tool in result.tools
            ]

            self._servers[server.name] = server
            logger.info(
                "Connected to %s MCP server (%s)%s — %d tools",
                "local" if server.url.startswith("http://supervisor") else "remote",
                server.name,
                f" at {server.url}" if not server.url.startswith("http://supervisor") else " via Supervisor API",
                len(server.tools),
            )
        except BaseException:
            logger.exception("Failed to connect to MCP server %s at %s", server.name, server.url)
            # Clean up partially-opened contexts so cancel scopes don't leak
            for obj in (session, ctx):
                if obj is not None:
                    try:
                        await obj.__aexit__(None, None, None)
                    except BaseException:
                        logger.debug("Error during cleanup of %s", server.name, exc_info=True)

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
        for ctx in reversed(self._contexts):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                logger.exception("Error closing MCP context")
        self._contexts.clear()
        self._servers.clear()
