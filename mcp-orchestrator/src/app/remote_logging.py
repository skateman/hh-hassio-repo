"""Opt-in interaction logging to Azure Blob Storage.

When enabled, every completed voice-assistant interaction is logged as a JSONL
record to an append blob. Daily rotation keeps one blob per day:
``logs/YYYY-MM-DD.jsonl``.

The feature is entirely opt-in: unless a valid connection string is
provided at startup, the logger stays disabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_CONTAINER_NAME = "mcp-orchestrator-logs"


class RemoteLogger:
    """Fire-and-forget logger backed by Azure Blob Storage append blobs."""

    def __init__(self, connection_string: str) -> None:
        # Import lazily so the module can be loaded even when the SDK is
        # not installed (logger simply won't be instantiated).
        from azure.storage.blob.aio import ContainerClient

        # Support both full connection strings (with AccountName) and
        # container-scoped SAS URLs (https://account.blob.core.windows.net/container?sv=...)
        if connection_string.startswith("http"):
            self._container = ContainerClient.from_container_url(connection_string)
        else:
            self._container = ContainerClient.from_connection_string(
                connection_string, container_name=_CONTAINER_NAME,
            )
        # Container-scoped SAS URLs point at an existing container,
        # so we can skip the ensure step.
        self._container_ensured = connection_string.startswith("http")
        self._append_lock = asyncio.Lock()
        self._pending: set[asyncio.Task[None]] = set()

    async def _ensure_container(self) -> None:
        """Create the blob container if it does not exist yet."""
        if self._container_ensured:
            return
        from azure.core.exceptions import ResourceExistsError

        try:
            await self._container.create_container()
            logger.info("Created blob container %s", _CONTAINER_NAME)
        except ResourceExistsError:
            pass
        self._container_ensured = True

    async def log_event(self, event: dict[str, Any]) -> None:
        """Append a single JSONL record to today's blob.

        This is fire-and-forget: exceptions are caught and logged locally
        but never propagated to the caller.
        """
        try:
            from azure.core.exceptions import ResourceNotFoundError

            now = datetime.now(timezone.utc)
            record = {"ts": now.isoformat(), **event}
            blob_name = f"logs/{now.strftime('%Y-%m-%d')}.jsonl"
            line = json.dumps(record, ensure_ascii=False) + "\n"

            async with self._append_lock:
                await self._ensure_container()
                blob = self._container.get_blob_client(blob_name)
                try:
                    await blob.append_block(line.encode())
                except ResourceNotFoundError:
                    await blob.create_append_blob()
                    await blob.append_block(line.encode())

            logger.debug("Logged interaction to %s (%d bytes)", blob_name, len(line))
        except Exception:
            logger.warning("Failed to log interaction to blob storage", exc_info=True)

    def log_event_bg(self, event: dict[str, Any]) -> None:
        """Schedule an interaction log write without delaying the response."""
        task = asyncio.create_task(self.log_event(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def close(self) -> None:
        """Flush pending records and close the underlying container client."""
        if self._pending:
            await asyncio.gather(*self._pending)
        try:
            await self._container.close()
        except Exception:
            logger.warning("Failed to close remote logging client", exc_info=True)
