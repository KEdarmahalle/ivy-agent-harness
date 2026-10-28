"""OpenAI-Compatible /v1/chat/completions and /v1/models API Router.

Allows Open-WebUI, LibreChat, Continue.dev, Dify, LangChain, and any OpenAI SDK client
to use Ivy Agent Harness as a drop-in OpenAI-compatible backend provider.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from harness.sdk import AgentHarness, AgentResult, RunConfig


async def _stream_events(harness: AgentHarness, prompt: str, config: RunConfig):
    iterator = iter(harness.stream(prompt, config=config))
    sentinel = object()
    while True:
        event = await asyncio.to_thread(next, iterator, sentinel)
        if event is sentinel:
            return
        yield event


openai_router = APIRouter(prefix="/v1", tags=["OpenAI Compatibility"])


# ==================== OpenAI Request / Response Schemas ====================


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant|tool)$", description="Message role")
    content: str | None = Field("", max_length=100_000, description="Text content; multimodal content is not supported")
    name: str | None = Field(None, max_length=256)


class ChatCompletionRequest(BaseModel):
    model: str = Field("ivy", description="Model identifier (e.g. 'ivy', 'nim.nemotron', 'default')")
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=1000, description="List of chat history messages")
    stream: bool = Field(False, description="Whether to stream back partial tokens as SSE")
    temperature: float | None = Field(0.7, ge=0, le=2, description="Sampling temperature")
    max_tokens: int | None = Field(None, ge=1, le=32768, description="Max tokens to generate")
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    user: str | None = None


class ChatChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatChoiceMessage
    finish_reason: str = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    system_fingerprint: str = "fp_ivy_v010"
    choices: list[ChatChoice]
    usage: UsageInfo


def _extract_prompt_and_system(messages: list[ChatMessage]) -> tuple[str, str | None]:
    """Extract user prompt and system prompt from OpenAI messages list."""
    system_parts: list[str] = []
    user_parts: list[str] = []

    for msg in messages:
        content_str = str(msg.content or "")
        if msg.role == "system":
            system_parts.append(content_str)
        elif msg.role in {"user", "assistant"}:
            if msg.role == "assistant":
                user_parts.append(f"[Previous Assistant]: {content_str}")
            else:
                user_parts.append(content_str)

    system_prompt = "\n\n".join(system_parts) if system_parts else None
    user_prompt = "\n\n".join(user_parts) if user_parts else "Hello"
    return user_prompt, system_prompt


@openai_router.get("/models")
async def list_models(request: Request) -> dict[str, Any]:
    """List available models in standard OpenAI format."""
    harness: AgentHarness = request.app.state.harness
    models_data = [
        {"id": "ivy", "object": "model", "created": 1740528000, "owned_by": "ivy-harness"},
        {"id": "ivy-autonomous", "object": "model", "created": 1740528000, "owned_by": "ivy-harness"},
    ]

    for route_name in harness.models.routes:
        models_data.append({
            "id": route_name,
            "object": "model",
            "created": 1740528000,
            "owned_by": "ivy-routes",
        })

    return {"object": "list", "data": models_data}


@openai_router.get("/models/{model_id}")
async def get_model(model_id: str, request: Request) -> dict[str, Any]:
    """Get single model metadata."""
    return {
        "id": model_id,
        "object": "model",
        "created": 1740528000,
        "owned_by": "ivy-harness",
    }


@openai_router.post("/chat/completions")
async def create_chat_completion(
    req_body: ChatCompletionRequest,
    request: Request,
) -> Any:
    """OpenAI-compatible chat completions endpoint (supports stream=False and stream=True)."""
    harness: AgentHarness = request.app.state.harness
    if req_body.tools or req_body.tool_choice is not None:
        raise HTTPException(status_code=400, detail="OpenAI tool calling is not supported by this endpoint")
    if any(message.role == "tool" for message in req_body.messages):
        raise HTTPException(status_code=400, detail="tool-role history is not supported by this endpoint")
    user_prompt, system_prompt = _extract_prompt_and_system(req_body.messages)

    route = req_body.model if req_body.model in harness.models.routes else None
    run_config = RunConfig(
        route=route,
        system_prompt=system_prompt,
        max_tokens=req_body.max_tokens if req_body.max_tokens is not None else 4096,
    )

    created_timestamp = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if req_body.stream:
        # Stream chunked Server-Sent Events in standard OpenAI chunk format
        async def openai_chunk_stream():
            # 1. Initial role chunk
            first_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_timestamp,
                "model": req_body.model,
                "system_fingerprint": "fp_ivy_v010",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(first_chunk)}\n\n"

            # 2. Token chunks from harness.stream
            async for event in _stream_events(harness, user_prompt, run_config):
                if event.type == "token" and event.delta:
                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_timestamp,
                        "model": req_body.model,
                        "system_fingerprint": "fp_ivy_v010",
                        "choices": [{"index": 0, "delta": {"content": event.delta}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                elif event.type == "turn_completed" and not event.delta:
                    # If whole output completed without delta tokens (e.g. from tool synthesis)
                    pass

            # 3. Final stop chunk
            stop_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_timestamp,
                "model": req_body.model,
                "system_fingerprint": "fp_ivy_v010",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(stop_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(openai_chunk_stream(), media_type="text/event-stream")

    # Non-streaming synchronous response
    result: AgentResult = await asyncio.to_thread(harness.run, user_prompt, run_config)
    output_text = result.final_output

    prompt_toks = max(1, len(user_prompt) // 4)
    comp_toks = max(1, len(output_text) // 4)

    return ChatCompletionResponse(
        id=completion_id,
        object="chat.completion",
        created=created_timestamp,
        model=req_body.model,
        system_fingerprint="fp_ivy_v010",
        choices=[
            ChatChoice(
                index=0,
                message=ChatChoiceMessage(role="assistant", content=output_text),
                finish_reason="stop",
            )
        ],
        usage=UsageInfo(
            prompt_tokens=prompt_toks,
            completion_tokens=comp_toks,
            total_tokens=prompt_toks + comp_toks,
        ),
    )
