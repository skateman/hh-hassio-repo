from __future__ import annotations

import asyncio
import copy
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from openai import APITimeoutError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_REPLY = "Request timed out."


@dataclass(frozen=True)
class TimeoutSettings:
    llm_seconds: float
    request_seconds: float
    reply: str

    @classmethod
    def from_environment(cls) -> TimeoutSettings:
        def seconds(name: str, default: int) -> float:
            try:
                value = float(os.environ.get(name, str(default)))
            except ValueError as exc:
                raise ValueError(f"{name} must be a positive number of seconds") from exc
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive number of seconds")
            return value

        llm_seconds = seconds("LLM_TIMEOUT_SECONDS", 10)
        request_seconds = seconds("REQUEST_TIMEOUT_SECONDS", 30)
        reply = os.environ.get("TIMEOUT_REPLY", DEFAULT_TIMEOUT_REPLY).strip()
        if not reply:
            raise ValueError("TIMEOUT_REPLY must not be empty")
        return cls(llm_seconds, request_seconds, reply)


class RequestTimeoutError(RuntimeError):
    def __init__(self, stage: str):
        self.stage = stage
        super().__init__(f"Request timed out during {stage}")


@dataclass
class RequestBudget:
    llm_timeout: float
    request_timeout: float
    started_at: float = field(default_factory=time.monotonic)
    retry_used: bool = False
    speech_started: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)
    action_results: dict[tuple[str, str], tuple[str, str | None]] = field(default_factory=dict)

    @property
    def deadline(self) -> float:
        return self.started_at + self.request_timeout

    def check(self, stage: str) -> None:
        if time.monotonic() >= self.deadline:
            raise RequestTimeoutError(stage)

    def _start_attempt(self, iteration: int, number: int) -> tuple[float, dict[str, Any]]:
        self.check("llm")
        started = time.monotonic()
        record = {
            "iteration": iteration,
            "attempt": number,
            "started_ms": round((started - self.started_at) * 1000),
            "duration_ms": 0,
            "outcome": "cancelled",
            "usage_reported": False,
        }
        self.attempts.append(record)
        return started, record

    def _retry_timeout(self, iteration: int) -> bool:
        if (
            self.retry_used
            or self.speech_started
            or time.monotonic() >= self.deadline
        ):
            return False
        self.retry_used = True
        logger.warning("LLM iteration %d timed out; retrying this model step once", iteration)
        return True

    @staticmethod
    def _check_attempt_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("Model attempt deadline reached")

    async def response(
        self,
        create: Callable[..., Awaitable[Any]],
        kwargs: dict[str, Any],
        iteration: int,
    ) -> Any:
        number = 0
        while True:
            number += 1
            started, record = self._start_attempt(iteration, number)
            deadline = min(self.deadline, started + self.llm_timeout)
            try:
                self._check_attempt_deadline(deadline)
                async with asyncio.timeout_at(deadline):
                    response = await create(**copy.deepcopy(kwargs))
                self._check_attempt_deadline(deadline)
                if response.status != "completed":
                    details = (
                        getattr(response, "error", None)
                        or getattr(response, "incomplete_details", None)
                        or "no error details"
                    )
                    raise RuntimeError(f"Azure OpenAI returned {response.status}: {details}")
                record["outcome"] = "completed"
                record["usage_reported"] = response.usage is not None
                return response
            except (TimeoutError, APITimeoutError) as exc:
                record["outcome"] = "timeout"
                if self._retry_timeout(iteration):
                    continue
                raise RequestTimeoutError("llm") from exc
            except Exception as exc:
                record["outcome"] = "error"
                if number > 1:
                    logger.exception("The LLM timeout retry failed")
                    raise RequestTimeoutError("llm") from exc
                raise
            finally:
                record["duration_ms"] = round((time.monotonic() - started) * 1000)

    async def events(
        self,
        create: Callable[..., Awaitable[Any]],
        kwargs: dict[str, Any],
        iteration: int,
    ) -> AsyncIterator[Any]:
        number = 0
        while True:
            number += 1
            started, record = self._start_attempt(iteration, number)
            deadline = min(self.deadline, started + self.llm_timeout)
            stream = None
            try:
                self._check_attempt_deadline(deadline)
                async with asyncio.timeout_at(deadline):
                    stream = await create(**copy.deepcopy(kwargs))
                iterator = aiter(stream)
                while True:
                    # Keep timeouts inside model awaits, not across a yielded
                    # text chunk where the caller may be sending audio.
                    self._check_attempt_deadline(deadline)
                    async with asyncio.timeout_at(deadline):
                        event = await anext(iterator)
                    self._check_attempt_deadline(deadline)
                    if event.type in {"error", "response.failed", "response.incomplete"}:
                        details = event.model_dump(mode="json", exclude_none=True)
                        raise RuntimeError(
                            f"Azure OpenAI stream ended with {event.type}: {details}"
                        )
                    if event.type == "response.completed":
                        record["outcome"] = "completed"
                        record["usage_reported"] = event.response.usage is not None
                        yield event
                        return
                    yield event
            except StopAsyncIteration as exc:
                record["outcome"] = "error"
                if number > 1:
                    raise RequestTimeoutError("llm") from exc
                raise RuntimeError("Azure OpenAI stream ended without a completed response") from exc
            except (TimeoutError, APITimeoutError) as exc:
                record["outcome"] = "timeout"
                if self._retry_timeout(iteration):
                    continue
                raise RequestTimeoutError("llm") from exc
            except Exception as exc:
                record["outcome"] = "error"
                if number > 1:
                    logger.exception("The LLM timeout retry failed")
                    raise RequestTimeoutError("llm") from exc
                raise
            finally:
                try:
                    if stream is not None:
                        await stream.close()
                finally:
                    record["duration_ms"] = round((time.monotonic() - started) * 1000)
