"""Restore one synthetic Qdrant snapshot into a temporary isolated collection."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SOURCE = "company_knowledge_eval_v3__merchant_eval_agent"
PROBE = "jd_restore_probe_20261020"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-name", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if "/" in args.snapshot_name or "\\" in args.snapshot_name or ".." in args.snapshot_name:
        parser.error("Snapshot name must be a plain filename")
    print(f"Source={SOURCE}; snapshot={args.snapshot_name}; temporary collection={PROBE}")
    if not args.apply:
        return
    if os.environ.get("QDRANT_URL") != "http://127.0.0.1:56333":
        raise RuntimeError("Restore verification is restricted to isolated eval Qdrant")

    from qdrant_client import QdrantClient

    client = QdrantClient(url=os.environ["QDRANT_URL"], trust_env=False, timeout=60)
    report = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_collection": SOURCE,
        "snapshot_name": args.snapshot_name,
        "probe_collection": PROBE,
        "passed": False,
    }
    created = False
    try:
        if not client.collection_exists(SOURCE):
            raise RuntimeError("Source collection is missing")
        if client.collection_exists(PROBE):
            raise RuntimeError("Probe collection already exists; refusing to overwrite it")
        snapshots = {item.name for item in client.list_snapshots(SOURCE)}
        if args.snapshot_name not in snapshots:
            raise RuntimeError("Snapshot is not listed for the source collection")
        location = f"file:///qdrant/snapshots/{SOURCE}/{args.snapshot_name}"
        client.recover_snapshot(collection_name=PROBE, location=location, wait=True)
        created = client.collection_exists(PROBE)
        if not created:
            raise RuntimeError("Restore did not create the probe collection")
        source_count = client.count(collection_name=SOURCE, exact=True).count
        restored_count = client.count(collection_name=PROBE, exact=True).count
        report.update({
            "source_points_at_check": source_count,
            "restored_points": restored_count,
            "passed": source_count == restored_count and restored_count > 0,
        })
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if created:
            client.delete_collection(PROBE)
            report["probe_removed"] = not client.collection_exists(PROBE)
        client.close()
        output = ROOT / "eval_agent" / "results" / f"qdrant_restore_{datetime.now(timezone.utc):%Y%m%d_%H%M%SZ}.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(output)
        print(f"Restore check: {'PASS' if report['passed'] else 'FAIL'}")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
