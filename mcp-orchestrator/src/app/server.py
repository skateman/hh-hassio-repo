from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global llm_client
    await mcp_manager.connect_all()
    logger.info("Connected sites: %s", mcp_manager.connected_sites)
    llm_client = LLMClient(mcp_manager)
    yield
    await mcp_manager.close()


app = FastAPI(lifespan=lifespan)


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


@app.post("/api/chat")
async def ollama_chat(request: OllamaChatRequest) -> OllamaChatResponse | StreamingResponse | JSONResponse:
    if llm_client is None:
        return _error_response(500, "LLM client not initialized")

    messages = [ChatMessage(role=m.role, content=m.content) for m in request.messages]

    try:
        if request.stream:
            return StreamingResponse(
                llm_client.chat_stream_ollama(messages),
                media_type="application/x-ndjson",
            )
        response = await llm_client.chat(messages)
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
