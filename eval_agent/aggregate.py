"""Aggregate Agent E2E v1.5 reports without mixing earlier dataset versions."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATASET_VERSION = "1.5.0"


def percentile(values: list[float], rank: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * rank
    lower = math.floor(position)
    upper = math.ceil(position)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-repeats", type=int, default=3)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--report", action="append", default=[], help="Exact raw report filename; repeatable")
    args = parser.parse_args()
    if args.min_repeats < 1:
        raise ValueError("--min-repeats must be positive")

    dataset = json.loads((ROOT / "cases.json").read_text(encoding="utf-8-sig"))
    if dataset.get("dataset_version") != DATASET_VERSION:
        raise RuntimeError("Expected Agent E2E dataset v1.5.0")
    case_ids = [case["case_id"] for case in dataset["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise RuntimeError("Duplicate case ID in dataset")

    by_case: dict[str, list[dict]] = defaultdict(list)
    report_paths = []
    if args.report:
        if len(args.report) != len(set(args.report)):
            raise ValueError("Duplicate explicit report filename")
        paths = []
        for name in args.report:
            if Path(name).name != name or not name.startswith("agent_") or name.startswith("agent_summary_"):
                raise ValueError(f"Expected an exact raw agent report filename: {name}")
            path = ROOT / "results" / name
            if not path.is_file():
                raise FileNotFoundError(path)
            paths.append(path)
    else:
        paths = [path for path in sorted((ROOT / "results").glob("agent_*.json"))
                 if not path.name.startswith("agent_summary_")]
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8-sig"))
        if report.get("dataset_version") != DATASET_VERSION:
            continue
        seen = set()
        for result in report.get("results", []):
            case_id = result["case_id"]
            if case_id not in case_ids or case_id in seen:
                raise RuntimeError(f"Unexpected or duplicate case ID in {path.name}: {case_id}")
            seen.add(case_id)
            by_case[case_id].append({"report": path.name, **result})
        report_paths.append(path.name)

    per_case = []
    all_latencies = []
    postflight = Counter()
    for case_id in case_ids:
        results = by_case[case_id]
        latencies = [float(row["elapsed_seconds"]) for row in results if isinstance(row.get("elapsed_seconds"), (int, float))]
        all_latencies.extend(latencies)
        for row in results:
            postflight.update(row.get("observed", {}).get("postflight_actions") or [])
        passed = sum(row.get("passed") is True for row in results)
        per_case.append({
            "case_id": case_id,
            "attempts": len(results),
            "passed": passed,
            "pass_rate": round(passed / len(results), 3) if results else None,
            "latency_p50_seconds": percentile(latencies, 0.5),
            "latency_p95_seconds": percentile(latencies, 0.95),
            "failures": [
                {"report": row["report"], "error": row.get("error"),
                 "failed_checks": [key for key, value in row.get("checks", {}).items() if value is False],
                 "ticket_id": row.get("observed", {}).get("ticket_id"),
                 "task_id": row.get("observed", {}).get("task_id")}
                for row in results if not row.get("passed")
            ],
        })

    quality_rows = [
        row.get("reply_quality_checks")
        for rows in by_case.values() for row in rows
        if row.get("reply_quality_checks")
    ]
    reply_quality_metrics = {
        "scored_pending_review_outputs": len(quality_rows),
        "pending_review_disclosed": sum(row.get("pending_review_disclosed") is True for row in quality_rows),
        "no_refund_completion_claim": sum(row.get("no_refund_completion_claim") is True for row in quality_rows),
        "order_not_refunded": sum(row.get("order_not_refunded") is True for row in quality_rows),
        "state_consistent_outputs": sum(all(row.values()) for row in quality_rows),
    }
    summary = {
        "dataset_version": DATASET_VERSION,
        "evaluator_version": "1.6.0-reply-consistency",
        "reply_quality_metrics": reply_quality_metrics,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_cases": len(case_ids),
        "target_repeats_per_case": args.min_repeats,
        "report_selection": "explicit" if args.report else "all_matching_v1_5",
        "complete": all(row["attempts"] >= args.min_repeats for row in per_case),
        "reports": report_paths,
        "total_attempts": sum(row["attempts"] for row in per_case),
        "total_passed": sum(row["passed"] for row in per_case),
        "rejected_before_worker_attempts": sum(1 for rows in by_case.values() for row in rows if row.get("observed", {}).get("submit_status") == 404),
        "accepted_submit_attempts": sum(1 for rows in by_case.values() for row in rows if row.get("observed", {}).get("submit_status") == 202),
        "cases_without_attempts": [row["case_id"] for row in per_case if row["attempts"] == 0],
        "latency_p50_seconds": percentile(all_latencies, 0.5),
        "latency_p95_seconds": percentile(all_latencies, 0.95),
        "postflight_actions": dict(postflight),
        "model_usage_and_cost": "not_observed",
        "tool_call_sequence": "not_observed",
        "raw_model_reply_before_postflight": "not_observed",
        "per_case": per_case,
    }
    checkpoint_paths = sorted((ROOT / "results").glob("checkpoint_observability_v1_5_*.json"))
    if checkpoint_paths and not args.report:
        checkpoint_path = checkpoint_paths[-1]
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
        accepted_ids = {
            row["observed"]["ticket_id"]
            for rows in by_case.values() for row in rows
            if row.get("observed", {}).get("submit_status") == 202
        }
        checkpoint_ids = {entry["ticket_id"] for entry in checkpoint.get("entries", [])}
        if accepted_ids != checkpoint_ids or len(checkpoint_ids) != len(checkpoint["entries"]):
            raise RuntimeError("Checkpoint report does not match accepted Agent tickets")
        summary["checkpoint_observability_report"] = checkpoint_path.name
        summary["model_usage_and_cost"] = {
            "model_calls": checkpoint["model_calls"],
            "input_tokens": checkpoint["input_tokens"],
            "output_tokens": checkpoint["output_tokens"],
            "total_tokens": checkpoint["total_tokens"],
            "cache_read_tokens": checkpoint["cache_read_tokens"],
            "cost": "not_observed",
        }
        summary["tool_call_sequence"] = "observed_in_checkpoint_observability_report"
        summary["raw_model_reply_before_postflight"] = "observed_in_checkpoint_observability_report"
        summary["postflight_reply_changed"] = checkpoint["postflight_reply_changed"]
    print(f"Agent E2E v1.5: {summary['total_passed']}/{summary['total_attempts']} passed; complete={summary['complete']}")
    for row in per_case:
        print(f"  {row['case_id']}: {row['passed']}/{row['attempts']}")
    if args.write:
        output = ROOT / "results" / f"agent_summary_v1_5_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%fZ}.json"
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Report: {output}")


if __name__ == "__main__":
    main()


