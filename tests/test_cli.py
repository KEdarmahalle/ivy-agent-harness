from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import cli
from harness.secrets import list_secret_names


class CliTests(unittest.TestCase):
    def test_no_command_starts_chat(self) -> None:
        with (
            patch("harness.cli.load_secrets"),
            patch("harness.cli.ModelRegistry") as registry,
            patch("builtins.input", side_effect=["/exit"]),
            patch("sys.stdout", io.StringIO()) as stdout,
        ):
            exit_code = cli.main([])
        self.assertEqual(exit_code, 0)
        registry.from_config.return_value.route_for.return_value.model = "test-model"
        self.assertIn("Ivy Agent Harness", stdout.getvalue())

    def test_chat_once_uses_configured_route_without_api_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                json.dumps(
                    {
                        "chat": {"graph": "brainstorm", "node": "generate"},
                        "models": {
                            "default": {"provider": "fallback", "model": "deterministic-local"},
                            "brainstorm.generate": {
                                "provider": "fallback",
                                "model": "deterministic-brainstormer",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with (
                patch("harness.cli.load_secrets"),
                patch("sys.stdout", stdout),
            ):
                exit_code = cli.main(["--config", str(config_path), "chat", "--once", "hello"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Clarify the audience", stdout.getvalue())

    def test_chat_includes_local_harness_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                json.dumps(
                    {
                        "chat": {"graph": "nim", "node": "example"},
                        "models": {
                            "default": {"provider": "fallback", "model": "deterministic-local"},
                            "nim.example": {"provider": "fallback", "model": "deterministic-test"},
                        },
                        "tools": {"builtins": {"enabled": ["file.search_text"]}},
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("harness.cli.load_secrets"),
                patch("harness.cli.ModelRegistry") as registry_type,
                patch("sys.stdout", io.StringIO()),
            ):
                registry = registry_type.from_config.return_value
                registry.route_for.return_value.model = "deterministic-test"
                registry.complete.return_value = "ok"
                exit_code = cli.main(["--config", str(config_path), "chat", "--once", "review"])
        self.assertEqual(exit_code, 0)
        messages = registry.complete.call_args.args[2]
        self.assertIn("Local harness context", messages[0]["content"])
        self.assertIn("Enabled builtin tools: file.search_text", messages[0]["content"])

    def test_chat_executes_model_requested_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                json.dumps(
                    {
                        "chat": {"graph": "nim", "node": "example", "max_tool_steps": 2},
                        "models": {
                            "default": {"provider": "fallback", "model": "deterministic-local"},
                            "nim.example": {"provider": "fallback", "model": "deterministic-test"},
                        },
                        "tools": {
                            "builtins": {"enabled": ["file.list"]},
                            "allowlist": ["file.list"],
                            "sandbox": {"workspace_roots": [directory]},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (Path(directory) / "example.txt").write_text("hello", encoding="utf-8")
            with (
                patch("harness.cli.load_secrets"),
                patch("harness.cli.ModelRegistry") as registry_type,
                patch("sys.stdout", io.StringIO()) as stdout,
            ):
                registry = registry_type.from_config.return_value
                registry.route_for.return_value.model = "deterministic-test"
                registry.complete.side_effect = [
                    '{"tool":"file.list","args":{"root":"' + directory.replace("\\", "\\\\") + '"}}',
                    "The project contains example.txt.",
                ]
                exit_code = cli.main(["--config", str(config_path), "chat", "--once", "list files"])
        self.assertEqual(exit_code, 0)
        self.assertIn("file.list", stdout.getvalue())
        self.assertIn("The project contains example.txt.", stdout.getvalue())
        followup_messages = registry.complete.call_args.args[2]
        self.assertIn("Tool result", followup_messages[-1]["content"])
        self.assertIn("example.txt", followup_messages[-1]["content"])

    def test_chat_summarizes_observations_when_model_keeps_calling_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                json.dumps(
                    {
                        "chat": {"graph": "nim", "node": "example", "max_tool_steps": 1},
                        "models": {
                            "default": {"provider": "fallback", "model": "deterministic-local"},
                            "nim.example": {"provider": "fallback", "model": "deterministic-test"},
                        },
                        "tools": {
                            "builtins": {"enabled": ["file.list"]},
                            "allowlist": ["file.list"],
                            "sandbox": {"workspace_roots": [directory]},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (Path(directory) / "harness").mkdir()
            with (
                patch("harness.cli.load_secrets"),
                patch("harness.cli.ModelRegistry") as registry_type,
                patch("sys.stdout", io.StringIO()) as stdout,
            ):
                registry = registry_type.from_config.return_value
                registry.route_for.return_value.model = "deterministic-test"
                registry.complete.return_value = (
                    '{"tool":"file.list","args":{"root":"' + directory.replace("\\", "\\\\") + '"}}'
                )
                exit_code = cli.main(["--config", str(config_path), "chat", "--once", "list files"])
        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("file.list", output)
        self.assertIn("I inspected the project with local tools", output)
        self.assertIn("harness/", output)

    def test_output_configuration_tolerates_unicode(self) -> None:
        class StdoutProbe(io.StringIO):
            def __init__(self) -> None:
                super().__init__()
                self.reconfigured_errors: str | None = None

            def reconfigure(self, *, errors: str) -> None:
                self.reconfigured_errors = errors

        probe = StdoutProbe()
        with patch("sys.stdout", probe):
            cli.configure_output()
        self.assertEqual(probe.reconfigured_errors, "replace")

    def test_secrets_set_and_list_use_configured_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "secrets.json"
            with patch("harness.cli.load_secrets"):
                exit_code = cli.main(
                    [
                        "--secret-store",
                        str(store),
                        "secrets",
                        "set",
                        "NVIDIA_NIM_API_KEY",
                        "--value",
                        "test-secret",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(list_secret_names(path=store), ["NVIDIA_NIM_API_KEY"])

    def test_models_command_calls_routes_endpoint(self) -> None:
        with patch("harness.cli.request", return_value={"routes": {}}) as request:
            exit_code = cli.main(["--base-url", "http://testserver", "--api-key", "secret", "models"])
        self.assertEqual(exit_code, 0)
        request.assert_called_once_with(
            "GET",
            "http://testserver",
            "/models/routes",
            api_key="secret",
        )

    def test_model_smoke_command_posts_prompt(self) -> None:
        with patch("harness.cli.request", return_value={"response_excerpt": "ok"}) as request:
            exit_code = cli.main(
                [
                    "--base-url",
                    "http://testserver",
                    "model-smoke",
                    "--graph",
                    "nim",
                    "--node",
                    "example",
                    "--prompt",
                    "Reply with ok.",
                ]
            )
        self.assertEqual(exit_code, 0)
        request.assert_called_once_with(
            "POST",
            "http://testserver",
            "/models/smoke",
            {"graph": "nim", "node": "example", "prompt": "Reply with ok."},
            api_key=None,
        )

    def test_tool_commands_call_tool_endpoints(self) -> None:
        with patch("harness.cli.request", return_value={"tools": {}}) as request:
            exit_code = cli.main(["--base-url", "http://testserver", "tools"])
        self.assertEqual(exit_code, 0)
        request.assert_called_once_with("GET", "http://testserver", "/tools", api_key=None)

        with patch("harness.cli.request", return_value={"result": {}}) as request:
            exit_code = cli.main(
                [
                    "--base-url",
                    "http://testserver",
                    "tool",
                    "sandbox.inspect",
                    "--args-json",
                    "{}",
                ]
            )
        self.assertEqual(exit_code, 0)
        request.assert_called_once_with(
            "POST",
            "http://testserver",
            "/tools/sandbox.inspect",
            {"arguments": {}},
            api_key=None,
        )

    def test_tool_command_accepts_key_value_args(self) -> None:
        with patch("harness.cli.request", return_value={"result": {}}) as request:
            exit_code = cli.main(
                [
                    "--base-url",
                    "http://testserver",
                    "tool",
                    "file.search_text",
                    "--arg",
                    "root=.",
                    "--arg",
                    "query=nvidia_nim",
                    "--arg",
                    "max_results=3",
                ]
            )
        self.assertEqual(exit_code, 0)
        request.assert_called_once_with(
            "POST",
            "http://testserver",
            "/tools/file.search_text",
            {"arguments": {"root": ".", "query": "nvidia_nim", "max_results": 3}},
            api_key=None,
        )

    def test_pyproject_registers_ivy_entry_points(self) -> None:
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        scripts = pyproject["project"]["scripts"]
        self.assertEqual(scripts["agent-harness"], "harness.cli:main")
        self.assertEqual(scripts["agent-harness-nim-smoke"], "harness.nim_smoke:main")
        self.assertEqual(scripts["ivy"], "harness.cli:main")
        self.assertEqual(scripts["ivy-nim-smoke"], "harness.nim_smoke:main")

    def test_serve_command_starts_uvicorn(self) -> None:
        with (
            patch("uvicorn.run") as run,
            patch.dict(os.environ, {"AGENT_HARNESS_API_KEY": "test-api-key"}),
        ):
            exit_code = cli.main(["serve", "--host", "0.0.0.0", "--port", "9000", "--reload"])
        self.assertEqual(exit_code, 0)
        run.assert_called_once_with(
            "harness.api.main:app",
            host="0.0.0.0",
            port=9000,
            reload=True,
        )

    def test_serve_accepts_configured_per_principal_api_keys(self) -> None:
        config = {"auth": {"api_keys": {"operator": {"key_env": "OPERATOR_API_KEY"}}}}
        with (
            patch("harness.cli.load_config", return_value=config),
            patch("harness.cli.load_secrets"),
            patch.dict(os.environ, {"OPERATOR_API_KEY": "operator-secret"}, clear=True),
            patch("uvicorn.run") as run,
        ):
            exit_code = cli.main(["serve", "--host", "0.0.0.0", "--port", "9001"])

        self.assertEqual(exit_code, 0)
        run.assert_called_once_with(
            "harness.api.main:app",
            host="0.0.0.0",
            port=9001,
            reload=False,
        )

    def test_powershell_wrappers_propagate_exit_codes(self) -> None:
        self.assertIn("exit $LASTEXITCODE", Path("ivy.ps1").read_text(encoding="utf-8"))
        self.assertIn("exit $LASTEXITCODE", Path("ivy-nim-smoke.ps1").read_text(encoding="utf-8"))

    def test_interactive_commands_and_help(self) -> None:
        with (
            patch("harness.cli.load_secrets"),
            patch("harness.cli.ModelRegistry") as registry_mock,
            patch("builtins.input", side_effect=["/help", "/tools", "/models", "/context", "/config", "/exit"]),
            patch("sys.stdout", io.StringIO()) as stdout,
        ):
            reg = registry_mock.from_config.return_value
            reg.route_for.return_value.model = "test-model"
            reg.route_for.return_value.provider = "test-provider"
            reg.routes_metadata.return_value = {"test": {"model": "m", "provider": "p", "has_api_key": False}}
            exit_code = cli.main([])
        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("Command Reference", output)
        self.assertIn("Registered Tools", output)
        self.assertIn("Configured Model Routes", output)
        self.assertIn("Local Harness Context", output)

    def test_interactive_tool_approval_declined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                json.dumps(
                    {
                        "chat": {"graph": "nim", "node": "example", "max_tool_steps": 2},
                        "models": {
                            "default": {"provider": "fallback", "model": "deterministic-local"},
                            "nim.example": {"provider": "fallback", "model": "deterministic-test"},
                        },
                        "tools": {
                            "builtins": {
                                "enabled": ["file.write_text"],
                                "definitions": {"file.write_text": {"requires_approval": True}},
                            },
                            "allowlist": ["file.write_text"],
                            "sandbox": {"workspace_roots": [directory], "writable_roots": [directory]},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("harness.cli.load_secrets"),
                patch("harness.cli.ModelRegistry") as registry_type,
                patch("builtins.input", side_effect=["n"]),  # User declines
                patch("sys.stdout", io.StringIO()),
            ):
                registry = registry_type.from_config.return_value
                registry.route_for.return_value.model = "deterministic-test"
                registry.complete.side_effect = [
                    '{"tool":"file.write_text","args":{"file":"test.txt","content":"hello"}}',
                    "Understood, I will not write the file.",
                ]
                exit_code = cli.main(["--config", str(config_path), "chat", "--once", "create a file"])
        self.assertEqual(exit_code, 0)

    def test_parse_tool_calls_parallel_and_windows_paths(self) -> None:
        raw = (
            '{"tool": "file.read_text", "args": {"file": "tests/test_api.py", "root": "C:\\agent_harness"}},\n'
            '{"tool": "file.read_text", "args": {"file": "tests/test_cli.py", "root": "C:\\agent_harness"}}'
        )
        calls = cli.parse_tool_calls(raw)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["tool"], "file.read_text")
        self.assertEqual(calls[0]["args"]["file"], "tests/test_api.py")
        self.assertEqual(calls[1]["tool"], "file.read_text")
        self.assertEqual(calls[1]["args"]["file"], "tests/test_cli.py")

    def test_interactive_memory_and_session_commands(self) -> None:
        with (
            tempfile.TemporaryDirectory(),
            patch("harness.cli.load_secrets"),
            patch("harness.cli.ModelRegistry") as registry_mock,
            patch("harness.cli.MemoryStore") as memory_cls,
            patch("builtins.input", side_effect=[
                "/memory add Always run tests with uv",
                "/memory list",
                "/session save my-session",
                "/session list",
                "/compact",
                "/exit",
            ]),
            patch("sys.stdout", io.StringIO()) as stdout,
        ):
            reg = registry_mock.from_config.return_value
            reg.route_for.return_value.model = "test-model"
            reg.route_for.return_value.provider = "test-provider"
            reg.routes_metadata.return_value = {}
            mem_inst = memory_cls.return_value
            mem_inst.render_system_memory_context.return_value = ""
            mem_inst.list_all.return_value = []
            mem_inst.load_project_guidelines.return_value = ""
            mem_inst.list_sessions.return_value = ["my-session"]
            exit_code = cli.main([])
            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("Workspace Memory", output)

    def test_repair_truncated_json(self) -> None:
        truncated = '{"tool": "sandbox.run_python", "args": {"code": "import os\\nwith open(config_path) as'
        repaired = cli.repair_truncated_json(truncated)
        self.assertTrue(repaired.endswith('"}}'))
        parsed = json.loads(repaired)
        self.assertEqual(parsed["tool"], "sandbox.run_python")
        self.assertIn("with open(config_path) as", parsed["args"]["code"])

    def test_parse_tool_calls_truncated_recovery(self) -> None:
        truncated = '{"tool": "sandbox.run_python", "args": {"code": "print(123)'
        calls = cli.parse_tool_calls(truncated)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "sandbox.run_python")
        self.assertEqual(calls[0]["args"]["code"], "print(123)")

    def test_config_command_max_tokens(self) -> None:
        ui = cli.ChatUI(
            config={"chat": {"agent_loop": {"max_tokens": 4096}}},
            graph="nim",
            node="example",
            model="test-model",
        )
        ui.handle_config_command("/config set max_tokens 8192")
        self.assertEqual(ui.config["chat"]["agent_loop"]["max_tokens"], 8192)

    def test_feature_command_toggle_and_set(self) -> None:
        ui = cli.ChatUI(
            config={"chat": {"streaming": {"enabled": True}}},
            graph="nim",
            node="example",
            model="test-model",
        )
        ui.handle_feature_command("/feature toggle streaming.enabled")
        self.assertFalse(ui.config["chat"]["streaming"]["enabled"])
        ui.handle_feature_command("/feature enable streaming.enabled")
        self.assertTrue(ui.config["chat"]["streaming"]["enabled"])

    def test_route_command_switch(self) -> None:
        models = cli.ModelRegistry.from_config(
            {
                "models": {
                    "nim.example": {"provider": "nvidia_nim", "model": "test-nemotron"},
                    "nim.llama-70b": {"provider": "nvidia_nim", "model": "test-llama"},
                }
            }
        )
        ui = cli.ChatUI(
            config={"chat": {}},
            graph="nim",
            node="example",
            model="test-nemotron",
            models=models,
        )
        switched = ui.handle_route_command("/route nim.llama-70b")
        self.assertIsNotNone(switched)
        self.assertEqual(switched[0], "nim")
        self.assertEqual(switched[1], "llama-70b")
        self.assertEqual(switched[2], "test-llama")
        self.assertEqual(ui.model, "test-llama")

    def test_config_command_generic_dot_path(self) -> None:
        ui = cli.ChatUI(
            config={"custom": {"nested": {"value": "initial"}}},
            graph="nim",
            node="example",
            model="test-model",
        )
        ui.handle_config_command("/config set custom.nested.value updated")
        self.assertEqual(ui.config["custom"]["nested"]["value"], "updated")

    def test_skill_command_view_and_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            skills = cli.SkillRegistry(workspace_root=tmp_dir)
            ui = cli.ChatUI(
                config={},
                graph="nim",
                node="example",
                model="test-model",
                skills=skills,
            )
            ui.handle_skill_command("/skill create test-skill A sample skill")
            skill = skills.get_skill("test-skill")
            self.assertIsNotNone(skill)
            self.assertEqual(skill.metadata.name, "test-skill")

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                ui.handle_skill_command("/skill list")
                ui.handle_skill_command("/skill view test-skill")
            output = stdout.getvalue()
            self.assertIn("test-skill", output)

    def test_mcp_command_list_and_tools(self) -> None:
        from harness.registries.mcp_registry import MCPServerConfig

        mcp_reg = cli.MCPRegistry(
            {
                "utility": MCPServerConfig(
                    name="utility",
                    transport="stdio",
                    command=sys.executable,
                    args=["-m", "harness.mcp_server"],
                )
            }
        )
        try:
            ui = cli.ChatUI(
                config={},
                graph="nim",
                node="example",
                model="test-model",
                mcp=mcp_reg,
            )
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                ui.handle_mcp_command("/mcp list")
                ui.handle_mcp_command("/mcp tools")
            output = stdout.getvalue()
            self.assertIn("utility", output)
            self.assertIn("hash_text", output)
        finally:
            mcp_reg.close_all()


if __name__ == "__main__":
    unittest.main()

