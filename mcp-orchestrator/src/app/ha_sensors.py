from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SUPERVISOR_API = "http://supervisor/core/api"

_SENSORS: list[tuple[str, str, str, str, str]] = [
    # (entity_id, stats_key, friendly_name, unit, icon)
    ("sensor.mcp_orchestrator_requests", "requests", "MCP Orchestrator Requests", "requests", "mdi:chat"),
    ("sensor.mcp_orchestrator_prompt_tokens", "prompt_tokens", "MCP Orchestrator Prompt Tokens", "tokens", "mdi:arrow-up"),
    ("sensor.mcp_orchestrator_completion_tokens", "completion_tokens", "MCP Orchestrator Completion Tokens", "tokens", "mdi:arrow-down"),
    ("sensor.mcp_orchestrator_total_tokens", "total_tokens", "MCP Orchestrator Total Tokens", "tokens", "mdi:sigma"),
    ("sensor.mcp_orchestrator_tool_calls", "tool_calls", "MCP Orchestrator Tool Calls", "calls", "mdi:wrench"),
]

_registry_done = False


async def _mark_diagnostic(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    """Mark all sensors as diagnostic in the entity registry (once)."""
    global _registry_done
    if _registry_done:
        return
    for entity_id, *_ in _SENSORS:
        try:
            await client.post(
                f"{_SUPERVISOR_API}/config/entity_registry/{entity_id}",
                headers=headers,
                json={"entity_category": "diagnostic"},
            )
        except Exception:
            logger.debug("Could not set entity_category for %s", entity_id)
    _registry_done = True


async def push_stats(snapshot: dict[str, Any]) -> None:
    """Push current stats to HA as sensor entities via the Supervisor API."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return

    totals = snapshot.get("totals", {})
    by_site = snapshot.get("by_site", {})
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        for entity_id, key, friendly_name, unit, icon in _SENSORS:
            attrs: dict[str, Any] = {
                "friendly_name": friendly_name,
                "state_class": "total_increasing",
                "unit_of_measurement": unit,
                "icon": icon,
            }
            for site, site_stats in by_site.items():
                attrs[f"site_{site}"] = site_stats.get(key, 0)

            try:
                await client.post(
                    f"{_SUPERVISOR_API}/states/{entity_id}",
                    headers=headers,
                    json={"state": totals.get(key, 0), "attributes": attrs},
                )
            except Exception:
                logger.warning("Failed to push %s to HA", entity_id)

        await _mark_diagnostic(client, headers)
