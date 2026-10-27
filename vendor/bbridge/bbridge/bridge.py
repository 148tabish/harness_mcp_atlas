"""FastAPI app: Anthropic /v1/messages -> Amazon Bedrock Converse.

One upstream call shape (non-streaming Converse at the application inference
profile ARN, Bearer-authenticated with a Bedrock API key). Streaming clients get
the complete answer replayed as Anthropic SSE, with ping frames while waiting.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import secrets
import urllib.parse
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .credentials import CredentialsError, EnvKeyProvider, KeyProvider
from .errors import (ClassifiedError, ErrorKind, classify_bedrock_error,
                     forward_upstream_headers, to_anthropic_error_response)
from .sse import PING_FRAME, error_frame, replay_message
from .stream_tee import StreamTee
from .translate import (REASONING_EFFORTS, TranslationError,
                        anthropic_to_converse_request, converse_to_anthropic_response)

_LOG = logging.getLogger("bbridge")

DEFAULT_REGION = "ap-south-1"
DEFAULT_MODEL_NAME = "glm-5.3-bedrock"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


class Config:
    def __init__(self):
        self.region = os.environ.get("BB_REGION", "").strip() or DEFAULT_REGION
        self.model_arn = os.environ.get("BB_MODEL_ARN", "").strip()
        self.model_name = os.environ.get("BB_MODEL_NAME", "").strip() or DEFAULT_MODEL_NAME
        self.upstream_url = os.environ.get("BB_UPSTREAM_URL", "").strip() or (
            f"https://bedrock-runtime.{self.region}.amazonaws.com/model/"
            f"{urllib.parse.quote(self.model_arn, safe='')}/converse")
        self.bridge_secret = os.environ.get("BB_BRIDGE_SECRET", "").strip()
        self.thinking_sig_key = os.environ.get("BB_THINKING_SIG_KEY", "").strip().encode() or None
        effort = os.environ.get("BB_REASONING_EFFORT", "").strip().lower()
        self.reasoning_effort = effort if effort in REASONING_EFFORTS else None
        self.stream_log_path = os.environ.get("BB_STREAM_LOG_PATH", "").strip() or None
        self.read_timeout = _env_float("BB_READ_TIMEOUT_S", 600.0)
        self.connect_timeout = _env_float("BB_CONNECT_TIMEOUT_S", 30.0)
        self.max_retries = _env_int("BB_MAX_RETRIES", 3)
        self.max_wait = _env_int("BB_MAX_WAIT_S", 30)
        self.ping_interval = _env_float("BB_PING_INTERVAL_S", 10.0)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_AUTH_HEADER_NAMES = ("x-bbridge-secret", "x-api-key", "authorization")


def _client_secret(request: Request) -> str:
    for h in _AUTH_HEADER_NAMES:
        v = request.headers.get(h, "")
        if not v:
            continue
        if h == "authorization" and v.lower().startswith("bearer "):
            return v[len("bearer "):].strip()
        return v.strip()
    return ""


def _auth_ok(cfg: Config, request: Request) -> bool:
    return not cfg.bridge_secret or _client_secret(request) == cfg.bridge_secret


def _backoff(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    wait = min(cap, base * (2 ** attempt))
    return max(0.1, wait + wait * random.uniform(-0.2, 0.2))


# ---------------------------------------------------------------------------
# Upstream call with retries
# ---------------------------------------------------------------------------

async def call_converse(
    cfg: Config, key_provider: KeyProvider, body: dict[str, Any], client: httpx.AsyncClient,
) -> tuple[dict[str, Any] | None, dict[str, str], ClassifiedError | None]:
    """(converse_json, upstream_headers, error). Retries throttles, 5xx and network drops."""
    attempt = 0
    last: ClassifiedError | None = None
    hdrs: dict[str, str] = {}
    while True:
        try:
            api_key = key_provider.get()
        except CredentialsError as e:
            return None, {}, ClassifiedError(ErrorKind.AUTH, 500, "authentication_error", str(e))
        try:
            resp = await client.post(
                cfg.upstream_url, json=body,
                headers={"authorization": f"Bearer {api_key}", "content-type": "application/json",
                         "accept": "application/json"},
                timeout=httpx.Timeout(cfg.read_timeout, connect=cfg.connect_timeout),
            )
        except httpx.HTTPError as e:
            last = ClassifiedError(ErrorKind.NETWORK, 502, "api_error", f"upstream network error: {e}")
            _LOG.warning("network error attempt=%d: %s", attempt, e)
        else:
            hdrs = dict(resp.headers)
            if 200 <= resp.status_code < 300:
                try:
                    return resp.json(), hdrs, None
                except json.JSONDecodeError:
                    return None, hdrs, ClassifiedError(ErrorKind.UNKNOWN, 502, "api_error",
                                                       "upstream returned non-JSON success body")
            last = classify_bedrock_error(resp.status_code, resp.content, hdrs)
            _LOG.info("upstream error status=%d kind=%s msg=%s", resp.status_code, last.kind.value, last.message[:200])
            if not last.kind.is_retryable:
                return None, hdrs, last
        if attempt >= cfg.max_retries:
            return None, hdrs, last
        await asyncio.sleep(min(cfg.max_wait, _backoff(attempt)))
        attempt += 1


def _err_response(c: ClassifiedError, upstream_hdrs: dict[str, str]) -> JSONResponse:
    status, body, err_hdrs = to_anthropic_error_response(c)
    fwd = forward_upstream_headers(upstream_hdrs, error_kind=c.kind.value)
    fwd.update(err_hdrs)
    return JSONResponse(body, status_code=status, headers=fwd)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def build_app(
    key_provider: KeyProvider | None = None,
    config: Config | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    cfg = config or Config()
    key_prov: KeyProvider = key_provider or EnvKeyProvider()

    if not cfg.bridge_secret:
        _LOG.warning("BB_BRIDGE_SECRET is not set: the bridge is UNAUTHENTICATED; any local "
                     "process can spend the Bedrock account. Set it to lock down.")
    if cfg.thinking_sig_key is None:
        _LOG.warning("BB_THINKING_SIG_KEY is empty: thinking-block signatures will be blank.")
    if not cfg.model_arn:
        _LOG.error("BB_MODEL_ARN is not set: every request will fail")

    own_client = http_client is None
    client_instance = http_client or httpx.AsyncClient()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if own_client:
                with contextlib.suppress(Exception):
                    await client_instance.aclose()

    app = FastAPI(title="bbridge", version="0.1.0", lifespan=_lifespan)
    app.state._client = client_instance

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({
            "ok": True,
            "upstream": f"bedrock-runtime.{cfg.region}.amazonaws.com",
            "model_arn": cfg.model_arn,
            "model_default": cfg.model_name,
            "stream_mode": "buffered",
            "reasoning_effort_override": cfg.reasoning_effort,
        })

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        if not _auth_ok(cfg, request):
            return JSONResponse({"type": "error", "error": {"type": "authentication_error",
                                                            "message": "invalid or missing bridge secret"}},
                                status_code=401)
        raw = await request.body()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            return JSONResponse({"type": "error", "error": {"type": "invalid_request_error",
                                                            "message": f"invalid JSON: {e}"}}, status_code=400)
        try:
            converse_body = anthropic_to_converse_request(body, reasoning_effort_override=cfg.reasoning_effort)
        except TranslationError as e:
            return JSONResponse({"type": "error", "error": {"type": e.error_type, "message": str(e)}}, status_code=400)
        except Exception as e:  # noqa: BLE001
            _LOG.exception("request translation failed")
            return JSONResponse({"type": "error", "error": {"type": "invalid_request_error",
                                                            "message": f"bbridge: request translation failed: {e}"}},
                                status_code=400)

        # the name the client asked for comes back on the message, so LiteLLM's cost
        # lookup and the trajectory's model_name read "glm-5.3", never the ARN
        model_name = str(body.get("model") or cfg.model_name)
        streaming = bool(body.get("stream"))
        req_id = secrets.token_hex(8)
        tee = StreamTee(path=cfg.stream_log_path, source="agent", request_id=req_id, model=model_name) \
            if cfg.stream_log_path else None
        if tee:
            tee.event(kind="status", event="request", delta=f"stream={int(streaming)} msgs={len(converse_body['messages'])}")

        client = app.state._client

        if not streaming:
            data, hdrs, err = await call_converse(cfg, key_prov, converse_body, client)
            if err is not None:
                if tee:
                    tee.event(kind="error", event="error", delta=err.message[:200]); tee.close()
                return _err_response(err, hdrs)
            try:
                anth = converse_to_anthropic_response(data or {}, model_name, cfg.thinking_sig_key)
            except Exception as e:  # noqa: BLE001
                _LOG.exception("response translation failed")
                return _err_response(ClassifiedError(ErrorKind.UNKNOWN, 500, "api_error",
                                                     f"bbridge: response translation failed: {e}"), hdrs)
            if tee:
                tee.event(kind="status", event="response", delta=anth["stop_reason"]); tee.close()
            return JSONResponse(anth, status_code=200, headers=forward_upstream_headers(hdrs))

        async def gen():
            task = asyncio.create_task(call_converse(cfg, key_prov, converse_body, client))
            try:
                while not task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=cfg.ping_interval)
                    except TimeoutError:
                        yield PING_FRAME
                data, _hdrs, err = task.result()
            except asyncio.CancelledError:
                task.cancel()
                raise
            if err is not None:
                yield error_frame(err.anthropic_error_type, err.message)
                if tee:
                    tee.event(kind="error", event="error", delta=err.message[:200]); tee.close()
                return
            try:
                anth = converse_to_anthropic_response(data or {}, model_name, cfg.thinking_sig_key)
            except Exception as e:  # noqa: BLE001
                _LOG.exception("response translation failed")
                yield error_frame("api_error", f"bbridge: response translation failed: {e}")
                return
            for frame in replay_message(anth):
                yield frame
            if tee:
                tee.event(kind="status", event="message_stop", delta=anth["stop_reason"]); tee.close()

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"bbridge-stream-mode": "buffered"})

    return app
