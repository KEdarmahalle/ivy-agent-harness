"""FastAPI application for Agent Harness with SSE streaming, Bidirectional WebSockets, REST, and HITL Webhooks."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from harness.config import deep_get, load_config
from harness.engine.checkpointer import build_store
from harness.engine.graph_loader import list_graphs
from harness.engine.runner import GraphRunner
from harness.engine.schemas import (
    ApprovalRequest,
    ModelSmokeRequest,
    ModelSmokeResponse,
    ResumeRequest,
    RunCreateRequest,
    RunSnapshot,
    ToolInvokeRequest,
    ToolInvokeResponse,
)
from harness.events import global_event_bus
from harness.governance import (
    RateLimiter,
    RateLimitExceededError,
    TenantContext,
    authenticate_api_key,
    has_auth_credentials,
    reset_current_tenant,
    set_current_tenant,
)

logger = logging.getLogger(__name__)
from harness.network import OutboundRequestError, safe_http_request, validate_outbound_url
from harness.registries.model_registry import ModelConfigurationError
from harness.sdk import AgentHarness, AgentResult, RunConfig
from harness.secrets import load_secrets


def _is_loopback_peer(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        # Starlette's in-process test client does not have a socket peer.
        return host == "testclient"

# ==================== Pydantic Schemas for Network Tier 3 ====================


class ChatStreamRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=100_000, description="Prompt or user instruction")
    route: str | None = Field(None, description="Optional LLM route override")
    system_prompt: str | None = Field(None, description="Optional system prompt override")
    max_tool_steps: int = Field(10, ge=1, le=50, description="Max tool execution turns")
    auto_approve: bool = Field(False, description="Deprecated and ignored; approval policy is server-controlled")


class AgentRunRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=100_000, description="Task for autonomous agent execution")
    route: str | None = Field(None, description="Optional LLM route override")
    max_tool_steps: int = Field(10, ge=1, le=50, description="Max tool execution turns")
    webhook_url: str | None = Field(None, description="Optional webhook URL for completion alerts")
    stream: bool = Field(False, description="Whether to stream as text/event-stream")


class AgentRunResponse(BaseModel):
    task: str
    success: bool
    final_output: str
    steps: int
    duration_s: float
    tools_executed: list[dict[str, Any]] = Field(default_factory=list)


class SwarmRunRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=100_000, description="Task for multi-agent swarm")
    agents: str = Field("analyst,reviewer", min_length=1, max_length=500, description="Comma-separated agent names")
    pattern: str = Field("fanout", pattern="^(fanout|handoff)$", description="Coordination pattern")


async def _dispatch_webhook(url: str, payload: dict[str, Any], allowed_hosts: list[str]) -> None:
    """Non-blocking background webhook dispatch for HITL and completion alerts."""
    try:
        if not allowed_hosts:
            return
        data = json.dumps(payload).encode("utf-8")
        await asyncio.to_thread(
            safe_http_request,
            url,
            method="POST",
            body=data,
            headers={"Content-Type": "application/json", "User-Agent": "Ivy-Agent-Harness/0.1.0"},
            timeout=5.0,
            max_bytes=4096,
            allowed_hosts=allowed_hosts,
        )
    except (OSError, TimeoutError, ValueError, OutboundRequestError) as exc:
        logger.warning("Completion webhook delivery failed (%s)", type(exc).__name__)


async def _stream_events(harness: AgentHarness, prompt: str, config: RunConfig):
    """Advance the blocking SDK iterator off the event loop, one event at a time."""
    iterator = iter(harness.stream(prompt, config=config))
    sentinel = object()
    while True:
        event = await asyncio.to_thread(next, iterator, sentinel)
        if event is sentinel:
            return
        yield event


def create_app(config_path: str | Path = "config.yaml") -> FastAPI:
    config = load_config(config_path)
    load_secrets(config)
    store = build_store(config)
    runner = GraphRunner(store=store, config=config)
    harness = AgentHarness(config=config)
    configured_auth = has_auth_credentials(config)
    key_records = deep_get(config, "auth.api_keys", {}) or {}
    if isinstance(key_records, dict):
        key_records = list(key_records.values())
    for record in key_records if isinstance(key_records, list) else []:
        env_name = record.get("key_env") if isinstance(record, dict) else None
        if env_name and not os.environ.get(str(env_name)):
            raise RuntimeError(f"API key environment variable {env_name!r} is not configured")
    bind_host = os.environ.get("AGENT_HARNESS_BIND_HOST") or str(deep_get(config, "api.host", "127.0.0.1"))
    try:
        is_loopback = ipaddress.ip_address(bind_host).is_loopback
    except ValueError:
        is_loopback = bind_host.lower() == "localhost"
    if not is_loopback and not configured_auth:
        raise RuntimeError("Refusing non-loopback API bind without configured API credentials")

    governance_config = deep_get(config, "governance.rate_limiting", {}) or {}
    max_rpm = int(governance_config.get("max_requests_per_minute", 60))
    max_concurrent = int(governance_config.get("max_concurrent_runs", 10))
    limiters: dict[str, RateLimiter] = {}
    limiter_lock = asyncio.Lock()

    async def require_api_key(
        connection: HTTPConnection,
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        token = x_api_key
        if not token and authorization and authorization.startswith("Bearer "):
            token = authorization[7:].strip()
        is_signed_webhook = connection.url.path.startswith("/api/v1/webhooks/")
        principal = authenticate_api_key(config, token)
        if principal is None and configured_auth:
            if not is_signed_webhook:
                raise HTTPException(status_code=401, detail="invalid or missing API key")
            principal = TenantContext(tenant_id="integrations", user_id="webhook", roles=["webhook"])
        if principal is None:
            peer_host = connection.client.host if connection.client else ""
            peer_is_loopback = _is_loopback_peer(peer_host)
            if not peer_is_loopback and not is_signed_webhook:
                raise HTTPException(status_code=401, detail="API credentials are required for remote clients")
            principal = TenantContext(tenant_id="local", user_id="local", roles=["admin"])
        principal.auto_approve_tools = bool(deep_get(config, "chat.agent_loop.auto_approve_tools", False))
        connection.state.principal = principal
        limiter_key = f"{principal.tenant_id}:{principal.user_id}"
        async with limiter_lock:
            limiter = limiters.setdefault(limiter_key, RateLimiter(max_rpm, max_concurrent))
        try:
            limiter.acquire()
        except RateLimitExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        context_token = set_current_tenant(principal)
        try:
            yield
        finally:
            reset_current_tenant(context_token)
            limiter.release()

    app = FastAPI(
        title="Ivy Agent Harness API",
        version="0.1.0",
        description="High-performance network API suite: REST, SSE streaming, Bidirectional WebSockets, and HITL webhooks.",
        dependencies=[Depends(require_api_key)],
    )
    app.state.config = config

    # Enable compliant CORS for web clients
    cors_origins = list(deep_get(config, "api.cors_origins", ["*"]))
    allow_creds = cors_origins != ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=allow_creds,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.runner = runner
    app.state.store = store
    app.state.harness = harness

    def get_owned_run(run_id: str, principal: TenantContext) -> RunSnapshot:
        try:
            snapshot = store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        is_tenant_admin = "admin" in principal.roles
        if snapshot.tenant_id != principal.tenant_id or (
            snapshot.user_id != principal.user_id and not is_tenant_admin
        ):
            raise HTTPException(status_code=404, detail="run not found")
        return snapshot

    # Mount OpenAI-Compatible /v1 router
    from harness.api.openai import openai_router
    app.include_router(openai_router)

    # ==================== Health & Catalog ====================

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/graphs")
    async def graphs() -> dict[str, list[str]]:
        return {"graphs": list_graphs()}

    @app.get("/models/routes")
    async def model_routes() -> dict:
        return {"routes": runner.models.routes_metadata()}

    @app.get("/tools")
    async def tools() -> dict:
        return {"tools": runner.tools.catalog()}

    @app.post("/tools/{tool_name:path}", response_model=ToolInvokeResponse)
    async def invoke_tool(tool_name: str, request: ToolInvokeRequest) -> ToolInvokeResponse:
        try:
            result = await asyncio.to_thread(runner.tools.invoke, tool_name, **request.arguments)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ToolInvokeResponse(tool=tool_name, result=result)

    @app.post("/models/smoke", response_model=ModelSmokeResponse)
    async def model_smoke(request: ModelSmokeRequest) -> ModelSmokeResponse:
        if not runner.models.has_route(request.graph, request.node):
            raise HTTPException(
                status_code=404,
                detail=f"model route not configured: {request.graph}.{request.node}",
            )
        try:
                text = await asyncio.to_thread(
                    runner.models.complete,
                request.graph,
                request.node,
                [{"role": "user", "content": request.prompt}],
            )
        except ModelConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"model provider call failed: {type(exc).__name__}: {exc}",
            ) from exc
        return ModelSmokeResponse(
            graph=request.graph,
            node=request.node,
            response_excerpt=text[:1000],
        )

    # ==================== Tier 3: Server-Sent Events (SSE) ====================

    @app.post("/api/v1/chat/stream")
    async def chat_stream(request: ChatStreamRequest) -> StreamingResponse:
        """Stream autonomous agent events, tokens, and tool results via SSE (text/event-stream)."""
        run_cfg = RunConfig(
            route=request.route,
            system_prompt=request.system_prompt,
            max_tool_steps=request.max_tool_steps,
            auto_approve_tools=bool(deep_get(config, "chat.agent_loop.auto_approve_tools", False)),
        )

        async def event_generator():
            async for event in _stream_events(harness, request.message, run_cfg):
                if event.type == "token":
                    yield f"event: token\ndata: {json.dumps({'delta': event.delta})}\n\n"
                elif event.type == "tool_start":
                    yield f"event: tool_start\ndata: {json.dumps({'tool': event.tool_name, 'args': event.args, 'step': event.step})}\n\n"
                elif event.type == "tool_result":
                    yield f"event: tool_result\ndata: {json.dumps({'tool': event.tool_name, 'summary': event.summary, 'duration_s': event.duration_s, 'step': event.step})}\n\n"
                elif event.type == "subagent_completed":
                    yield f"event: subagent_completed\ndata: {json.dumps({'agent': event.agent, 'observation': event.observation})}\n\n"
                elif event.type == "turn_completed":
                    yield f"event: turn_completed\ndata: {json.dumps({'final_output': event.final_output, 'steps': event.step, 'duration_s': event.duration_s})}\n\n"
                elif event.type == "error":
                    yield f"event: error\ndata: {json.dumps({'error': event.error})}\n\n"
            yield "event: done\ndata: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # ==================== Tier 3: Autonomous Agent REST & Swarms ====================

    @app.post("/api/v1/agent/run")
    async def run_agent(request: AgentRunRequest, background_tasks: BackgroundTasks) -> Any:
        """Execute autonomous agent task with optional streaming or synchronous structured result."""
        run_cfg = RunConfig(
            route=request.route,
            max_tool_steps=request.max_tool_steps,
            auto_approve_tools=bool(deep_get(config, "chat.agent_loop.auto_approve_tools", False)),
        )

        if request.stream:
            async def event_generator():
                async for event in _stream_events(harness, request.task, run_cfg):
                    if event.type == "token":
                        yield f"event: token\ndata: {json.dumps({'delta': event.delta})}\n\n"
                    elif event.type == "tool_start":
                        yield f"event: tool_start\ndata: {json.dumps({'tool': event.tool_name, 'args': event.args})}\n\n"
                    elif event.type == "tool_result":
                        yield f"event: tool_result\ndata: {json.dumps({'tool': event.tool_name, 'summary': event.summary, 'duration_s': event.duration_s})}\n\n"
                    elif event.type == "turn_completed":
                        yield f"event: turn_completed\ndata: {json.dumps({'final_output': event.final_output, 'steps': event.step, 'duration_s': event.duration_s})}\n\n"
                yield "event: done\ndata: [DONE]\n\n"

            return StreamingResponse(event_generator(), media_type="text/event-stream")

        result: AgentResult = await asyncio.to_thread(harness.run, request.task, run_cfg)

        if request.webhook_url:
            allowed_hosts = list(deep_get(config, "api.callback_hosts", []) or [])
            try:
                validate_outbound_url(request.webhook_url, allowed_hosts=allowed_hosts)
            except OutboundRequestError as exc:
                raise HTTPException(status_code=403, detail="webhook destination is not allowed") from exc
            background_tasks.add_task(
                _dispatch_webhook,
                request.webhook_url,
                {"event": "agent_completed", "task": request.task, "result": result.to_dict()},
                allowed_hosts,
            )

        return AgentRunResponse(
            task=request.task,
            success=result.success,
            final_output=result.final_output,
            steps=result.steps,
            duration_s=result.duration_s,
            tools_executed=result.tool_calls,
        )

    @app.post("/api/v1/agent/swarm")
    async def run_swarm(request: SwarmRunRequest) -> dict[str, Any]:
        """Execute multi-agent parallel scatter-gather or sequential handoff swarm."""
        return await asyncio.to_thread(
            harness.swarm,
            task=request.task,
            agents=request.agents,
            pattern=request.pattern,
        )

    # ==================== Tier 4: Event-Driven Webhooks (GitHub, Slack, Jira) ====================

    @app.post("/api/v1/webhooks/github")
    async def github_webhook(
        request: Request,
        payload: dict[str, Any],
        background_tasks: BackgroundTasks,
        x_github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
        x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
    ) -> dict[str, Any]:
        """Handle incoming GitHub webhook events (PR review, issue triage)."""
        from harness.workers.github import on_issue_opened, on_pull_request_opened

        gh_secret = os.environ.get("GITHUB_WEBHOOK_SECRET") or deep_get(config, "webhooks.github_secret")
        if not gh_secret or not x_hub_signature_256:
            raise HTTPException(status_code=401, detail="webhook authentication is not configured")
        if gh_secret and x_hub_signature_256:
            import hashlib
            body_bytes = await request.body()
            expected = "sha256=" + hmac.new(gh_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, x_hub_signature_256):
                raise HTTPException(status_code=401, detail="invalid GitHub webhook signature")

        event_name = x_github_event or payload.get("action", "")
        repo_name = payload.get("repository", {}).get("full_name", "")

        if event_name in {"pull_request", "pull_request_target"}:
            action = payload.get("action")
            if action in {"opened", "synchronize", "reopened"}:
                pr = payload.get("pull_request", {})
                pr_number = int(pr.get("number", 0))
                pr_diff = pr.get("diff_url", "")
                pr_title = pr.get("title", "")
                background_tasks.add_task(
                    on_pull_request_opened,
                    pr_diff=pr_diff or f"PR Title: {pr_title}",
                    pr_number=pr_number,
                    repo=repo_name,
                    title=pr_title,
                )
                return {"status": "enqueued", "event": "pull_request", "pr_number": pr_number}

        if event_name in {"issues", "issue"}:
            action = payload.get("action")
            if action in {"opened", "reopened"}:
                issue = payload.get("issue", {})
                issue_num = int(issue.get("number", 0))
                issue_title = issue.get("title", "")
                issue_body = issue.get("body", "")
                background_tasks.add_task(
                    on_issue_opened,
                    issue_number=issue_num,
                    issue_title=issue_title,
                    issue_body=issue_body,
                    repo=repo_name,
                )
                return {"status": "enqueued", "event": "issues", "issue_number": issue_num}

        return {"status": "ignored", "event": event_name}

    @app.post("/api/v1/webhooks/slack")
    async def slack_webhook(
        request: Request,
        payload: dict[str, Any],
        background_tasks: BackgroundTasks,
        x_slack_signature: Annotated[str | None, Header(alias="X-Slack-Signature")] = None,
        x_slack_request_timestamp: Annotated[str | None, Header(alias="X-Slack-Request-Timestamp")] = None,
    ) -> dict[str, Any]:
        """Handle incoming Slack webhook events and URL verification challenge."""
        from harness.workers.slack import on_slack_mention

        slack_secret = os.environ.get("SLACK_SIGNING_SECRET") or deep_get(config, "webhooks.slack_signing_secret")
        if not slack_secret or not x_slack_signature or not x_slack_request_timestamp:
            raise HTTPException(status_code=401, detail="webhook authentication is not configured")
        if slack_secret and x_slack_signature and x_slack_request_timestamp:
            import hashlib
            try:
                ts_val = float(x_slack_request_timestamp)
                if abs(time.time() - ts_val) > 300:
                    raise HTTPException(status_code=401, detail="Slack request timestamp expired")
            except ValueError:
                raise HTTPException(status_code=401, detail="Invalid timestamp header")
            body_bytes = await request.body()
            sig_base = f"v0:{x_slack_request_timestamp}:{body_bytes.decode('utf-8')}".encode()
            expected = "v0=" + hmac.new(slack_secret.encode("utf-8"), sig_base, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, x_slack_signature):
                raise HTTPException(status_code=401, detail="invalid Slack webhook signature")

        if "challenge" in payload:
            return {"challenge": payload["challenge"]}

        event = payload.get("event", {})
        event_type = event.get("type")
        if event_type == "app_mention":
            text = event.get("text", "")
            user = event.get("user", "")
            channel = event.get("channel", "")
            thread_ts = event.get("thread_ts") or event.get("ts")
            background_tasks.add_task(
                on_slack_mention,
                text=text,
                user=user,
                channel=channel,
                thread_ts=thread_ts,
            )
            return {"status": "enqueued", "event": "app_mention"}

        return {"status": "ignored", "event": event_type}

    @app.post("/api/v1/webhooks/jira")
    async def jira_webhook(
        payload: dict[str, Any],
        background_tasks: BackgroundTasks,
        x_webhook_token: Annotated[str | None, Header(alias="X-Webhook-Token")] = None,
    ) -> dict[str, Any]:
        """Handle incoming Atlassian Jira issue created webhooks."""
        jira_secret = os.environ.get("JIRA_WEBHOOK_SECRET") or deep_get(config, "webhooks.jira_secret")
        if not jira_secret or not x_webhook_token or not hmac.compare_digest(x_webhook_token, jira_secret):
            raise HTTPException(status_code=401, detail="webhook authentication is not configured")
        from harness.workers.jira import on_jira_issue_created

        webhook_event = payload.get("webhookEvent", "")
        issue = payload.get("issue", {})
        issue_key = issue.get("key", "")
        fields = issue.get("fields", {})
        summary = fields.get("summary", "")
        description = str(fields.get("description", ""))

        if issue_key:
            background_tasks.add_task(
                on_jira_issue_created,
                issue_key=issue_key,
                summary=summary,
                description=description,
            )
            return {"status": "enqueued", "issue_key": issue_key}

        return {"status": "ignored", "event": webhook_event}

    # ==================== Tier 3: Bidirectional WebSockets ====================

    @app.websocket("/ws/chat")
    async def websocket_chat(websocket: WebSocket) -> None:
        """Bidirectional WebSocket for real-time tokens, tool card streaming, and subagent telemetry."""
        client_key = websocket.headers.get("x-api-key")
        auth_header = websocket.headers.get("authorization", "")
        if not client_key and auth_header.startswith("Bearer "):
            client_key = auth_header[7:].strip()
        principal = authenticate_api_key(config, client_key)
        if principal is None and configured_auth:
            await websocket.close(code=1008, reason="Unauthorized: invalid or missing API key")
            return
        if principal is None:
            peer_host = websocket.client.host if websocket.client else ""
            if not _is_loopback_peer(peer_host):
                await websocket.close(code=1008, reason="API credentials are required for remote clients")
                return
            principal = TenantContext(tenant_id="local", user_id="local", roles=["admin"])
        principal.auto_approve_tools = bool(deep_get(config, "chat.agent_loop.auto_approve_tools", False))

        await websocket.accept()
        context_token = set_current_tenant(principal)
        try:
            while True:
                raw_msg = await websocket.receive_text()
                if len(raw_msg.encode("utf-8")) > 1_000_000:
                    await websocket.close(code=1009, reason="Message too large")
                    return
                data = json.loads(raw_msg)
                msg_type = data.get("type", "chat")

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong", "time": time.time()})
                    continue

                if msg_type == "chat":
                    prompt = str(data.get("message", ""))
                    route = data.get("route")
                    max_steps = int(data.get("max_tool_steps", 10))
                    run_cfg = RunConfig(
                        route=route,
                        max_tool_steps=max_steps,
                        auto_approve_tools=bool(deep_get(config, "chat.agent_loop.auto_approve_tools", False)),
                    )
                    async for event in _stream_events(harness, prompt, run_cfg):
                        if event.type == "token":
                            await websocket.send_json({"type": "token", "delta": event.delta})
                        elif event.type == "tool_start":
                            await websocket.send_json({
                                "type": "tool_start",
                                "tool": event.tool_name,
                                "args": event.args,
                                "step": event.step,
                            })
                        elif event.type == "tool_result":
                            await websocket.send_json({
                                "type": "tool_result",
                                "tool": event.tool_name,
                                "summary": event.summary,
                                "duration_s": event.duration_s,
                                "step": event.step,
                            })
                        elif event.type == "subagent_completed":
                            await websocket.send_json({
                                "type": "subagent_completed",
                                "agent": event.agent,
                                "observation": event.observation,
                            })
                        elif event.type == "turn_completed":
                            await websocket.send_json({
                                "type": "turn_completed",
                                "final_output": event.final_output,
                                "steps": event.step,
                                "duration_s": event.duration_s,
                            })
                        elif event.type == "error":
                            await websocket.send_json({"type": "error", "error": event.error})

                elif msg_type == "swarm":
                    task = str(data.get("task", ""))
                    agents = data.get("agents", "analyst,reviewer")
                    pattern = data.get("pattern", "fanout")
                    await websocket.send_json({"type": "swarm_started", "task": task, "agents": agents, "pattern": pattern})
                    swarm_res = harness.swarm(task=task, agents=agents, pattern=pattern)
                    await websocket.send_json({"type": "swarm_completed", "result": swarm_res})

        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - websocket errors must not escape the ASGI handler
            logger.error("WebSocket handler failed (%s)", type(exc).__name__)
            try:
                await websocket.send_json({"type": "error", "error": "internal server error"})
            except (RuntimeError, WebSocketDisconnect):
                logger.debug("WebSocket closed before error response could be sent")
        finally:
            reset_current_tenant(context_token)

    # ==================== Graph Workflows & Durable Checkpoints ====================

    @app.post("/runs", response_model=RunSnapshot)
    async def create_run(
        body: RunCreateRequest,
        background_tasks: BackgroundTasks,
        http_request: Request,
    ) -> RunSnapshot:
        principal: TenantContext = http_request.state.principal
        try:
            snapshot = runner.create_run(
                body.graph,
                body.input,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                roles=principal.roles,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="graph not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if body.wait:
            return await runner.continue_run(snapshot.run_id)
        background_tasks.add_task(_run_background, runner, snapshot.run_id)
        return snapshot

    @app.get("/runs/{run_id}", response_model=RunSnapshot)
    async def get_run(run_id: str, request: Request) -> RunSnapshot:
        return get_owned_run(run_id, request.state.principal)

    @app.delete("/runs/{run_id}", status_code=204)
    async def delete_run(run_id: str, request: Request) -> None:
        get_owned_run(run_id, request.state.principal)
        store.delete_run(run_id)

    @app.get("/runs/{run_id}/stream")
    async def stream_run(run_id: str, request: Request) -> StreamingResponse:
        get_owned_run(run_id, request.state.principal)

        async def event_generator():
            queue = global_event_bus.create_stream()
            last_seq = 0
            last_event_type: str | None = None
            try:
                while True:
                    # The queue is a wake-up mechanism; the persistent trace is
                    # the source of truth so terminal runs and missed queue events
                    # cannot leave a subscriber waiting forever.
                    for trace in store.list_trace(run_id, after_seq=last_seq):
                        last_seq = trace.seq
                        last_event_type = trace.event_type
                        event_data = {
                            "id": f"{run_id}:{trace.seq}",
                            "type": trace.event_type,
                            "run_id": trace.run_id,
                            "timestamp": trace.timestamp,
                            "node": trace.node,
                            "payload": trace.payload,
                        }
                        yield (
                            f"event: {trace.event_type}\n"
                            f"data: {json.dumps(event_data, default=str)}\n\n"
                        )
                    current = store.get_run(run_id)
                    if current.status in {"done", "failed"}:
                        if last_event_type not in {
                            "run_completed",
                            "run_failed",
                        }:
                            terminal_type = "run_completed" if current.status == "done" else "run_failed"
                            terminal_data = {
                                "id": f"{run_id}:terminal",
                                "type": terminal_type,
                                "run_id": run_id,
                                "timestamp": current.completed_at or current.updated_at,
                                "node": current.current_node,
                                "payload": {"status": current.status, "error": current.error},
                            }
                            yield (
                                f"event: {terminal_type}\n"
                                f"data: {json.dumps(terminal_data, default=str)}\n\n"
                            )
                        break
                    try:
                        await asyncio.wait_for(queue.get(), timeout=20.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                global_event_bus.close_stream(queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/runs/{run_id}/trace")
    async def get_trace(run_id: str, request: Request) -> dict:
        get_owned_run(run_id, request.state.principal)
        return {"run_id": run_id, "events": [event.model_dump(mode="json") for event in store.list_trace(run_id)]}

    @app.get("/runs/{run_id}/result")
    async def get_result(run_id: str, request: Request) -> dict:
        snapshot = get_owned_run(run_id, request.state.principal)
        if snapshot.result is None:
            raise HTTPException(status_code=404, detail="run has no result yet")
        return snapshot.result

    @app.post("/runs/{run_id}/resume", response_model=RunSnapshot)
    async def resume_run(
        run_id: str,
        body: ResumeRequest,
        background_tasks: BackgroundTasks,
        http_request: Request,
    ) -> RunSnapshot:
        get_owned_run(run_id, http_request.state.principal)
        snapshot = await runner.resume_run(run_id, grant_id=body.grant_id)
        if body.wait and snapshot.status not in {"done", "failed"}:
            return await runner.continue_run(run_id)
        if not body.wait and snapshot.status in {"queued", "running"}:
            background_tasks.add_task(_run_background, runner, run_id)
        return snapshot

    @app.post("/runs/{run_id}/approval")
    async def approve_run(run_id: str, body: ApprovalRequest, request: Request) -> dict[str, str]:
        principal: TenantContext = request.state.principal
        try:
            run = store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        if run.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=403, detail="approver and run must belong to the same tenant")
        try:
            return runner.issue_approval(run_id, principal.tenant_id, principal.user_id, principal.roles)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/runs/{run_id}/checkpoints")
    async def create_checkpoint(run_id: str, request: Request, label: str = "") -> dict:
        get_owned_run(run_id, request.state.principal)
        try:
            chk = store.create_checkpoint(run_id, label=label)
            return chk.to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/runs/{run_id}/checkpoints")
    async def list_checkpoints(run_id: str, request: Request) -> dict:
        get_owned_run(run_id, request.state.principal)
        try:
            chks = store.list_checkpoints(run_id)
            return {"checkpoints": [c.to_dict() for c in chks]}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/runs/{run_id}/checkpoints/{checkpoint_id}/rewind", response_model=RunSnapshot)
    async def rewind_checkpoint(run_id: str, checkpoint_id: str, request: Request) -> RunSnapshot:
        get_owned_run(run_id, request.state.principal)
        try:
            return store.rewind_to_checkpoint(run_id, checkpoint_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


def _run_background(runner: GraphRunner, run_id: str) -> None:
    snapshot = runner.store.get_run(run_id)
    principal = TenantContext(
        tenant_id=snapshot.tenant_id,
        user_id=snapshot.user_id,
        roles=snapshot.roles,
        auto_approve_tools=bool(deep_get(runner.config, "chat.agent_loop.auto_approve_tools", False)),
    )
    context_token = set_current_tenant(principal)
    try:
        asyncio.run(runner.continue_run(run_id))
    finally:
        reset_current_tenant(context_token)


app = create_app(Path(os.environ.get("AGENT_HARNESS_CONFIG", "config.yaml")))
