"""Model Context Protocol (MCP) Server for IDE and Client App Integration.

Exposes Ivy Agent Harness capabilities to Cursor, Claude Desktop, VS Code, and Windsurf:
- MCP Tools: `ivy_agent_turn`, `ivy_swarm`, `ivy_run_graph`, `ivy_tool_catalog`, `ivy_search_code`, `ivy_run_sandbox_python`
- MCP Prompts: `ivy_repository_audit`, `ivy_bug_investigation`, `ivy_feature_implementation`
- MCP Resources: `ivy://workspace/status`, `ivy://models/routes`, `ivy://runs/recent`
"""

from __future__ import annotations

import json
import sys
from typing import Any

from harness.sdk import AgentHarness, RunConfig

_harness_instance: AgentHarness | None = None


def get_harness() -> AgentHarness:
    global _harness_instance
    if _harness_instance is None:
        _harness_instance = AgentHarness.from_project(workspace_root=".")
    return _harness_instance


def handle_initialize(req_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": False},
                "prompts": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": {"name": "ivy-agent-harness", "version": "0.1.0"},
        },
    }


def handle_tools_list(req_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "tools": [
                {
                    "name": "ivy_agent_turn",
                    "description": "Execute an autonomous multi-turn agent task with full tool execution, code inspection, and test verification loop",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "description": "The user intent or instruction for the autonomous agent",
                            },
                            "route": {
                                "type": "string",
                                "description": "Optional model route (e.g. 'nim.nemotron', 'nim.llama-70b')",
                            },
                            "max_tool_steps": {
                                "type": "integer",
                                "description": "Maximum tool turns permitted (default: 10)",
                                "default": 10,
                            },
                        },
                        "required": ["prompt"],
                    },
                },
                {
                    "name": "ivy_swarm",
                    "description": "Coordinate a multi-subagent parallel fan-out or sequential handoff swarm for deep repository tasks",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "The overarching swarm objective",
                            },
                            "agents": {
                                "type": "string",
                                "description": "Comma-separated subagent personas (e.g. 'code_auditor,security_sentinel,test_runner')",
                                "default": "code_auditor,security_sentinel,test_runner",
                            },
                            "pattern": {
                                "type": "string",
                                "enum": ["fanout", "handoff"],
                                "default": "fanout",
                            },
                        },
                        "required": ["task"],
                    },
                },
                {
                    "name": "ivy_run_graph",
                    "description": "Execute a declarative LangGraph-compatible state machine workflow (e.g. 'code-investigate', 'research-report', 'brainstorm')",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "graph": {
                                "type": "string",
                                "enum": ["code-investigate", "research-report", "brainstorm"],
                                "description": "Graph workflow identifier",
                            },
                            "input_data": {
                                "type": "object",
                                "description": "Initial state dictionary for the graph runner",
                            },
                        },
                        "required": ["graph", "input_data"],
                    },
                },
                {
                    "name": "ivy_tool_catalog",
                    "description": "List all active builtin capability packs, filesystem, sandbox, and data tools available in the harness",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                    },
                },
                {
                    "name": "ivy_search_code",
                    "description": "Search codebase files for pattern or text with exact line citations",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Text or symbol to search for"},
                            "root": {"type": "string", "description": "Optional search root directory", "default": "."},
                        },
                        "required": ["query"],
                    },
                },
                {
                    "name": "ivy_run_sandbox_python",
                    "description": "Execute sandboxed Python code within the workspace environment",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "description": "Python source code string to execute"},
                        },
                        "required": ["code"],
                    },
                },
                {
                    "name": "sqlite_query",
                    "description": "Execute a SQL query on a SQLite database file",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "db_path": {"type": "string", "description": "Path to SQLite file"},
                            "query": {"type": "string", "description": "SQL query to execute"},
                        },
                        "required": ["db_path", "query"],
                    },
                },
                {
                    "name": "hash_text",
                    "description": "Compute cryptographic hash of a text string (sha256 or md5)",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "Text to hash"},
                            "algorithm": {"type": "string", "enum": ["sha256", "md5"], "default": "sha256"},
                        },
                        "required": ["text"],
                    },
                },
                {
                    "name": "generate_uuid",
                    "description": "Generate one or more UUID4 strings",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "count": {"type": "integer", "default": 1},
                        },
                    },
                },
            ]
        },
    }


def handle_prompts_list(req_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "prompts": [
                {
                    "name": "ivy_repository_audit",
                    "description": "Comprehensive repository security, AST architecture, and test suite audit",
                    "arguments": [
                        {"name": "focus_area", "description": "Optional area of focus (e.g. 'security', 'models')", "required": False}
                    ],
                },
                {
                    "name": "ivy_bug_investigation",
                    "description": "Locate root cause of a defect, write a minimal reproduction test, and verify fix",
                    "arguments": [
                        {"name": "issue_description", "description": "Description of the bug or unexpected behavior", "required": True}
                    ],
                },
                {
                    "name": "ivy_feature_implementation",
                    "description": "Plan, implement, and verify a new feature against the existing architecture",
                    "arguments": [
                        {"name": "specification", "description": "Feature specification or user story", "required": True}
                    ],
                },
            ]
        },
    }


