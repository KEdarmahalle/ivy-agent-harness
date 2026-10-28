from __future__ import annotations

import unittest

from harness.config import load_config
from harness.prompts.builder import build_smart_system_prompt
from harness.registries.tool_registry import DOMAIN_PACKS, ToolRegistry
from harness.sandbox.factory import get_sandbox_provider
from harness.sandbox.local import LocalSubprocessSandbox


class PromptRuntimeConfigurationTests(unittest.TestCase):
    def build_prompt(self, provider: str, network_enabled: bool = False) -> str:
        return build_smart_system_prompt(
            base_prompt="You are an assistant.",
            tools_catalog={"time.now": {"description": "Get current time"}},
            harness_context="Runtime test context.",
            config={
                "app": {"name": "Test Harness"},
                "tools": {"sandbox": {"provider": provider, "network_enabled": network_enabled}},
            },
        )

    def test_prompt_describes_configured_local_provider_without_eager_tool_calls(self) -> None:
        prompt = self.build_prompt("local")
        self.assertIn("Execution provider: `local`", prompt)
        self.assertIn("Commands run as the current user on the host", prompt)
        self.assertIn("For a direct conversational or capability question, answer directly", prompt)
        self.assertNotIn("run tests directly", prompt.lower())
        self.assertNotIn("host shell environment", prompt.lower())

    def test_prompt_tracks_provider_and_network_configuration(self) -> None:
        prompt = self.build_prompt("docker", network_enabled=True)
        self.assertIn("Execution provider: `docker`", prompt)
        self.assertIn("enabled in tool configuration", prompt)
        self.assertNotIn("configured `local`", prompt)

    def test_sandbox_provider_is_selected_from_configuration_and_invalid_names_fail(self) -> None:
        local = get_sandbox_provider({"tools": {"sandbox": {"provider": "local"}}})
        self.assertIsInstance(local, LocalSubprocessSandbox)
        with self.assertRaisesRegex(ValueError, "unsupported sandbox provider"):
            get_sandbox_provider({"tools": {"sandbox": {"provider": "not-a-provider"}}})

    def test_sample_config_exposes_all_enabled_builtin_tools(self) -> None:
        config = load_config()
        enabled = {tool for pack in DOMAIN_PACKS.values() for tool in pack}
        allowed = set(config["tools"]["allowlist"])
        self.assertLessEqual(enabled, allowed)

    def test_configured_local_python_tool_executes_without_docker(self) -> None:
        registry = ToolRegistry.from_config(load_config())
        result = registry.invoke("sandbox.run_python", code="print('LOCAL_PROVIDER_OK')")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("LOCAL_PROVIDER_OK", result["stdout"])


if __name__ == "__main__":
    unittest.main()
