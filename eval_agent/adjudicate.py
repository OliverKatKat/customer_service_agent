"""Apply explicit, evidence-bound human adjudications to a frozen v1.5 summary."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
summary_path = RESULTS / "agent_summary_v1_5_20261008_155531_185195Z.json"
summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
decisions = json.loads((ROOT / "adjudications_v1_5.json").read_text(encoding="utf-8-sig"))
if summary["dataset_version"] != decisions["dataset_version"] or not summary["complete"]:
    raise RuntimeError("Dataset or completion mismatch")

validated = []
for item in decisions["adjudications"]:
    report = json.loads((RESULTS / item["report"]).read_text(encoding="utf-8-sig"))
    if report["dataset_version"] != decisions["dataset_version"]:
        raise RuntimeError("Adjudication report version mismatch")
    matches = [row for row in report["results"] if row["case_id"] == item["case_id"]]
    if len(matches) != 1 or matches[0]["observed"].get("ticket_id") != item["ticket_id"]:
        raise RuntimeError("Adjudication target mismatch")
    row = matches[0]
    if row["passed"] or item["decision"] != "evaluator_false_positive":
        raise RuntimeError("Only a recorded machine failure can be adjudicated")
    if {key for key, value in row["checks"].items() if value is False} != {"reply_forbidden"}:
        raise RuntimeError("Unexpected failed checks")
    observed = row["observed"]
    if not (
        item["case_id"] == "E2E-ALREADY-REFUNDED"
        and observed["order_status"] == "refunded"
        and observed["review_status"] is None
        and observed["task_status"] == "success"
        and "该订单已经完成退款" in observed["reply"]
        and "无法重复发起退款申请" in observed["reply"]
    ):
        raise RuntimeError("Historical-refund evidence no longer matches")
    validated.append(item)

output = {
    "dataset_version": summary["dataset_version"],
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "machine_summary_report": summary_path.name,
    "machine_passed": summary["total_passed"],
    "total_attempts": summary["total_attempts"],
    "adjudicated_false_positives": len(validated),
    "adjusted_passed": summary["total_passed"] + len(validated),
    "scope": "One targeted human semantic review; other machine passes were not manually certified",
    "adjudications": validated,
}
path = RESULTS / f"adjudicated_summary_v1_5_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%fZ}.json"
path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Machine {output['machine_passed']}/{output['total_attempts']}; adjusted {output['adjusted_passed']}/{output['total_attempts']}")
print(f"Report: {path}")