def handle_prompts_get(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments", {})

    if name == "ivy_repository_audit":
        focus = args.get("focus_area", "all")
        content = (
            f"Run a comprehensive repository audit focusing on '{focus}'. "
            "Delegate in parallel to specialized subagents ('code_auditor', 'security_sentinel', 'test_runner'), "
            "inspect ASTs, run tests, and synthesize findings."
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "description": "Repository audit prompt",
                "messages": [{"role": "user", "content": {"type": "text", "text": content}}],
            },
        }

    if name == "ivy_bug_investigation":
        issue = args.get("issue_description", "")
        content = (
            f"Diagnose and fix the following issue:\n{issue}\n\n"
            "Steps:\n1. Search for relevant symbols.\n2. Inspect target source files.\n"
            "3. Write a reproduction test.\n4. Apply fix and verify with pytest."
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "description": "Bug investigation prompt",
                "messages": [{"role": "user", "content": {"type": "text", "text": content}}],
            },
        }

    if name == "ivy_feature_implementation":
        spec = args.get("specification", "")
        content = f"Implement the following feature specification:\n{spec}\n\nEnsure 100% test coverage."
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "description": "Feature implementation prompt",
                "messages": [{"role": "user", "content": {"type": "text", "text": content}}],
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Prompt not found: {name}"},
    }


def handle_resources_list(req_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "resources": [
                {
                    "uri": "ivy://workspace/status",
                    "name": "Workspace Status",
                    "description": "Active workspace root, detected domain profile, and tool summary",
                    "mimeType": "application/json",
                },
                {
                    "uri": "ivy://models/routes",
                    "name": "Model Routes",
                    "description": "Configured LLM routes, providers, and failover chains",
                    "mimeType": "application/json",
                },
            ]
        },
    }


def handle_resources_read(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    uri = params.get("uri", "")
    harness = get_harness()

    if uri == "ivy://workspace/status":
        data = {
            "workspace_root": str(harness.workspace_root),
            "project_name": harness.profile.project_name,
            "primary_domain": harness.profile.primary_domain,
            "registered_tools_count": len(harness.list_tools()),
            "skills_count": len(harness.skills.list_skills()),
        }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(data, indent=2)}]},
        }

    if uri == "ivy://models/routes":
        routes = {}
        for r_name, r_obj in harness.models.routes.items():
            routes[r_name] = {
                "provider": r_obj.provider,
                "model": r_obj.model,
                "max_tokens": r_obj.max_tokens,
                "fallbacks": getattr(r_obj, "fallbacks", []),
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(routes, indent=2)}]},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32002, "message": f"Resource not found: {uri}"},
    }


def handle_tool_call(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments", {})
    harness = get_harness()

    try:
        if name == "ivy_agent_turn":
            prompt = str(args.get("prompt", ""))
            route = args.get("route")
            max_steps = int(args.get("max_tool_steps", 10))
            config = RunConfig(route=route, max_tool_steps=max_steps) if route else RunConfig(max_tool_steps=max_steps)
            result = harness.run(prompt, config=config)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result.to_dict(), indent=2),
                        }
                    ]
                },
            }

        if name == "ivy_swarm":
            task = str(args.get("task", ""))
            agents = args.get("agents", "code_auditor,security_sentinel,test_runner")
            pattern = str(args.get("pattern", "fanout"))
            swarm_res = harness.swarm(task=task, agents=agents, pattern=pattern)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(swarm_res, indent=2, default=str)}]},
            }

        if name == "ivy_run_graph":
            graph = str(args.get("graph", "code-investigate"))
            input_data = args.get("input_data", {})
            snap = harness.workflow(graph_name=graph, input_data=input_data)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(snap.state, indent=2, default=str)}]},
            }

        if name == "ivy_tool_catalog":
            catalog = harness.tools.catalog()
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(catalog, indent=2)}]},
            }

        if name == "ivy_search_code":
            query = str(args.get("query", ""))
            root = args.get("root", ".")
            search_res = harness.tool("file.search_text", query=query, root=root)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(search_res, indent=2)}]},
            }

        if name == "ivy_run_sandbox_python":
            code = str(args.get("code", ""))
            py_res = harness.tool("sandbox.run_python", code=code)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(py_res, indent=2)}]},
            }

        if name == "hash_text":
            import hashlib
            text = str(args.get("text", ""))
            algo = str(args.get("algorithm", "sha256")).lower()
            if algo == "md5":
                digest = hashlib.md5(text.encode("utf-8")).hexdigest()
            else:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": f"{algo}:{digest}"}]},
            }

        if name == "generate_uuid":
            import uuid
            count = int(args.get("count", 1))
            uuids = [str(uuid.uuid4()) for _ in range(max(1, min(count, 50)))]
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": "\n".join(uuids)}]},
            }

        if name == "sqlite_query":
            db_path = str(args.get("db_path", ".harness/runs.sqlite3"))
            sql = str(args.get("query", ""))
            try:
                res = harness.tools.invoke("data.sql_query", query=sql, db_path=db_path)
                rows = res.get("rows", [])
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(rows, indent=2)}]},
                }
            except Exception as exc:  # noqa: BLE001 - return tool failures as JSON-RPC tool errors
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"isError": True, "content": [{"type": "text", "text": f"SQLite Error: {exc}"}]},
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {name}"},
        }

    except Exception as exc:  # noqa: BLE001 - isolate malformed or failed MCP tool requests
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"isError": True, "content": [{"type": "text", "text": f"Execution Error: {type(exc).__name__}: {exc}"}]},
        }


def main() -> None:
    """Run stdio JSON-RPC loop for Model Context Protocol."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})

            if method == "initialize":
                resp = handle_initialize(req_id)
            elif method == "notifications/initialized":
                continue
            elif method == "ping":
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            elif method == "tools/list":
                resp = handle_tools_list(req_id)
            elif method == "tools/call":
                resp = handle_tool_call(req_id, params)
            elif method == "prompts/list":
                resp = handle_prompts_list(req_id)
            elif method == "prompts/get":
                resp = handle_prompts_get(req_id, params)
            elif method == "resources/list":
                resp = handle_resources_list(req_id)
            elif method == "resources/read":
                resp = handle_resources_read(req_id, params)
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unsupported method: {method}"},
                }

            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
