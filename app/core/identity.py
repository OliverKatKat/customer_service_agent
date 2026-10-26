"""Stable, nonreversible customer references for internal observability."""

import hashlib
import hmac

from app.core.config import settings


def customer_ref(tenant_id: str, customer_id: str) -> str:
    message = f"{tenant_id}\0{customer_id}".encode("utf-8")
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()[:16]
