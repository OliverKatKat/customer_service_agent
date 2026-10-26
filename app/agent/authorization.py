from math import isfinite
from typing import Any


def is_valid_refund_authorization(
    candidate: Any,
    *,
    ticket_id: Any,
    tenant_id: Any | None = None,
    order_id: Any | None = None,
    amount: Any | None = None,
) -> bool:
    if not isinstance(candidate, dict):
        return False

    if (
        candidate.get("source") != "admin_ticket_approval"
        or type(candidate.get("review_id")) is not int
        or candidate["review_id"] <= 0
        or candidate.get("operation") != "large_refund"
        or not isinstance(candidate.get("actor_id"), str)
        or not candidate["actor_id"].strip()
        or not isinstance(tenant_id, str)
        or not tenant_id.strip()
        or not isinstance(candidate.get("tenant_id"), str)
        or candidate["tenant_id"] != tenant_id
        or not isinstance(ticket_id, (str, int))
        or not str(ticket_id).strip()
        or str(candidate.get("ticket_id", "")) != str(ticket_id)
        or not isinstance(candidate.get("order_id"), str)
        or not candidate["order_id"].strip()
    ):
        return False

    approved_amount = candidate.get("amount")
    if type(approved_amount) not in (int, float):
        return False
    if not isfinite(float(approved_amount)) or approved_amount < 0:
        return False

    if order_id is not None and candidate["order_id"] != str(order_id):
        return False

    if amount is not None:
        if type(amount) not in (int, float) or not isfinite(float(amount)):
            return False
        if round(float(approved_amount), 2) != round(float(amount), 2):
            return False

    return True
