"""把确定性规则检查与 LLM-as-a-Judge 的判定做对比。

读取一个 `eval_agent/run.py` 冻结报告，对每个产出了回复的案例跑 Judge，
然后输出：
- 规则 vs Judge 的一致率（agreement）
- 「规则漏判、Judge 抓到」的语义误报（semantic false positives）
- 「规则抓到、Judge 漏判」的规则独有拦截

用法：
  python -m eval_agent.run_judge                    # 预览：列出会评审的案例
  python -m eval_agent.run_judge --apply            # 实际调用 Judge
  python -m eval_agent.run_judge --report <路径>     # 指定冻结报告
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from eval_agent.judge import judge_reply  # noqa: E402

RESULTS_DIR = ROOT / "results"


def load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def latest_report() -> Path:
    reports = sorted(RESULTS_DIR.glob("agent_*.json"), key=lambda p: p.stat().st_mtime)
    if not reports:
        raise SystemExit("未找到 run.py 冻结报告，请先运行 eval_agent/run.py --apply --allow-external-llm")
    return reports[-1]


def judge_report(report: dict, apply: bool) -> dict:
    rows = []
    for item in report["results"]:
        observed = item.get("observed", {})
        reply = (observed.get("reply") or "").strip()
        # 只在真正进入 Agent 并产出回复的案例上跑 Judge
        if observed.get("submit_status") != 202 or not reply:
            continue
        verdict: dict = {"verdict": "preview"}
        if apply:
            try:
                verdict = judge_reply(observed.get("topic", ""), reply, observed)
            except Exception as exc:
                verdict = {"verdict": "error", "error": f"{type(exc).__name__}: {exc}"}
        judge_pass = verdict.get("verdict") == "pass"
        rule_pass = bool(item.get("passed"))
        rows.append({
            "case_id": item["case_id"],
            "rule_passed": rule_pass,
            "judge_verdict": verdict.get("verdict"),
            "judge_passed": judge_pass,
            "dimensions": verdict.get("dimensions"),
            "reason": verdict.get("reason", ""),
            "evidence": verdict.get("evidence", ""),
            "error": verdict.get("error", ""),
            "reply": reply[:200],
            "agreement": judge_pass == rule_pass,
        })

    total = len(rows)
    semantic_fp = [r for r in rows if r["judge_verdict"] == "fail" and r["rule_passed"]]
    rule_only = [r for r in rows if r["judge_verdict"] == "pass" and not r["rule_passed"]]
    agreement = sum(r["agreement"] for r in rows)
    return {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_report": report.get("run_at_utc"),
        "judged_count": total,
        "rule_passed_count": sum(r["rule_passed"] for r in rows),
        "judge_passed_count": sum(r["judge_verdict"] == "pass" for r in rows),
        "agreement_count": agreement,
        "agreement_rate": round(agreement / total, 3) if total else 0.0,
        "semantic_false_positives_count": len(semantic_fp),
        "semantic_false_positives": [
            {"case_id": r["case_id"], "reason": r["reason"], "evidence": r["evidence"]}
            for r in semantic_fp
        ],
        "rule_only_catches_count": len(rule_only),
        "rule_only_catches": [{"case_id": r["case_id"]} for r in rule_only],
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="run.py 冻结报告路径；缺省用最新一份")
    parser.add_argument("--apply", action="store_true", help="实际调用 LLM Judge")
    args = parser.parse_args()

    report_path = args.report or latest_report()
    report = load_report(report_path)
    result = judge_report(report, args.apply)

    eligible = len(result["rows"])
    print(f"源报告: {report_path.name}（{report.get('passed', '?')}/{report.get('total', '?')} 规则通过）")
    print(f"可评审案例: {eligible} 条（含 Agent 回复的案例）")
    if not args.apply:
        print("预览模式。加 --apply 才实际调用 LLM Judge。")
        for r in result["rows"]:
            print(f"  {r['case_id']}")
        return

    print(f"规则通过: {result['rule_passed_count']}/{eligible}")
    print(f"Judge 通过: {result['judge_passed_count']}/{eligible}")
    print(f"一致率: {result['agreement_rate']:.1%}（{result['agreement_count']}/{eligible}）")
    print(f"语义误报（规则漏判、Judge 抓到）: {result['semantic_false_positives_count']} 条")
    for fp in result["semantic_false_positives"]:
        print(f"  - {fp['case_id']}: {fp['reason']}")
        if fp["evidence"]:
            print(f"      违规句: {fp['evidence']}")
    print(f"规则独有拦截（Judge 漏判）: {result['rule_only_catches_count']} 条")
    for r in result["rule_only_catches"]:
        print(f"  - {r['case_id']}")

    out = RESULTS_DIR / f"judge_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%fZ}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告: {out}")


if __name__ == "__main__":
    main()
