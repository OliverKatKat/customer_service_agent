"""Prepare the small synthetic demo, then run its API and optional live Agent worker."""

import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "infra/workbench-demo/.env"


def configure():
    from dotenv import dotenv_values

    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(
            "# Local synthetic demo only; do not commit.\n"
            + f"DEMO_DB_PASSWORD={secrets.token_hex(18)}\nDEMO_SECRET_KEY={secrets.token_hex(32)}\nDEMO_PASSWORD=EchoDemo2026!\nDEMO_APPROVAL_CODE=demo-only-approval-20261028\n",
            encoding="utf-8",
        )
    values = dotenv_values(CONFIG)
    required = [
        "DEMO_DB_PASSWORD",
        "DEMO_SECRET_KEY",
        "DEMO_PASSWORD",
        "DEMO_APPROVAL_CODE",
    ]
    if any(not values.get(key) for key in required):
        raise SystemExit("Demo configuration is incomplete")
    password = values["DEMO_DB_PASSWORD"]
    if not password.isalnum():
        raise SystemExit("Demo DB password must be alphanumeric")
    if len(values["DEMO_PASSWORD"]) < 12 or len(values["DEMO_APPROVAL_CODE"]) < 16:
        raise SystemExit("Demo account/approval credentials are too short")
    model_file = dotenv_values(ROOT / "app/.env")
    model_key = (
        os.environ.get("DEEPSEEK_API_KEY") or model_file.get("DEEPSEEK_API_KEY") or ""
    )
    model_ready = bool(model_key and not model_key.startswith(("test-", "demo-")))
    env = os.environ.copy()
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "DATABASE_URL": f"postgresql+asyncpg://wb_demo:{password}@127.0.0.1:55459/workbench_demo",
            "READONLY_DATABASE_URL": "",
            "LOGISTICS_DATABASE_URL": f"postgresql://wb_logistics_reader:{password}@127.0.0.1:55459/workbench_demo",
            "ANALYTICS_READONLY_DATABASE_URL": f"postgresql+asyncpg://wb_demo_reader:{password}@127.0.0.1:55459/workbench_demo",
            "REDIS_URL": "redis://127.0.0.1:56380/0",
            "QDRANT_URL": "http://127.0.0.1:56335",
            "SECRET_KEY": values["DEMO_SECRET_KEY"],
            "ADMIN_PASSWORD": values["DEMO_PASSWORD"],
            "ADMIN_APPROVAL_CODE": values["DEMO_APPROVAL_CODE"],
            "DEMO_PASSWORD": values["DEMO_PASSWORD"],
            "KNOWLEDGE_RETRIEVAL_MODE": "bm25",
            "DEMO_DATA_ONLY": "false" if model_ready else "true",
        }
    )
    for key in ["DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"]:
        if not env.get(key) and not model_file.get(key):
            env[key] = "demo-unconfigured"
    return env, values, model_ready


def prepare(env, values):
    from psycopg import connect, sql

    commands = [
        [
            "docker",
            "compose",
            "-p",
            "workbench-demo",
            "--env-file",
            str(CONFIG),
            "-f",
            str(CONFIG.parent / "compose.yaml"),
            "up",
            "-d",
            "--wait",
        ],
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, "scripts/seed_workbench_demo.py", "--apply"],
    ]
    for command in commands:
        subprocess.run(command, cwd=ROOT, env=env, check=True)
    with connect(
        host="127.0.0.1",
        port=55459,
        dbname="workbench_demo",
        user="wb_demo",
        password=values["DEMO_DB_PASSWORD"],
    ) as connection:
        exists = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='wb_demo_reader')"
        ).fetchone()[0]
        if not exists:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE wb_demo_reader LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                ).format(sql.Literal(values["DEMO_DB_PASSWORD"]))
            )
        connection.execute("GRANT CONNECT ON DATABASE workbench_demo TO wb_demo_reader")
        connection.execute("GRANT USAGE ON SCHEMA public TO wb_demo_reader")
        connection.execute("GRANT SELECT ON case_registry TO wb_demo_reader")
        if not connection.execute("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='wb_logistics_reader')").fetchone()[0]:
            connection.execute(sql.SQL("CREATE ROLE wb_logistics_reader LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS").format(sql.Literal(values["DEMO_DB_PASSWORD"])))
        connection.execute("GRANT CONNECT ON DATABASE workbench_demo TO wb_logistics_reader")
        connection.execute("GRANT USAGE ON SCHEMA logistics TO wb_logistics_reader")
        connection.execute("GRANT SELECT ON logistics.shipments, logistics.shipment_events TO wb_logistics_reader")



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    env, values, model_ready = configure()
    prepare(env, values)
    print(
        "\nSynthetic demo ready: 28 orders, 14 cases, 15 contacts. Existing demo edits are preserved.",
        flush=True,
    )
    print(
        "Accounts: demo_echo_alice / demo_echo_staff / demo_echo_analyst / demo_echo_admin",
        flush=True,
    )
    print("Demo-only password: " + values["DEMO_PASSWORD"], flush=True)
    print("Demo-only approval code: " + values["DEMO_APPROVAL_CODE"], flush=True)
    print(
        "Live Agent uses existing model configuration."
        if model_ready
        else "No model key detected: data/material browsing only; chat dispatch and approval are disabled.",
        flush=True,
    )
    if args.prepare_only:
        return
    children = []
    handles = []
    try:
        if model_ready:
            logdir = ROOT / "logs"
            logdir.mkdir(exist_ok=True)
            for command, name in [
                (
                    [
                        sys.executable,
                        "-m",
                        "celery",
                        "-A",
                        "app.workers.celery_app:celery_app",
                        "worker",
                        "--pool=solo",
                        "--loglevel=INFO",
                    ],
                    "demo-worker.log",
                ),
                (
                    [
                        sys.executable,
                        "-m",
                        "celery",
                        "-A",
                        "app.workers.celery_app:celery_app",
                        "beat",
                        "--loglevel=INFO",
                        "--schedule",
                        str(logdir / "demo-beat"),
                    ],
                    "demo-beat.log",
                ),
            ]:
                handle = (logdir / name).open("a", encoding="utf-8")
                handles.append(handle)
                children.append(
                    subprocess.Popen(
                        command,
                        cwd=ROOT,
                        env=env,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if sys.platform == "win32"
                        else 0,
                    )
                )
        print(
            f"Open http://127.0.0.1:{args.port}/demo ; Ctrl+C stops this API and its owned worker processes.",
            flush=True,
        )
        subprocess.run(
            [
                sys.executable,
                "scripts/run_api.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    main()
