"""Verify preference replacement and revocation through isolated API and worker."""
from __future__ import annotations

import argparse
import asyncio
import json
import selectors
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from celery import Celery

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from eval_agent import run_memory as memory_eval  # noqa: E402
from eval_agent.run import BASE_URL, REDIS_URL, require_eval_environment  # noqa: E402
from eval_agent.seed import apply_fixture  # noqa: E402


async def run(run_number: int, timeout: int) -> dict:
    require_eval_environment()
    memory_eval.configure_identity(run_number)
    from app.agent.customer_memory import read_scoped_preferences, store_preferences
    from app.agent.graph import close_pool

    await apply_fixture(memory_eval.FIXTURE, __import__("os").environ["EVAL_V3_PASSWORD"])
    celery = Celery("memory_lifecycle_reader", broker=REDIS_URL, backend=REDIS_URL)
    evidence = {}
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=20, trust_env=False) as client:
            token = await memory_eval.login(client, memory_eval.ALICE)
            existing = await asyncio.to_thread(
                read_scoped_preferences, memory_eval.TENANT_ID, memory_eval.ALICE,
            )
            if existing:
                raise RuntimeError("Synthetic customer already has memory; choose another run number")
            steps = [
                ("email", "我的耳机偶尔断连。以后请通过邮件联系我。"),
                ("phone", "我的耳机仍然断连。以后改用电话联系我。"),
                ("revoke", "我的耳机问题还在。请删除我的联系偏好。"),
            ]
            for label, topic in steps:
                ticket = await memory_eval.submit_and_wait(client, celery, token, topic, timeout)
                if ticket["task_status"] != "success":
                    raise RuntimeError(f"{label} ticket failed: {ticket['task_status']}")
                job = await memory_eval.wait_for_memory_job(
                    celery, ticket.get("memory_write_task_id"), timeout,
                )
                if job != {"state": "SUCCESS", "stored": 1}:
                    raise RuntimeError(f"{label} memory write incomplete: {job}")
                direct = await memory_eval.direct_read(
                    memory_eval.TENANT_ID, memory_eval.ALICE, ticket["ticket_id"],
                )
                evidence[label] = {
                    "ticket_id": ticket["ticket_id"],
                    "memory_write_job": job,
                    "direct_read": direct,
                }
            stale = await asyncio.to_thread(
                store_preferences,
                memory_eval.TENANT_ID, memory_eval.ALICE,
                str(evidence["email"]["ticket_id"]),
                [("contact_preference", "联系偏好：邮件联系")],
            )
            after_stale = await memory_eval.direct_read(
                memory_eval.TENANT_ID, memory_eval.ALICE, evidence["revoke"]["ticket_id"],
            )
    finally:
        await close_pool()

    def values(label):
        return [item["text"] for item in evidence[label]["direct_read"].get("memories", [])]

    checks = {
        "initial_email": values("email") == ["联系偏好：邮件联系"],
        "phone_replaces_email": values("phone") == ["联系偏好：电话联系"],
        "revoke_hides_preference": evidence["revoke"]["direct_read"].get("status") == "no_match",
        "stale_job_skipped": stale == 0,
        "stale_job_did_not_restore": after_stale.get("status") == "no_match",
    }
    return {
        "evaluation_type": "customer_memory_lifecycle_e2e",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_number": run_number,
        "checks": checks,
        "passed": all(checks.values()),
        "evidence": {**evidence, "stale_write_count": stale, "after_stale": after_stale},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-external-llm", action="store_true")
    parser.add_argument("--run-number", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    memory_eval.configure_identity(args.run_number)
    print(f"Preview: {memory_eval.ALICE}; three synthetic tickets")
    if not args.apply:
        return
    if not args.allow_external_llm:
        parser.error("Three Agent tickets call the configured LLM")
    try:
        result = asyncio.run(
            run(args.run_number, args.timeout_seconds),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    except Exception as error:
        result = {"passed": False, "error": f"{type(error).__name__}: {error}"}
    output = ROOT / "results" / f"memory_lifecycle_{datetime.now(timezone.utc):%Y%m%d_%H%M%SZ}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Lifecycle E2E: {sum(result.get('checks', {}).values())}/{len(result.get('checks', {}))}; report: {output}")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
