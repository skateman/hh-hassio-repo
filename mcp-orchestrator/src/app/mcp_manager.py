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
        if supervisor_token:
            servers.append(MCPServer(
                name=local_name,
                url="http://supervisor/core/api/mcp",
                token=supervisor_token,
            ))

        # Remote connections from config
        raw = os.environ.get("REMOTE_MCP_SERVERS", "")
        if raw:
            try:
                remote_servers = json.loads(raw)
            except json.JSONDecodeError:
                remote_servers = []
            for entry in remote_servers:
                servers.append(MCPServer(
                    name=entry["name"],
                    url=entry["url"],
                    token=entry["token"],
                ))

        return servers

    async def connect_all(self) -> None:
        servers = self._build_server_list()
        for server in servers:
            await self._connect(server)

    async def _connect(self, server: MCPServer) -> None:
        try:
            headers = {"Authorization": f"Bearer {server.token}"}
            ctx = streamablehttp_client(server.url, headers=headers)
            read, write, _ = await ctx.__aenter__()
            self._contexts.append(ctx)

            session = ClientSession(read, write)
            await session.__aenter__()
            self._contexts.append(session)

            await session.initialize()
            server.session = session
            server._connected = True

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
        except Exception:
            logger.exception("Failed to connect to MCP server %s at %s", server.name, server.url)

    def get_all_tools_openai(self) -> list[dict[str, Any]]:
        """Return all tools in OpenAI function-calling format, namespaced by site."""
        tools = []
        for server in self._servers.values():
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

    async def close(self) -> None:
        for ctx in reversed(self._contexts):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                logger.exception("Error closing MCP context")
        self._contexts.clear()
        self._servers.clear()
