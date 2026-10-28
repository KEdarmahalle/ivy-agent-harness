"""Adversarial stress-testing suite for Phase 2 (Universal Capability Packs, Plugins & SDK)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from harness.detector import detect_project
from harness.plugins import PluginRegistry
from harness.registries.tool_registry import ToolRegistry
from harness.sdk import AgentHarness


class Phase2PacksAndPluginsTests(unittest.TestCase):
    def test_custom_plugin_discovery_and_schema_reflection(self) -> None:
        """Adversarial test: Dynamic plugin script dropped in workspace plugins folder."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugins_dir = root / ".harness" / "plugins"
            plugins_dir.mkdir(parents=True)

            plugin_code = '''
from harness.plugins import tool
from typing import Optional

@tool(name="finance.calculate_roi", description="Calculate return on investment percentage", category="finance")
def calculate_roi(initial_investment: float, final_value: float, tax_rate: Optional[float] = 0.0) -> dict:
    gain = final_value - initial_investment
    net_gain = gain * (1.0 - (tax_rate or 0.0))
    roi = (net_gain / initial_investment) * 100.0
    return {"roi_percentage": round(roi, 2), "net_gain": round(net_gain, 2)}
'''
            (plugins_dir / "finance.py").write_text(plugin_code, encoding="utf-8")

            plugin_reg = PluginRegistry(workspace_root=root)
            tools = plugin_reg.list_tools()
            self.assertEqual(len(tools), 1)
            self.assertEqual(tools[0].name, "finance.calculate_roi")
            self.assertEqual(tools[0].category, "finance")
            self.assertIn("initial_investment", tools[0].input_schema)
            self.assertIn("tax_rate", tools[0].input_schema)

            # Test execution via ToolRegistry
            from harness.registries.tool_registry import SandboxPolicy

            tool_reg = ToolRegistry(
                plugins=plugin_reg,
                sandbox=SandboxPolicy(workspace_roots=[root], writable_roots=[root]),
            )
            res = tool_reg.invoke("finance.calculate_roi", initial_investment=1000.0, final_value=1500.0, tax_rate=0.1)
            self.assertEqual(res["roi_percentage"], 45.0)
            self.assertEqual(res["net_gain"], 450.0)

    def test_domain_packs_toggling_isolation(self) -> None:
        """Adversarial test: Disabling shell_sandbox pack removes all shell tools from catalog."""
        tool_reg = ToolRegistry.from_config(
            {
                "packs": {
                    "shell_sandbox": {"enabled": False},
                    "data_analytics": {"enabled": True},
                }
            }
        )
        names = tool_reg.names()
        self.assertNotIn("process.run", names)
        self.assertNotIn("sandbox.inspect", names)
        self.assertNotIn("sandbox.run_python", names)
        self.assertIn("data.sql_query", names)
        self.assertIn("data.json_transform", names)

        with self.assertRaises(KeyError):
            tool_reg.invoke("process.run", command="echo 1")

    def test_data_analytics_sql_query_and_security_gate(self) -> None:
        """Adversarial test: data.sql_query runs read queries and blocks dangerous DDL."""
        with tempfile.TemporaryDirectory() as directory:
            from harness.registries.tool_registry import SandboxPolicy

            db_path = Path(directory) / "analytics.sqlite3"
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE metrics (id INTEGER PRIMARY KEY, metric TEXT, value REAL)")
            conn.execute("INSERT INTO metrics (metric, value) VALUES ('latency', 12.4), ('throughput', 850.0)")
            conn.commit()
            conn.close()

            tool_reg = ToolRegistry(
                sandbox=SandboxPolicy(
                    workspace_roots=[Path(directory)],
                    writable_roots=[Path(directory)],
                )
            )
            res = tool_reg.invoke(
                "data.sql_query",
                query="SELECT metric, value FROM metrics WHERE value > 50",
                db_path=str(db_path),
            )
            self.assertEqual(res["row_count"], 1)
            self.assertEqual(res["rows"][0]["metric"], "throughput")

            # Verify destructive queries are forbidden
            with self.assertRaises(PermissionError):
                tool_reg.invoke(
                    "data.sql_query",
                    query="DROP TABLE metrics",
                    db_path=str(db_path),
                )

    def test_data_json_transform_keypath(self) -> None:
        """Test nested keypath extraction in JSON datasets."""
        tool_reg = ToolRegistry()
        data = {
            "store": {
                "inventory": [
                    {"sku": "A1", "price": 10},
                    {"sku": "B2", "price": 25},
                ],
                "active": True,
            }
        }
        res = tool_reg.invoke("data.json_transform", data=data, key_path="store.inventory.1.sku")
        self.assertEqual(res["result"], "B2")

    def test_data_csv_inspect(self) -> None:
        """Test CSV tabular inspector."""
        with tempfile.TemporaryDirectory() as directory:
            from harness.registries.tool_registry import SandboxPolicy

            csv_path = Path(directory) / "sales.csv"
            csv_path.write_text("date,region,amount\n2026-01-01,North,500\n2026-01-02,South,750\n", encoding="utf-8")

            tool_reg = ToolRegistry(
                sandbox=SandboxPolicy(
                    workspace_roots=[Path(directory)],
                    writable_roots=[Path(directory)],
                )
            )

            res = tool_reg.invoke("data.csv_inspect", file="sales.csv")
            self.assertEqual(res["column_count"], 3)
            self.assertEqual(res["total_rows"], 2)
            self.assertEqual(res["headers"], ["date", "region", "amount"])

    def test_project_stack_detection(self) -> None:
        """Test universal project stack and domain auto-detection."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text("[project]\nname = 'test-app'", encoding="utf-8")
            (root / "tests").mkdir()

            profile = detect_project(root)
            self.assertIn("Python", profile.languages)
            self.assertTrue(profile.has_tests)
            self.assertIn("core", profile.recommended_packs)

    def test_universal_sdk_embedding(self) -> None:
        """Test embedding harness via 1-line Python SDK."""
        with tempfile.TemporaryDirectory() as directory:
            harness = AgentHarness.from_project(directory)
            tools = harness.list_tools()
            self.assertIn("environment.info", tools)
            self.assertIn("time.now", tools)
            self.assertIn("data.json_transform", tools)

    def test_broken_syntax_plugin_resilience(self) -> None:
        """Adversarial test: Plugin file with corrupted/broken Python syntax must not crash discovery."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugins_dir = root / ".harness" / "plugins"
            plugins_dir.mkdir(parents=True)

            # 1. Broken syntax file
            (plugins_dir / "broken.py").write_text("def syntax_error(::: this is invalid", encoding="utf-8")

            # 2. Valid plugin file
            valid_code = '''
from harness.plugins import tool

@tool(name="valid.echo")
def echo_message(msg: str) -> dict:
    return {"echo": msg}
'''
            (plugins_dir / "valid.py").write_text(valid_code, encoding="utf-8")

            plugin_reg = PluginRegistry(workspace_root=root)
            tools = plugin_reg.list_tools()
            self.assertEqual(len(tools), 1)
            self.assertEqual(tools[0].name, "valid.echo")

    def test_dynamic_plugin_hot_reload(self) -> None:
        """Adversarial test: Plugins dropped into directory after initialization are picked up on reload."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugins_dir = root / ".harness" / "plugins"
            plugins_dir.mkdir(parents=True)

            plugin_reg = PluginRegistry(workspace_root=root)
            self.assertEqual(len(plugin_reg.list_tools()), 0)

            # Drop new plugin during runtime
            new_code = '''
from harness.plugins import tool

@tool(name="ops.healthcheck")
def healthcheck() -> dict:
    return {"status": "healthy"}
'''
            (plugins_dir / "health.py").write_text(new_code, encoding="utf-8")
            plugin_reg.discover_plugins()

            self.assertEqual(len(plugin_reg.list_tools()), 1)
            self.assertEqual(plugin_reg.list_tools()[0].name, "ops.healthcheck")


if __name__ == "__main__":
    unittest.main()
