"""Small fixed-case eval runner for graph behavior regression checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from harness.config import load_config
from harness.engine.runner import GraphRunner
from harness.engine.store import SQLiteRunStore


def _deep_get(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


async def run_cases(cases_path: Path, config_path: Path) -> dict[str, Any]:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    config = load_config(config_path)
    results = []
    with tempfile.TemporaryDirectory() as directory:
        config.setdefault("database", {})["sqlite_path"] = str(Path(directory) / "evals.sqlite3")
        runner = GraphRunner(SQLiteRunStore(config["database"]["sqlite_path"]), config=config)
        for case in cases:
            snapshot = await runner.run(case["graph"], case.get("input", {}))
            checks = []
            for expected_path, expected_value in case.get("expect", {}).items():
                actual = _deep_get(snapshot.model_dump(mode="json"), expected_path)
                checks.append(
                    {
                        "path": expected_path,
                        "expected": expected_value,
                        "actual": actual,
                        "passed": actual == expected_value,
                    }
                )
            results.append(
                {
                    "name": case["name"],
                    "run_id": snapshot.run_id,
                    "passed": all(check["passed"] for check in checks),
                    "checks": checks,
                }
            )
    return {"passed": all(result["passed"] for result in results), "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run fixed graph eval cases")
    parser.add_argument("--cases", default="evals/cases.json")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    report = asyncio.run(run_cases(Path(args.cases), Path(args.config)))
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
