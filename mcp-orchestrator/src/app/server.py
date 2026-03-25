from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .ha_sensors import push_stats
from .llm import LLMClient
from .mcp_manager import MCPManager
from .models import (
    ChatMessage,
    ErrorDetail,
    ErrorResponse,
    OllamaChatRequest,
    OllamaChatResponse,
    OllamaMessage,
    OllamaModel,
    OllamaModelList,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

mcp_manager = MCPManager()
llm_client: LLMClient | None = None


async def _push_stats_safe() -> None:
    if llm_client:
        try:
            await push_stats(llm_client.stats.snapshot())
        except Exception:
            logger.warning("Stats push to HA failed")


async def _stream_and_push(stream: AsyncIterator[str]) -> AsyncIterator[str]:
    """Wrap a stream to push stats to HA after it finishes."""
    async for chunk in stream:
        yield chunk
    await _push_stats_safe()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global llm_client
    await mcp_manager.connect_all()
    logger.info("Connected sites: %s", mcp_manager.connected_sites)
    llm_client = LLMClient(mcp_manager)
    yield
    await mcp_manager.close()


app = FastAPI(lifespan=lifespan)

_api_key = os.environ.get("API_KEY", "")
_HASSIO_NETWORK = ipaddress.ip_network("172.30.32.0/23")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if _api_key and request.client:
        client_ip = ipaddress.ip_address(request.client.host)
        if not (client_ip.is_loopback or client_ip in _HASSIO_NETWORK):
            token = ""
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:]
            if not hmac.compare_digest(token, _api_key):
                return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return await call_next(request)


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error=ErrorDetail(message=message, code=str(status_code))
        ).model_dump(),
    )


@app.get("/api/tags")
async def ollama_tags() -> OllamaModelList:
    return OllamaModelList(models=[OllamaModel(
        name="ha-orchestrator:latest",
        model="ha-orchestrator:latest",
        modified_at=time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()),
    )])


@app.post("/api/chat", response_model=None)
async def ollama_chat(request: OllamaChatRequest):
    if llm_client is None:
        return _error_response(500, "LLM client not initialized")

    messages = [ChatMessage(role=m.role, content=m.content) for m in request.messages]

    try:
        if request.stream:
            return StreamingResponse(
                _stream_and_push(llm_client.chat_stream_ollama(messages)),
                media_type="application/x-ndjson",
            )
        response = await llm_client.chat(messages)
        await _push_stats_safe()
        return OllamaChatResponse(
            model="ha-orchestrator",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()),
            message=OllamaMessage(
                role="assistant",
                content=response.choices[0].message.content or "",
            ),
            done=True,
        )
    except Exception:
        logger.exception("Error processing Ollama chat")
        return _error_response(500, "Internal server error")
