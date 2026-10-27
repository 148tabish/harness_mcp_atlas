"""Bedrock error classification and the Anthropic-shaped error envelope."""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorKind(str, Enum):
    AUTH = "auth"
    PERMISSION = "permission"
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    THROTTLE = "throttle"
    MODEL_TIMEOUT = "model_timeout"
    UPSTREAM_5XX = "upstream_5xx"
    NETWORK = "network"
    UNKNOWN = "unknown"

    @property
    def is_retryable(self) -> bool:
        return self in {ErrorKind.THROTTLE, ErrorKind.MODEL_TIMEOUT,
                        ErrorKind.UPSTREAM_5XX, ErrorKind.NETWORK}


@dataclass
class ClassifiedError:
    kind: ErrorKind
    http_status: int
    anthropic_error_type: str
    message: str
    retry_after_seconds: int | None = None
    raw_body: Any = None


# Bedrock names the exception in the x-amzn-errortype header ("ValidationException:http://...")
# and repeats a human message in the JSON body ({"message": "..."}).
_BY_EXCEPTION: dict[str, tuple[ErrorKind, int, str]] = {
    "UnrecognizedClientException": (ErrorKind.AUTH, 401, "authentication_error"),
    "InvalidSignatureException": (ErrorKind.AUTH, 401, "authentication_error"),
    "ExpiredTokenException": (ErrorKind.AUTH, 401, "authentication_error"),
    "AccessDeniedException": (ErrorKind.PERMISSION, 403, "permission_error"),
    "ValidationException": (ErrorKind.VALIDATION, 400, "invalid_request_error"),
    "ResourceNotFoundException": (ErrorKind.NOT_FOUND, 404, "not_found_error"),
    "ThrottlingException": (ErrorKind.THROTTLE, 429, "rate_limit_error"),
    "ServiceQuotaExceededException": (ErrorKind.THROTTLE, 429, "rate_limit_error"),
    "ModelNotReadyException": (ErrorKind.UPSTREAM_5XX, 503, "overloaded_error"),
    "ModelTimeoutException": (ErrorKind.MODEL_TIMEOUT, 504, "api_error"),
    "ModelErrorException": (ErrorKind.UPSTREAM_5XX, 502, "api_error"),
    "ServiceUnavailableException": (ErrorKind.UPSTREAM_5XX, 503, "overloaded_error"),
    "InternalServerException": (ErrorKind.UPSTREAM_5XX, 500, "api_error"),
}


def classify_bedrock_error(status: int, body: Any, headers: dict[str, str] | None = None) -> ClassifiedError:
    parsed = _parse_body(body)
    message = ""
    if isinstance(parsed, dict):
        message = str(parsed.get("message") or parsed.get("Message") or "")
    if not message:
        message = f"upstream HTTP {status}"

    exc = ""
    for k, v in (headers or {}).items():
        if k.lower() == "x-amzn-errortype":
            exc = str(v).split(":", 1)[0].strip()
            break

    if exc in _BY_EXCEPTION:
        kind, http, atype = _BY_EXCEPTION[exc]
        return ClassifiedError(kind=kind, http_status=http, anthropic_error_type=atype,
                               message=f"{exc}: {message}", raw_body=parsed)

    if status in (401,):
        return ClassifiedError(ErrorKind.AUTH, 401, "authentication_error", message, raw_body=parsed)
    if status == 403:
        return ClassifiedError(ErrorKind.PERMISSION, 403, "permission_error", message, raw_body=parsed)
    if status == 404:
        return ClassifiedError(ErrorKind.NOT_FOUND, 404, "not_found_error", message, raw_body=parsed)
    if status == 429:
        return ClassifiedError(ErrorKind.THROTTLE, 429, "rate_limit_error", message, raw_body=parsed)
    if status == 424:
        return ClassifiedError(ErrorKind.MODEL_TIMEOUT, 504, "api_error", message, raw_body=parsed)
    if 400 <= status < 500:
        return ClassifiedError(ErrorKind.VALIDATION, 400, "invalid_request_error", message, raw_body=parsed)
    if 500 <= status < 600:
        return ClassifiedError(ErrorKind.UPSTREAM_5XX, status, "api_error", message, raw_body=parsed)
    return ClassifiedError(ErrorKind.UNKNOWN, status or 500, "api_error", message, raw_body=parsed)


def _parse_body(body: Any) -> Any:
    if body is None or isinstance(body, dict):
        return body
    try:
        if isinstance(body, (bytes, bytearray)):
            return json.loads(bytes(body).decode("utf-8", errors="replace"))
        if isinstance(body, str):
            return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return None


def to_anthropic_error_response(c: ClassifiedError) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = {"type": "error", "error": {"type": c.anthropic_error_type, "message": c.message}}
    headers: dict[str, str] = {"bbridge-error-kind": c.kind.value}
    if c.retry_after_seconds is not None:
        headers["retry-after"] = str(int(c.retry_after_seconds))
    return c.http_status, body, headers


def forward_upstream_headers(upstream_headers: dict[str, str], error_kind: str | None = None) -> dict[str, str]:
    """Carry Bedrock's request id (and nothing hop-by-hop) back to the client."""
    out: dict[str, str] = {}
    for k, v in (upstream_headers or {}).items():
        kl = k.lower()
        if kl == "x-amzn-requestid":
            out["request-id"] = str(v)
            out["bbridge-upstream-x-amzn-requestid"] = str(v)
        elif kl == "retry-after":
            out["retry-after"] = str(v)
    if error_kind:
        out["bbridge-error-kind"] = error_kind
    return out
