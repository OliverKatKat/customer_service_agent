"""Opt-in response contract for new routes; legacy API responses stay unchanged."""

import base64
import hashlib
import hmac
import json
import uuid

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm.exc import StaleDataError

from app.core.config import settings


def envelope(request: Request, data, **metadata):
    return {
        "data": data,
        "meta": {
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "schema_version": "v1",
            **metadata,
        },
        "error": None,
    }


class ContractRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request: Request):
            request.state.request_id = str(uuid.uuid4())
            try:
                return await handler(request)
            except (HTTPException, RequestValidationError, StaleDataError) as exc:
                if isinstance(exc, RequestValidationError):
                    status, message = 422, "请求字段或参数不合法"
                elif isinstance(exc, StaleDataError):
                    status, message = 409, "数据版本已变化，请刷新后重试"
                else:
                    status, message = exc.status_code, str(exc.detail)
                codes = {
                    401: "AUTH_REQUIRED",
                    403: "PERMISSION_DENIED",
                    404: "RESOURCE_NOT_FOUND",
                    409: "VERSION_CONFLICT",
                    422: "INVALID_REQUEST",
                    503: "DEPENDENCY_UNAVAILABLE",
                }
                body = envelope(request, None)
                body["error"] = {
                    "code": codes.get(status, "REQUEST_FAILED"),
                    "message": message,
                }
                return JSONResponse(
                    body, status_code=status, headers=getattr(exc, "headers", None)
                )

        return wrapped


def make_cursor(last_id: int | str, scope: str):
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"id": last_id, "scope": scope}, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(
        settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return payload + "." + signature


def read_cursor(cursor: str | None, scope: str, id_type=int):
    if cursor is None:
        return 0 if id_type is int else ""
    try:
        payload, signature = cursor.split(".")
        expected = hmac.new(
            settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        obj = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        identifier = obj["id"]
        valid_id = (
            (type(identifier) is int and identifier >= 1)
            if id_type is int
            else (type(identifier) is str and 1 <= len(identifier) <= 64)
        )
        if obj["scope"] != scope or not valid_id:
            raise ValueError()
        return obj["id"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise HTTPException(422, "分页游标不合法或筛选条件已变化") from None
