"""Validate eval v3 case declarations before a runner touches isolated stores."""
from __future__ import annotations

import json
from pathlib import Path


EXPECT_KEYS = {
    ("M0", "http"): {
        "http_status", "new_ticket", "order_status_before_approval",
        "ticket_delta", "order_status", "token_issued", "no_result_leak",
        "forbidden_values",
    },
    ("M0", "tool"): {
        "status", "order_id", "order_status", "amount", "forbidden_values",
    },
    ("M1", "memory"): {
        "contains_facts", "excludes_facts", "result_count",
    },
    ("M2", "review"): {
        "http_status", "review_state", "order_status", "dispatch_delta",
        "effective_decisions", "dispatch_count", "audit_has_actor",
        "audit_has_reason", "approved_outbox_count",
    },
}
REQUIRED_M2 = {
    "approve": {"http_status", "review_state", "order_status", "dispatch_delta"},
    "approve_twice": {"effective_decisions", "dispatch_count", "audit_has_actor"},
    "reject": {
        "review_state", "order_status", "dispatch_count",
        "approved_outbox_count", "audit_has_reason",
    },
}
SUPPLEMENTAL_KEYS = {
    "M1-LOCAL-EXTRACTION-AND-WRITE": {
        "extracted_facts", "stored_count", "own_contains", "other_excludes",
    },
    "M1-LEGACY-UNSCOPED-QUARANTINE": {"forbidden_value"},
    "M1-SOURCE-TICKET-OWNERSHIP": {"forbidden_value"},
    "M1-WRITE-WRONG-CUSTOMER": {"error_type"},
    "M2-REVIEW-ROLE-VERSION-CODE": {
        "user_list_status", "stale_version_status", "wrong_code_status", "review_state",
    },
    "M3-REVIEW-PAGE-AND-CUSTOMER-STATUS": {
        "own_status", "review_status", "other_status", "detail_status",
        "order_status", "page_status", "page_contains",
    },
    "M3-RECOVERY-MANUAL-STATUS": {
        "detail_status", "processing_state", "recovery_attempts",
        "customer_status", "customer_message_contains",
    },
    "M2-TOOL-DURABLE-PENDING": {
        "review_count", "ticket_status", "order_status", "dispatch_delta",
        "first_contains", "second_contains",
    },
    "M2-OUTBOX-RETRY": {
        "first_error_contains", "failed_status", "sent_status", "third_status",
        "event_attempts", "agent_dispatch_count",
    },
    "M2-APPROVED-TOOL-ONCE": {
        "ticket_status", "order_status", "refund_audit_count",
        "first_contains", "second_contains",
    },
}


def load_dataset(path: Path) -> dict:
    dataset = json.loads(path.read_text(encoding="utf-8-sig"))
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Eval dataset must have a nonempty cases list")
    seen: set[str] = set()
    for case in cases:
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("Every eval case needs a nonempty case_id")
        if case_id in seen:
            raise ValueError(f"Duplicate eval case_id: {case_id}")
        seen.add(case_id)
        milestone, surface = case.get("milestone"), case.get("surface")
        if case.get("action") == "supplemental":
            expected_keys = SUPPLEMENTAL_KEYS.get(case_id)
            expected = case.get("expect")
            if milestone not in {"M1", "M2", "M3"} or expected_keys is None:
                raise ValueError(f"Invalid supplemental case: {case_id}")
            if not case.get("description"):
                raise ValueError(f"Supplemental case has no description: {case_id}")
            if not isinstance(expected, dict) or set(expected) != expected_keys:
                raise ValueError(f"Supplemental expectations differ from runner contract: {case_id}")
            continue
        allowed = EXPECT_KEYS.get((milestone, surface))
        expected = case.get("expect")
        if allowed is None or not isinstance(expected, dict) or not expected:
            raise ValueError(f"Unsupported or assertion-free eval case: {case_id}")
        unknown = set(expected) - allowed
        if unknown:
            raise ValueError(f"Unimplemented expectations in {case_id}: {sorted(unknown)}")
        if milestone == "M0" and surface == "http" and expected.get("no_result_leak"):
            if not expected.get("forbidden_values"):
                raise ValueError(f"No forbidden values declared for {case_id}")
        if milestone == "M2":
            required = REQUIRED_M2.get(case.get("action"))
            if required is None or required - set(expected):
                raise ValueError(f"Missing review assertions in {case_id}")
    return dataset
