"""Reproducible integration checks on the disposable 55458 database only."""

from datetime import datetime, timezone
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    for stream in [sys.stdout, sys.stderr]:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    os.environ.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "DATABASE_URL": "postgresql+asyncpg://wb_test:wb_local_test_only@127.0.0.1:55458/workbench_test",
            "WORKBENCH_TEST_DATABASE_URL": "postgresql+asyncpg://wb_test:wb_local_test_only@127.0.0.1:55458/workbench_test",
            "ANALYTICS_READONLY_DATABASE_URL": "postgresql+asyncpg://wb_reader:wb_reader_local_only@127.0.0.1:55458/workbench_test",
            "SECRET_KEY": "local-workbench-test-key-never-production-20261028",
            "ADMIN_PASSWORD": "local-admin-test-password",
            "ADMIN_APPROVAL_CODE": "local-test-approval-code-20261028",
            "DEEPSEEK_API_KEY": "test-disabled",
            "DASHSCOPE_API_KEY": "test-disabled",
            "QWEN_JUDGE_API_KEY": "test-disabled",
            "LLM_BASE_URL": "http://127.0.0.1:1",
            "EMBEDDING_API_BASE": "http://127.0.0.1:1",
            "QWEN_JUDGE_BASE_URL": "http://127.0.0.1:1",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "QDRANT_URL": "http://127.0.0.1:1",
            "DEMO_DATA_ONLY": "false",
            "LOGISTICS_DATABASE_URL": "",
            "KNOWLEDGE_RETRIEVAL_MODE": "hybrid",
        }
    )
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT
    )
    if migrated.returncode:
        raise SystemExit(migrated.returncode)
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), pattern="test_workbench.py"
    )
    logging.getLogger("agent").setLevel(logging.WARNING)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "passed": result.wasSuccessful(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "scope": "workbench authorization, concurrency, migrations, approval contract and real logistics MCP/PostgreSQL",
        "external_models_called": False,
        "database": "disposable 127.0.0.1:55458/workbench_test",
        "versions": {
            name: version(name)
            for name in [
                "fastapi",
                "sqlalchemy",
                "pydantic",
                "opentelemetry-api",
                "opentelemetry-sdk",
            ]
        },
    }
    output = ROOT / "eval_workbench" / "results"
    output.mkdir(parents=True, exist_ok=True)
    path = output / (
        "workbench_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Integration report saved in eval_workbench/results")
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
